"""`cr review` — the local dev loop. `cr review-pr` — GitHub Actions.

Iterate on prompts locally, never by pushing to GitHub.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from cr import bench as bm
from cr import benchrun as br
from cr import evals as ev
from cr.config import TIERS, settings
from cr.diff import DiffSet, collect, parse
from cr.doctor import run as doctor_run
from cr.github import MARKER, GitHubPR, PRRef, build_review, pr_from_env
from cr.lint import analyse
from cr.llm.prefix import PRContext, RepoContext
from cr.models import ReviewResult, Severity
from cr.repo import RepoCache, default_cache_dir
from cr.review.engine import review as run_review
from cr.store import db as store
from cr.triage import triage
from cr.warm import context_for_pr, index_repo


def _force_utf8() -> None:
    """Windows consoles default to cp1252, which cannot encode the arrows and box
    characters Rich emits. Must run before the Console is constructed."""
    for stream in (sys.stdout, sys.stderr):
        enc = (getattr(stream, "encoding", "") or "").lower()
        if enc.replace("-", "") != "utf8" and hasattr(stream, "reconfigure"):
            with contextlib.suppress(ValueError, OSError):
                stream.reconfigure(encoding="utf-8", errors="replace")


_force_utf8()

app = typer.Typer(add_completion=False, help="High-precision AI code review")
console = Console()

SEVERITY_STYLE = {
    Severity.CRITICAL: "bold red",
    Severity.HIGH: "red",
    Severity.MEDIUM: "yellow",
    Severity.LOW: "dim",
}


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _render(result: ReviewResult) -> None:
    if result.errors:
        console.print("[red]INCOMPLETE review: one or more model/context calls failed.[/red]")
        for error in result.errors:
            console.print(f"  {error}", markup=False)
    elif not result.posted:
        console.print("\n[green]No findings survived verification.[/green]")
    for v in result.posted:
        f = v.finding
        sev = v.final_severity
        style = SEVERITY_STYLE[sev]
        body = [
            f"[{style}]{sev.upper()}[/{style}]  [dim]{f.category} · "
            f"confidence {f.confidence:.0%} · found by {f.found_by}[/dim]\n",
            f"[bold]{v.final_claim}[/bold]\n",
            f"[dim]How it breaks:[/dim] {v.final_failure_scenario}\n",
        ]
        if f.suggested_fix:
            body.append(f"\n[dim]Suggested fix:[/dim]\n{f.suggested_fix}")
        console.print(
            Panel(
                "".join(body),
                title=f"{f.anchor_file}:{f.anchor_line}",
                border_style=style,
                title_align="left",
            )
        )

    console.print(
        f"[dim]raw={len(result.raw_findings)} prefiltered={len(result.prefiltered)} "
        f"deduplicated={len(result.deduplicated)} refuted={len(result.refuted)} "
        f"memory={len(result.memory_suppressed)} cap-trimmed={len(result.budget_trimmed)}[/dim]"
    )
    if result.cache_hit:
        console.print(
            f"[green]Exact-input cache hit: $0 model spend "
            f"(original estimate ${result.cached_cost_usd:.4f}).[/green]"
        )

    u = result.usage
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_row("tier", result.tier)
    t.add_row("posted / suppressed", f"{len(result.posted)} / {len(result.suppressed)}")
    t.add_row("verifier kill rate", f"{result.verifier_kill_rate:.0%}")
    t.add_row("tokens in / out", f"{u.input_tokens:,} / {u.output_tokens:,}")
    t.add_row(
        "cache write / read",
        f"{u.cache_creation_input_tokens:,} / {u.cache_read_input_tokens:,}",
    )
    t.add_row("cache hit ratio", f"{u.cache_hit_ratio:.0%}")
    t.add_row("cost", f"${result.cost_usd:.4f}")
    t.add_row("elapsed", f"{result.elapsed_s:.1f}s")
    console.print(Panel(t, title="run", border_style="dim", title_align="left"))

    if u.cache_read_input_tokens == 0 and u.cache_creation_input_tokens > 0:
        console.print(
            "[dim]Provider cache written. Later identical prefixes may be cheaper "
            "while their TTL remains valid.[/dim]"
        )
    elif 0 < u.cache_hit_ratio < 0.2:
        console.print(
            "[yellow]warning:[/yellow] cache hit ratio is low — check for a silent "
            "invalidator, or a prefix below the model minimum. See PIPELINE.md §2.2."
        )


def _require_credentials() -> None:
    """Provider-aware credential check — Foundry uses a different key entirely."""
    if settings.provider == "foundry":
        if not settings.azure_api_key:
            console.print("[red]Set CR_AZURE_API_KEY (CR_PROVIDER=foundry).[/red]")
            raise typer.Exit(2)
        if not (settings.azure_resource or settings.azure_base_url):
            console.print("[red]Set CR_AZURE_RESOURCE or CR_AZURE_BASE_URL.[/red]")
            raise typer.Exit(2)
        return
    if not settings.anthropic_api_key and not os.environ.get("ANTHROPIC_API_KEY"):
        console.print("[red]No Anthropic API key. Set CR_ANTHROPIC_API_KEY.[/red]")
        raise typer.Exit(2)


@app.command()
def review(
    repo: Annotated[Path, typer.Argument(help="Repo to review")] = Path("."),
    base: Annotated[str | None, typer.Option("--base", "-b", help="Base ref, e.g. main")] = None,
    staged: Annotated[bool, typer.Option("--staged", help="Only what is git-added")] = False,
    tier: Annotated[str | None, typer.Option("--tier", "-t", help="Force T1/T2/T3")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
    trace: Annotated[
        Path | None, typer.Option("--trace", help="Save complete JSON stage trace")
    ] = None,
) -> None:
    """Review staged changes, uncommitted changes, or `base`..HEAD."""
    _setup_logging(verbose)

    diff = collect(str(repo), base, staged=staged)
    if not diff.files:
        console.print("[yellow]No changes to review.[/yellow]")
        raise typer.Exit(0)

    decision = triage(diff)
    console.print(
        f"[dim]{diff.total_files} files, {diff.total_hunks} hunks -> "
        f"[bold]{decision.tier}[/bold] ({decision.reason})[/dim]"
    )

    if decision.is_skip and not tier:
        console.print("[green]Skipped - no review needed. $0.00 spent.[/green]")
        raise typer.Exit(0)

    cfg = TIERS[tier.upper()] if tier else decision.config
    assert cfg is not None
    console.print(f"[dim]model={cfg.model} effort={cfg.effort} finders={len(cfg.finders)}[/dim]\n")

    reviewable = {f.path for f in decision.reviewable}
    filtered = [f for f in diff.files if f.path in reviewable]

    pr = PRContext(
        title=f"Local changes in {repo.resolve().name}",
        description=(
            f"Changes since {base}."
            if base
            else "Staged changes."
            if staged
            else "Uncommitted working-tree changes."
        ),
        diff=DiffSet(files=filtered, base=diff.base, head=diff.head).render(),
    )
    _require_credentials()

    result = asyncio.run(
        run_review(
            repo=RepoContext(slug=repo.resolve().as_posix()),
            pr=pr,
            tier=cfg,
            source="local",
        )
    )
    _render(result)
    if trace:
        trace.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    if result.errors:
        raise typer.Exit(1)


def _parse_pr_arg(value: str) -> PRRef | None:
    m = re.match(r"^([^/\s]+)/([^#\s]+)#(\d+)$", value.strip())
    return PRRef(m.group(1), m.group(2), int(m.group(3))) if m else None


@app.command("review-pr")
def review_pr(
    pr: Annotated[
        str | None,
        typer.Option("--pr", help="owner/repo#123. Defaults to the Actions environment."),
    ] = None,
    repo_path: Annotated[
        Path, typer.Option("--repo-path", help="Local checkout, used for linting")
    ] = Path("."),
    tier: Annotated[str | None, typer.Option("--tier", "-t")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print instead of posting")] = False,
    no_graph: Annotated[bool, typer.Option("--no-graph", help="Skip cross-file context")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Bypass the exact-input review cache")
    ] = False,
    trace: Annotated[
        Path | None, typer.Option("--trace", help="Save complete JSON stage trace")
    ] = None,
) -> None:
    """Review a GitHub pull request and post the findings.

    With no arguments it resolves the PR from GITHUB_REPOSITORY / GITHUB_EVENT_PATH
    and authenticates with GITHUB_TOKEN, so it just works inside Actions.
    """
    _setup_logging(verbose)
    _require_credentials()

    ref = _parse_pr_arg(pr) if pr else pr_from_env()
    if ref is None:
        console.print(
            "[red]Could not resolve the pull request.[/red] Pass --pr owner/repo#123, "
            "or run inside GitHub Actions."
        )
        raise typer.Exit(2)

    token = settings.github_token
    if not token:
        console.print("[red]GITHUB_TOKEN is not set.[/red] Add it to .env or export it.")
        raise typer.Exit(2)

    console.print(f"[dim]Reviewing {ref.slug}#{ref.number}[/dim]")

    with GitHubPR(ref, token) as gh:
        meta = gh.metadata()
        diffset = DiffSet(
            files=parse(gh.diff()),
            base=meta["base"]["sha"],
            head=meta["head"]["sha"],
        )

        if not diffset.files:
            console.print("[yellow]Empty diff; nothing to review.[/yellow]")
            raise typer.Exit(0)

        decision = triage(diffset)
        console.print(
            f"[dim]{diffset.total_files} files, {diffset.total_hunks} hunks -> "
            f"[bold]{decision.tier}[/bold] ({decision.reason})[/dim]"
        )
        if decision.is_skip and not tier:
            console.print("[green]Skipped - no review needed. $0.00 spent.[/green]")
            raise typer.Exit(0)

        cfg = TIERS[tier.upper()] if tier else decision.config
        assert cfg is not None

        reviewable = [f.path for f in decision.reviewable]
        lint = analyse(str(repo_path), reviewable)
        if lint.rules:
            console.print(f"[dim]linters reported {len(lint.rules)} rule(s); suppressed[/dim]")

        keep = set(reviewable)
        slice_text = (
            ""
            if no_graph
            else context_for_pr(
                ref.slug,
                ref.number,
                meta["head"]["sha"],
                meta["base"]["sha"],
                keep,
                token,
            )
        )
        if slice_text:
            console.print(f"[dim]graph context: {len(slice_text):,} chars[/dim]")

        pr_ctx = PRContext(
            title=meta.get("title") or f"PR #{ref.number}",
            description=(meta.get("body") or "")[:4000],
            diff=DiffSet(files=[f for f in diffset.files if f.path in keep]).render(),
            graph_slice=slice_text,
            lint_output=lint.output,
            suppressed_rules=lint.rules,
        )

        result = asyncio.run(
            run_review(
                repo=RepoContext(slug=ref.slug),
                pr=pr_ctx,
                tier=cfg,
                source="pr",
                pr_number=ref.number,
                head_sha=meta["head"]["sha"],
                use_cache=False if refresh else None,
            )
        )
        _render(result)
        if trace:
            trace.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        if result.errors:
            console.print("[red]Not posting an incomplete review. Fix the failure and rerun.[/red]")
            raise typer.Exit(1)

        already = set() if dry_run else gh.posted_fingerprints()
        body, comments = build_review(
            result.posted,
            commentable=diffset.commentable_map(),
            new_text=diffset.new_text_map(),
            already=already,
            tier=result.tier,
            cost=result.cost_usd,
            elapsed=result.elapsed_s,
            killed=len(result.suppressed),
        )

        if dry_run:
            console.print("\n[yellow]--dry-run: not posting.[/yellow]\n")
            console.print(body)
            for c in comments:
                console.print(f"  inline -> {c['path']}:{c['line']}")
            raise typer.Exit(0)

        gh.submit_review(body, comments, meta["head"]["sha"])
        console.print(f"[green]Posted {len(comments)} inline comment(s).[/green]")


@app.command()
def doctor(
    model: Annotated[
        str | None,
        typer.Option(
            "--model", "-m", help="Registry name, e.g. claude-sonnet-5 or groq:openai/gpt-oss-120b"
        ),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Probe a model: reachability, structured outputs, effort, and prompt caching.

    Costs a fraction of a cent. Run this before your first review, and before
    routing a tier to a model you have not used yet — on Foundry, caching is a
    beta capability, so whether it actually works is a real question and every
    cost estimate depends on the answer.
    """
    _setup_logging(verbose)
    target = model or TIERS["T2"].model

    report = doctor_run(settings, target)
    console.print(f"[dim]provider={report.provider or '?'} model={target}[/dim]\n")

    def mark(ok: bool) -> str:
        return "[green]yes[/green]" if ok else "[red]no[/red]"

    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_row("protocol", report.wire or "-")
    t.add_row("in registry", mark(report.known))
    t.add_row("reachable", mark(report.reachable))
    t.add_row("structured outputs", mark(report.structured_outputs))
    t.add_row(
        "effort",
        mark(report.effort_accepted) if report.effort_supported else "[dim]not supported[/dim]",
    )
    t.add_row(
        "prompt caching",
        mark(report.caching_works) + f" [dim]({report.cache_mode or 'unknown'})[/dim]",
    )
    t.add_row("cache write / read", f"{report.cache_write_tokens:,} / {report.cache_read_tokens:,}")
    t.add_row("probe cost", f"${report.cost_usd:.5f}")
    console.print(Panel(t, title="doctor", border_style="dim", title_align="left"))

    for err in report.errors:
        console.print(f"[red]error:[/red] {err}")

    if not report.known:
        console.print(
            "\n[yellow]This model is not in the registry.[/yellow] It is priced at a "
            "fallback rate; add it to cr/llm/registry.py so costs are real."
        )
    if report.healthy and not report.caching_works:
        if report.caching_expected:
            console.print(
                "\n[yellow]Caching is not taking effect.[/yellow] It still works, but input "
                "costs will be roughly 5x the figures in the docs. Check that prompt caching "
                "is enabled for this deployment."
            )
        else:
            console.print(
                "\n[dim]No cache hit observed on the second call. This model has no explicit "
                "cache, so that is expected; any automatic caching shows up in run costs.[/dim]"
            )
    if not report.healthy:
        raise typer.Exit(1)
    console.print("\n[green]Ready.[/green]")


@app.command()
def models(
    provider: Annotated[
        str | None, typer.Option("--provider", "-p", help="Only this provider id")
    ] = None,
) -> None:
    """List every provider and model CR knows, and which providers are configured."""
    from cr.llm.client import provider_status
    from cr.llm.registry import CATALOG, PROVIDERS

    status = provider_status(settings)
    pt = Table(title="Providers", title_justify="left", box=None, padding=(0, 2))
    for col in ("id", "provider", "protocol", "status"):
        pt.add_column(col)
    for pid, p in PROVIDERS.items():
        st = status[pid]
        pt.add_row(
            pid,
            p.label,
            p.wire.value,
            "[green]configured[/green]"
            if st["configured"]
            else f"[dim]set {', '.join(st['missing'])}[/dim]",
        )
    console.print(pt)

    mt = Table(title="\nModels", title_justify="left", box=None, padding=(0, 2))
    mt.add_column("ref", no_wrap=True)
    for col in ("model", "$ in / out per 1M", "cache", "effort"):
        mt.add_column(col)
    for m in CATALOG:
        if provider and m.provider != provider:
            continue
        price = (
            f"{m.pricing.input:g} / {m.pricing.output:g}" if m.pricing else "[dim]unpriced[/dim]"
        )
        levels = m.effort_levels
        effort = f"{levels[0]}–{levels[-1]}" if m.supports_effort else "-"
        label = m.label if m.verified else f"{m.label} [yellow](unverified)[/yellow]"
        mt.add_row(m.ref, label, price, m.cache.value, effort)
    console.print(mt)


@app.command()
def index(
    slug: Annotated[str, typer.Argument(help="owner/repo")],
    ref: Annotated[str, typer.Option("--ref", help="Branch or SHA to index")] = "HEAD",
    force: Annotated[bool, typer.Option("--force", help="Rebuild even if cached")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Clone and index a repo so later reviews are fast and see cross-file context.

    Run once per repo. Re-run after big merges to keep the index close to HEAD;
    reviews will otherwise build the delta themselves on first use.
    """
    _setup_logging(verbose)
    token = settings.github_token
    if not token:
        console.print("[yellow]No GITHUB_TOKEN — private repos will fail.[/yellow]")

    r = index_repo(slug, token, ref=ref, force=force)

    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_row("repo", r.slug)
    t.add_row("commit", r.commit[:12])
    t.add_row("clone", "cold (first time)" if r.was_cold else "warm")
    t.add_row("index", "reused" if r.reused else f"built in {r.seconds:.1f}s")
    t.add_row("files / symbols / refs", f"{r.files:,} / {r.symbols:,} / {r.refs:,}")
    console.print(Panel(t, title="index", border_style="dim", title_align="left"))
    console.print("\n[green]Warm.[/green] Reviews on this repo now include cross-file context.")


@app.command("cache-info")
def cache_info() -> None:
    """Show what is cached on disk."""
    cache = RepoCache(default_cache_dir())
    t = Table("repo", "mirror", "indexes")
    mirrors = sorted((cache.root / "mirrors").glob("*.git"))
    graphs = cache.root / "graphs"
    for m in mirrors:
        slug = m.stem.replace("__", "/")
        n = len(list((graphs / m.stem).glob("*.json.gz"))) if (graphs / m.stem).is_dir() else 0
        size = sum(f.stat().st_size for f in m.rglob("*") if f.is_file()) / 1e6
        t.add_row(slug, f"{size:,.0f} MB", str(n))
    if not mirrors:
        console.print(f"[dim]Nothing cached yet in {cache.root}[/dim]")
        return
    console.print(t)
    console.print(f"[dim]{cache.root}[/dim]")


@app.command()
def learn(
    pr: Annotated[str, typer.Option("--pr", help="owner/repo#123")],
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Record human rejections so those findings never come back.

    Reads resolved review threads and thumbs-down reactions; each rejected
    fingerprint is stored per repo and filtered out of future reviews.
    """
    _setup_logging(verbose)
    ref = _parse_pr_arg(pr)
    if ref is None:
        console.print("[red]Use --pr owner/repo#123[/red]")
        raise typer.Exit(2)
    token = settings.github_token
    if not token:
        console.print("[red]GITHUB_TOKEN is not set.[/red]")
        raise typer.Exit(2)

    with GitHubPR(ref, token) as gh:
        resolved = gh.resolved_threads()
        down = gh.thumbs_down()
        bodies = {}
        for c in gh.review_comments():
            for fp in MARKER.findall(c.get("body") or ""):
                bodies[fp] = ((c.get("body") or "")[:400], c.get("path") or "")

    added = 0
    for fps, reason in ((resolved, "resolved"), (down, "thumbs_down")):
        for fp in fps:
            body, path = bodies.get(fp, ("", ""))
            if store.suppress(
                ref.slug, fp, reason=reason, claim=body, file=path, pr_number=ref.number
            ):
                added += 1

    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_row("resolved threads", str(len(resolved)))
    t.add_row("thumbs-down", str(len(down)))
    t.add_row("newly suppressed", str(added))
    console.print(Panel(t, title="learn", border_style="dim", title_align="left"))
    if added:
        console.print("[green]These will not be posted again on this repo.[/green]")


@app.command()
def stats(
    repo: Annotated[str | None, typer.Option("--repo", help="owner/repo")] = None,
) -> None:
    """Run history and suppression-memory effectiveness."""
    d = store.stats(repo)
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_row("runs", f"{d['runs']:,}")
    t.add_row("comments posted", f"{d['posted']:,}")
    t.add_row("killed by verifier", f"{d['killed']:,}")
    t.add_row("total cost", f"${d['cost_usd']:.2f}")
    t.add_row("suppressions stored", f"{d['suppressions']:,}")
    t.add_row("suppressions fired", f"{d['suppression_hits']:,}")
    console.print(Panel(t, title="stats", border_style="dim", title_align="left"))


@app.command("eval")
def eval_cmd(
    fixture: Annotated[
        str | None, typer.Option("--fixture", "-f", help="Fixture name; omit for all")
    ] = None,
    runs: Annotated[int, typer.Option("--runs", "-n", help="Repeats, to show variance")] = 1,
    tier: Annotated[str, typer.Option("--tier", "-t")] = "T2",
    no_graph: Annotated[bool, typer.Option("--no-graph", help="Measure without context")] = False,
    directory: Annotated[Path, typer.Option("--dir")] = Path("evals/fixtures"),
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Score the reviewer against PRs whose bugs are known.

    Recall is trustworthy; precision is a lower bound, since an unmatched finding
    may be a real defect nobody labelled.
    """
    _setup_logging(verbose)
    _require_credentials()

    fixtures = [f for f in ev.load_all(directory) if not fixture or fixture in f.name]
    if not fixtures:
        console.print(f"[yellow]No fixtures in {directory}[/yellow]")
        raise typer.Exit(1)

    for fx in fixtures:
        console.print(f"\n[bold]{fx.name}[/bold]  [dim]{fx.pr}[/dim]")
        scores = ev.evaluate(fx, settings, tier_name=tier, runs=runs, use_graph=not no_graph)
        d = ev.summarise(fx, scores)

        t = Table(show_header=False, box=None, padding=(0, 2))
        spread = (
            f"{d['recall_mean']:.0%}"
            if runs == 1
            else f"{d['recall_mean']:.0%}  (min {d['recall_min']:.0%}, max {d['recall_max']:.0%})"
        )
        t.add_row("known bugs", str(d["expected"]))
        t.add_row("recall", spread)
        t.add_row("precision (lower bound)", f"{d['precision_mean']:.0%}")
        t.add_row("comments posted", f"{d['posted_mean']:.1f}")
        t.add_row("cost / run", f"${d['cost_mean']:.4f}")
        t.add_row("cache hit", f"{d['cache_ratio_mean']:.0%}")
        console.print(Panel(t, title=f"{runs} run(s)", border_style="dim", title_align="left"))

        if runs > 1 and d["found_every_run"] != d["found_at_least_once"]:
            flaky = set(d["found_at_least_once"]) - set(d["found_every_run"])
            console.print(f"[yellow]flaky:[/yellow] {', '.join(sorted(flaky))}")
        if d["never_found"]:
            console.print(f"[red]missed:[/red] {', '.join(d['never_found'])}")
        else:
            console.print("[green]all known bugs found[/green]")

        for s in scores[:1]:
            for extra in s.extra:
                console.print(f"[dim]unlabelled finding: {extra}[/dim]")


@app.command()
def serve(
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port")] = 8000,
) -> None:
    """Run the dashboard API (and the built UI, if dashboard/dist exists)."""
    import uvicorn

    console.print(f"[green]http://{host}:{port}[/green]  [dim]API docs at /api/docs[/dim]")
    uvicorn.run("cr.server:app", host=host, port=port, log_level="warning")


@app.command("bench-mine")
def bench_mine(
    slug: Annotated[str, typer.Argument(help="owner/repo")],
    prs: Annotated[int, typer.Option("--prs", help="How many merged PRs to mine")] = 10,
    per_pr: Annotated[int, typer.Option("--per-pr", help="Max comments per PR")] = 2,
    directory: Annotated[Path, typer.Option("--dir")] = Path("evals/fixtures"),
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Mine human review comments from merged PRs into a benchmark fixture.

    Ground truth is what human reviewers flagged (c-CRAB methodology), recorded
    against the exact commit they were looking at.
    """
    _setup_logging(verbose)
    token = settings.github_token
    if not token:
        console.print("[red]GITHUB_TOKEN is not set.[/red]")
        raise typer.Exit(2)

    console.print(f"[dim]Mining {slug}...[/dim]")
    found = bm.mine(slug, token, limit_prs=prs, max_comments_per_pr=per_pr)
    if not found:
        console.print("[yellow]No substantive human review comments found.[/yellow]")
        raise typer.Exit(1)

    fixture = bm.to_fixture(slug, found, f"{slug} — human review benchmark")
    path = bm.save_fixture(fixture, directory)

    t = Table("PR", "file", "reviewer", "comment")
    for e in fixture["prs"]:
        for h in e["human_reviews"]:
            t.add_row(
                e["pr"].split("#")[1],
                (h["file"] or "")[-34:],
                h["author"],
                h["comment"][:58] + "...",
            )
    console.print(t)
    console.print(
        f"[green]{len(found)} comments across {len(fixture['prs'])} PRs[/green] -> {path}"
    )


@app.command("bench")
def bench_cmd(
    fixture: Annotated[Path, typer.Argument(help="Fixture JSON from bench-mine")],
    limit: Annotated[int | None, typer.Option("--limit", "-n", help="Only the first N PRs")] = None,
    tier: Annotated[str, typer.Option("--tier", "-t")] = "T2",
    no_graph: Annotated[bool, typer.Option("--no-graph")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Score the reviewer against human reviewers on real merged PRs.

    Each PR is reviewed at the commit the human was looking at, then an LLM judge
    decides whether we raised the same concern.
    """
    _setup_logging(verbose)
    _require_credentials()

    summary, results = br.run_benchmark(
        fixture, settings, tier_name=tier, limit=limit, use_graph=not no_graph
    )

    t = Table("PR", "human", "matched", "posted", "cost", "note")
    for r in results:
        t.add_row(
            r.pr.split("#")[1],
            str(r.expected),
            "-" if r.skipped else f"{r.matched}/{r.expected}",
            "-" if r.skipped else str(r.posted),
            f"${r.cost:.3f}",
            r.skipped[:38] or "",
        )
    console.print(t)

    s = Table(show_header=False, box=None, padding=(0, 2))
    s.add_row("PRs scored", f"{summary['prs_scored']} of {summary['prs_attempted']}")
    s.add_row("human comments", str(summary["human_comments"]))
    s.add_row("agreed with human", f"{summary['matched']} ({summary['recall']:.0%})")
    s.add_row("our comments posted", str(summary["posted"]))
    s.add_row("precision (lower bound)", f"{summary['precision']:.0%}")
    s.add_row("total cost", f"${summary['cost']:.2f}")
    console.print(Panel(s, title=summary["repo"], border_style="dim", title_align="left"))

    for r in results:
        for d in r.details:
            mark = "[green]match[/green]" if d["matched"] else "[dim]miss [/dim]"
            console.print(f"{mark} [dim]{r.pr}[/dim] {d['human'][:90]}")
            if not d["matched"]:
                console.print(f"        [dim]{d['why'][:100]}[/dim]")


@app.command()
def tiers() -> None:
    """Show the routing table."""
    t = Table("tier", "finder model", "verifier model", "effort", "finders", "verifiers", "max")
    for name, c in TIERS.items():
        # Tiers that set verifier_model escalate (T3) or switch provider (T4)
        # for verification; the finders stay on their own model either way.
        verifier = c.verifier_model or c.model
        t.add_row(
            name,
            c.model,
            verifier + (" *" if verifier != c.model else ""),
            c.effort,
            str(len(c.finders)),
            str(len(c.verifier_lenses)),
            str(c.max_comments),
        )
    console.print(t)
    console.print(
        "[dim]T0 skips lockfiles and generated files entirely - no model call, $0.00.\n"
        "* T3 escalates verification to a stronger model; finders stay cheaper.[/dim]"
    )


app_cli = typer.Typer(no_args_is_help=True, help="The GitHub App: install once, reviews forever.")
app.add_typer(app_cli, name="app")


@app_cli.command("serve")
def app_serve(
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port")] = 8000,
    public_url: Annotated[
        str,
        typer.Option(
            "--public-url",
            help="Base URL your browser uses for the setup flow. Defaults to host:port.",
        ),
    ] = "",
    webhook_url: Annotated[
        str,
        typer.Option(
            "--webhook-url",
            help="Where GitHub POSTs events, if that is not <public-url>/webhook "
            "(e.g. a smee.io relay). Must be reachable from the internet.",
        ),
    ] = "",
    reload: Annotated[bool, typer.Option("--reload", help="Reload on code changes")] = False,
    role: Annotated[
        str, typer.Option("--role", help="all | web | worker — what this process does")
    ] = "",
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Run the GitHub App: webhook ingress, review workers and the dashboard.

    GitHub's servers must be able to reach the webhook URL. A tunnel serves
    everything from one public host:

        cloudflared tunnel --url http://localhost:8010
        cr app serve --port 8010 --public-url https://<tunnel-host>

    A relay gives you a public inbox instead, so the two URLs differ — the
    browser still talks to localhost:

        npx smee-client --url https://smee.io/<id> --target http://localhost:8010/webhook
        cr app serve --port 8010 --webhook-url https://smee.io/<id>
    """
    import uvicorn

    from cr.app.service import check_ready, create_app

    _setup_logging(verbose)
    logging.getLogger("cr").setLevel(logging.DEBUG if verbose else logging.INFO)
    if role:
        settings.app_role = role

    for problem in check_ready():
        console.print(f"[yellow]![/yellow] {problem}")

    base = public_url.rstrip("/") or f"http://{host}:{port}"
    hook = webhook_url.rstrip("/") or f"{base}/webhook"
    console.print(f"[green]http://{host}:{port}[/green]  [dim]dashboard + API[/dim]")
    console.print(f"[dim]webhook  {hook}[/dim]")
    if hook.startswith(("http://localhost", "http://127.0.0.1", "http://0.0.0.0")):
        console.print(
            "[yellow]![/yellow] GitHub cannot reach that webhook URL. Start a tunnel or "
            "relay and pass [bold]--public-url[/bold] or [bold]--webhook-url[/bold]."
        )
    if not settings.app_configured():
        console.print(f"[bold]Create the App:[/bold] {base}/app/setup")
    elif settings.github_app_slug:
        console.print(
            f"[dim]install   https://github.com/apps/{settings.github_app_slug}"
            "/installations/new[/dim]"
        )

    if reload:
        # Reload needs an import string, so the public URL travels by env var.
        os.environ["CR_APP_PUBLIC_URL"] = base
        os.environ["CR_APP_WEBHOOK_URL"] = webhook_url
        uvicorn.run("cr.app.service:reloadable_app", host=host, port=port, reload=True)
        return
    uvicorn.run(
        create_app(public_url=base, webhook_url=webhook_url),
        host=host,
        port=port,
        log_level="warning",
    )


@app_cli.command("status")
def app_status() -> None:
    """What the App is, what it is installed on, and what it is doing."""
    from cr.app.manifest import load_credentials
    from cr.app.service import check_ready

    for problem in check_ready():
        console.print(f"[yellow]![/yellow] {problem}")

    creds = load_credentials(settings.credentials_path()) or {}
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_row("configured", "yes" if settings.app_configured() else "no")
    t.add_row("app id", str(settings.github_app_id or "-"))
    t.add_row("slug", settings.github_app_slug or creds.get("slug", "-"))
    t.add_row("credentials", str(settings.credentials_path()))
    t.add_row("webhook secret", "set" if settings.github_webhook_secret else "[red]missing[/red]")
    t.add_row("incremental", "on" if settings.app_incremental else "off")
    t.add_row("debounce", f"{settings.app_debounce_s:.0f}s")
    t.add_row("forks", "reviewed" if settings.app_review_forks else "skipped")
    console.print(Panel(t, title="github app", border_style="dim", title_align="left"))

    installs = store.installations()
    if installs:
        it = Table("installation", "account", "repos", "state")
        for i in installs:
            it.add_row(
                str(i.id),
                i.account,
                str(len(i.repos or [])),
                "suspended" if i.suspended else "active",
            )
        console.print(it)
    else:
        console.print("[dim]No installations recorded yet.[/dim]")

    jobs = store.recent_jobs(10)
    if jobs:
        jt = Table("job", "kind", "key", "status")
        for j in jobs:
            colour = {"failed": "red", "done": "green", "superseded": "dim"}.get(j.status, "")
            status = f"[{colour}]{j.status}[/{colour}]" if colour else j.status
            jt.add_row(str(j.id), j.kind, j.key, status)
        console.print(jt)


@app_cli.command("doctor")
def app_doctor() -> None:
    """Check the App's setup on GitHub and whether its webhooks get through."""
    from cr.app.auth import AppAuth, AuthError
    from cr.app.diagnose import diagnose
    from cr.app.manifest import apply_credentials, load_credentials

    if not settings.app_configured():
        apply_credentials(load_credentials(settings.credentials_path()) or {}, settings)

    async def go():
        auth = AppAuth.from_settings(settings)
        try:
            return await diagnose(auth, recorded_installations=len(store.installations()))
        finally:
            await auth.aclose()

    try:
        report = asyncio.run(go())
    except (AuthError, OSError) as e:
        console.print(f"[red]x[/red] could not reach GitHub as the App: {e}")
        raise typer.Exit(1) from e

    console.print(f"[bold]{report.app}[/bold]  webhooks -> {report.hook_url or '-'}")
    t = Table(show_header=False, box=None, padding=(0, 2))
    for c in report.checks:
        t.add_row("[green]ok[/green]" if c.ok else "[red]!![/red]", c.title, c.detail)
    console.print(t)
    for f in report.failures[:10]:
        console.print(
            f"  [dim]{f.get('delivered_at')}[/dim]  {f.get('event')} {f.get('action') or ''}"
            f"  -> {f.get('status_code') or 'no response'}"
        )
    if report.failures:
        console.print(
            "[dim]Fix what the failures point at, then redeliver them from the App's "
            "settings (Advanced > Recent Deliveries).[/dim]"
        )
    if not report.healthy:
        raise typer.Exit(1)


@app_cli.command("worker")
def app_worker(
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Consume review jobs from Azure Service Bus. No HTTP, no ingress.

    The other half of `cr app serve --role web`: that one accepts webhooks and
    writes jobs, this one runs them. Splitting them is what lets the ingress
    stay small and always-on while the expensive half scales to zero between
    pull requests.
    """
    _setup_logging(verbose)
    logging.getLogger("cr").setLevel(logging.DEBUG if verbose else logging.INFO)

    from cr.app.service import AppService, check_ready

    if not settings.servicebus_connection:
        console.print(
            "[red]CR_SERVICEBUS_CONNECTION is not set.[/red] A worker with no shared "
            "queue has nothing to consume; run [bold]cr app serve[/bold] instead."
        )
        raise typer.Exit(2)

    for problem in check_ready():
        console.print(f"[yellow]![/yellow] {problem}")

    settings.app_role = "worker"
    service = AppService(settings)

    async def run() -> None:
        await service.start()
        console.print(
            f"[green]worker ready[/green] [dim]consuming {settings.servicebus_queue}[/dim]"
        )
        try:
            # Nothing to serve; the queue's consumer task is the whole process.
            while True:
                await asyncio.sleep(3600)
        finally:
            await service.stop()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        console.print("[dim]worker stopped[/dim]")


@app_cli.command("accounts")
def app_accounts(
    status: Annotated[str | None, typer.Option("--status", help="pending/approved/denied")] = None,
) -> None:
    """Who is allowed to have their pull requests reviewed."""
    store.init()
    rows = store.accounts(status)
    if not rows:
        console.print("[dim]No accounts recorded yet.[/dim]")
        return
    t = Table("account", "status", "type", "blocked", "decided by", "requested by")
    colour = {"approved": "green", "denied": "red", "pending": "yellow"}
    for a in rows:
        c = colour.get(a.status, "")
        t.add_row(
            a.login,
            f"[{c}]{a.status}[/{c}]" if c else a.status,
            a.account_type or "-",
            str(a.blocked_events or 0),
            a.decided_by or "-",
            a.requested_by or "-",
        )
    console.print(t)


@app_cli.command("approve")
def app_approve(
    login: Annotated[str | None, typer.Argument(help="GitHub account to approve")] = None,
    all_installed: Annotated[
        bool, typer.Option("--all-installed", help="Approve every account already installed")
    ] = False,
    by: Annotated[str, typer.Option("--by", help="Who is approving, for the audit trail")] = "cli",
) -> None:
    """Allow an account's pull requests to be reviewed.

    `--all-installed` is the upgrade path: turning the allowlist on would
    otherwise silently stop reviewing for everyone already using the App.
    """
    store.init()
    if all_installed:
        logins = {i.account for i in store.installations() if i.account}
        if not logins:
            console.print("[yellow]No installations recorded.[/yellow]")
            raise typer.Exit(0)
        for name in sorted(logins):
            store.record_account(name, status="approved", decided_by=by)
            console.print(f"[green]approved[/green] {name}")
        raise typer.Exit(0)

    if not login:
        console.print("[red]Give an account, or use --all-installed.[/red]")
        raise typer.Exit(2)
    console.print(f"[green]approved[/green] {login}")
    store.record_account(login, status="approved", decided_by=by)


@app_cli.command("deny")
def app_deny(
    login: Annotated[str, typer.Argument(help="GitHub account to deny")],
    by: Annotated[str, typer.Option("--by")] = "cli",
) -> None:
    """Refuse an account. Its webhooks are dropped before anything is spent."""
    store.init()
    store.record_account(login, status="denied", decided_by=by)
    console.print(f"[red]denied[/red] {login}")


@app_cli.command("admin")
def app_admin(
    login: Annotated[str, typer.Argument(help="GitHub login to make an administrator")],
) -> None:
    """Promote a user who has already signed in at least once."""
    store.init()
    for u in store.users():
        if u.login.lower() == login.lower():
            store.upsert_user(u.id, login=u.login, name=u.name, is_admin=True)
            console.print(f"[green]{u.login} is now an administrator.[/green]")
            return
    console.print(
        f"[yellow]{login} has not signed in yet.[/yellow] Add them to CR_ADMIN_LOGINS "
        "and have them sign in once."
    )
    raise typer.Exit(1)


@app_cli.command("replay")
def app_replay(
    pr: Annotated[str, typer.Option("--pr", help="owner/repo#123")],
    full: Annotated[bool, typer.Option("--full", help="Whole PR, not just the delta")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Run the App's review path on one PR, without a webhook.

    The same code the queue runs, driven by hand — which is how you test the
    App's incremental behaviour without pushing commits to GitHub.
    """
    from cr.app.auth import AppAuth
    from cr.app.runner import run_review_job
    from cr.app.service import check_ready

    _setup_logging(verbose)
    for problem in check_ready():
        console.print(f"[yellow]![/yellow] {problem}")

    ref = _parse_pr_arg(pr)
    if ref is None:
        console.print("[red]Use --pr owner/repo#123[/red]")
        raise typer.Exit(2)

    installation = store.installation_for_repo(ref.slug)
    if installation is None:
        console.print(
            f"[red]No installation recorded for {ref.slug}.[/red] "
            "Start `cr app serve` once so it can sync, or install the App on that repo."
        )
        raise typer.Exit(2)

    async def go():
        auth = AppAuth.from_settings(settings)
        try:
            return await run_review_job(
                {
                    "repo": ref.slug,
                    "pr_number": ref.number,
                    "installation_id": installation,
                    "full_review": full,
                },
                auth,
            )
        finally:
            await auth.aclose()

    outcome = asyncio.run(go())
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_row("tier", outcome.tier or "-")
    t.add_row("scope", "incremental" if outcome.incremental else "full PR")
    t.add_row("posted", str(outcome.posted))
    t.add_row("cost", f"${outcome.cost_usd:.4f}")
    t.add_row("elapsed", f"{outcome.elapsed_s:.0f}s")
    if outcome.skipped:
        t.add_row("skipped", outcome.skipped)
    if outcome.error:
        t.add_row("error", f"[red]{outcome.error}[/red]")
    console.print(
        Panel(t, title=f"{ref.slug}#{ref.number}", border_style="dim", title_align="left")
    )
    if outcome.error:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
