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

from cr import evals as ev
from cr.config import TIERS, settings
from cr.diff import DiffSet, collect, parse
from cr.doctor import run as doctor_run
from cr.github import MARKER, GitHubPR, PRRef, build_review, pr_from_env
from cr.lint import analyse
from cr.llm.client import RATES
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


def _render(result: ReviewResult, model: str) -> None:
    if not result.posted:
        console.print("\n[green]No findings survived verification.[/green]")
    for v in result.posted:
        f = v.finding
        sev = v.final_severity
        style = SEVERITY_STYLE[sev]
        body = [
            f"[{style}]{sev.upper()}[/{style}]  [dim]{f.category} · "
            f"confidence {f.confidence:.0%} · found by {f.found_by}[/dim]\n",
            f"[bold]{f.claim}[/bold]\n",
            f"[dim]How it breaks:[/dim] {f.failure_scenario}\n",
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

    if result.suppressed:
        console.print(f"\n[dim]{len(result.suppressed)} finding(s) killed by verification.[/dim]")

    u = result.usage
    in_rate, out_rate = RATES.get(model, (3.00, 15.00))
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
    t.add_row("cost", f"${u.cost_usd(in_rate, out_rate):.4f}")
    t.add_row("elapsed", f"{result.elapsed_s:.1f}s")
    console.print(Panel(t, title="run", border_style="dim", title_align="left"))

    if u.cache_read_input_tokens == 0 and u.cache_creation_input_tokens > 0:
        console.print(
            "[dim]cold cache — this run populated it; the next run on this repo "
            "reads at 0.1x.[/dim]"
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
    tier: Annotated[str | None, typer.Option("--tier", "-t", help="Force T1/T2/T3")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Review uncommitted changes, or `base`..HEAD."""
    _setup_logging(verbose)

    diff = collect(str(repo), base)
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
        description="Uncommitted working-tree changes." if not base else f"Changes since {base}.",
        diff=DiffSet(files=filtered, base=diff.base, head=diff.head).render(),
    )
    _require_credentials()

    result = asyncio.run(
        run_review(repo=RepoContext(slug=repo.resolve().name), pr=pr, tier=cfg)
    )
    _render(result, cfg.model)


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
        console.print(
            "[red]GITHUB_TOKEN is not set.[/red] Add it to .env or export it."
        )
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
        slice_text = "" if no_graph else context_for_pr(
            ref.slug,
            ref.number,
            meta["head"]["sha"],
            meta["base"]["sha"],
            keep,
            token,
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

        run_id = store.start_run(
            ref.slug,
            tier=cfg.name,
            model=cfg.model,
            pr_number=ref.number,
            head_sha=meta["head"]["sha"],
        )
        try:
            result = asyncio.run(
                run_review(
                    repo=RepoContext(slug=ref.slug),
                    pr=pr_ctx,
                    tier=cfg,
                    on_stage=lambda st: store.set_stage(run_id, st),
                )
            )
        except Exception as exc:
            store.finish_run(run_id, ReviewResult(tier=cfg.name), cost=0.0, error=str(exc))
            raise
        _cost = result.usage.cost_usd(*RATES.get(cfg.model, (3.0, 15.0)))
        _render(result, cfg.model)

        already = set() if dry_run else gh.posted_fingerprints()
        body, comments = build_review(
            result.posted,
            commentable=diffset.commentable_map(),
            already=already,
            tier=result.tier,
            cost=result.usage.cost_usd(*RATES.get(cfg.model, (3.0, 15.0))),
            elapsed=result.elapsed_s,
            killed=len(result.suppressed),
        )

        if dry_run:
            store.finish_run(run_id, result, cost=_cost)
            console.print("\n[yellow]--dry-run: not posting.[/yellow]\n")
            console.print(body)
            for c in comments:
                console.print(f"  inline -> {c['path']}:{c['line']}")
            raise typer.Exit(0)

        gh.submit_review(body, comments, meta["head"]["sha"])
        console.print(f"[green]Posted {len(comments)} inline comment(s).[/green]")
        store.finish_run(run_id, result, cost=_cost)


@app.command()
def doctor(
    model: Annotated[str | None, typer.Option("--model", "-m")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Probe the provider: reachability, structured outputs, and prompt caching.

    Costs a fraction of a cent. Run this before your first review — on Foundry,
    caching is a beta capability, so whether it actually works is a real question
    and every cost estimate depends on the answer.
    """
    _setup_logging(verbose)
    target = model or TIERS["T2"].model
    console.print(f"[dim]provider={settings.provider} model={target}[/dim]\n")

    report = doctor_run(settings, target)

    def mark(ok: bool) -> str:
        return "[green]yes[/green]" if ok else "[red]no[/red]"

    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_row("reachable", mark(report.reachable))
    t.add_row("structured outputs", mark(report.structured_outputs))
    t.add_row("effort accepted", mark(report.effort_accepted))
    t.add_row("prompt caching", mark(report.caching_works))
    t.add_row("cache write / read", f"{report.cache_write_tokens:,} / {report.cache_read_tokens:,}")
    t.add_row("probe cost", f"${report.cost_usd:.5f}")
    console.print(Panel(t, title="doctor", border_style="dim", title_align="left"))

    for err in report.errors:
        console.print(f"[red]error:[/red] {err}")

    if report.healthy and not report.caching_works:
        console.print(
            "\n[yellow]Caching is not taking effect.[/yellow] It still works, but input "
            "costs will be roughly 5x the figures in the docs. Check that prompt caching "
            "is enabled for this deployment."
        )
    if not report.healthy:
        raise typer.Exit(1)
    console.print("\n[green]Ready.[/green]")


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
    console.print(
        "\n[green]Warm.[/green] Reviews on this repo now include "
        "cross-file context."
    )


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


@app.command()
def tiers() -> None:
    """Show the routing table."""
    t = Table("tier", "model", "effort", "finders", "verifiers", "max comments")
    for name, c in TIERS.items():
        t.add_row(
            name,
            c.model,
            c.effort,
            str(len(c.finders)),
            str(len(c.verifier_lenses)),
            str(c.max_comments),
        )
    console.print(t)


if __name__ == "__main__":
    app()
