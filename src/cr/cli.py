"""`cr review` — the local dev loop.

Iterate on prompts here, never by pushing to GitHub.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from cr.config import TIERS, settings
from cr.diff import collect
from cr.llm.client import RATES
from cr.llm.prefix import PRContext, RepoContext
from cr.models import ReviewResult, Severity
from cr.review.engine import review as run_review
from cr.triage import triage

app = typer.Typer(add_completion=False, help="High-precision AI code review")
console = Console()

SEVERITY_STYLE = {
    Severity.CRITICAL: "bold red",
    Severity.HIGH: "red",
    Severity.MEDIUM: "yellow",
    Severity.LOW: "dim",
}


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

    if u.cache_hit_ratio < 0.2 and u.cache_creation_input_tokens > 0:
        console.print(
            "[yellow]warning:[/yellow] cache hit ratio is low — check for a silent "
            "invalidator, or a prefix below the model minimum. See PIPELINE.md §2.2."
        )


@app.command()
def review(
    repo: Annotated[Path, typer.Argument(help="Repo to review")] = Path("."),
    base: Annotated[str | None, typer.Option("--base", "-b", help="Base ref, e.g. main")] = None,
    tier: Annotated[str | None, typer.Option("--tier", "-t", help="Force T1/T2/T3")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Review uncommitted changes, or `base`..HEAD."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    diff = collect(str(repo), base)
    if not diff.files:
        console.print("[yellow]No changes to review.[/yellow]")
        raise typer.Exit(0)

    decision = triage(diff)
    console.print(
        f"[dim]{diff.total_files} files, {diff.total_hunks} hunks → "
        f"[bold]{decision.tier}[/bold] ({decision.reason})[/dim]"
    )

    if decision.is_skip and not tier:
        console.print("[green]Skipped — no review needed. $0.00 spent.[/green]")
        raise typer.Exit(0)

    cfg = TIERS[tier.upper()] if tier else decision.config
    assert cfg is not None
    console.print(f"[dim]model={cfg.model} effort={cfg.effort} finders={len(cfg.finders)}[/dim]\n")

    reviewable = {f.path for f in decision.reviewable}
    filtered = [f for f in diff.files if f.path in reviewable]

    pr = PRContext(
        title=f"Local changes in {repo.resolve().name}",
        description="Uncommitted working-tree changes." if not base else f"Changes since {base}.",
        diff=type(diff)(files=filtered, base=diff.base, head=diff.head).render(),
    )
    repo_ctx = RepoContext(slug=repo.resolve().name)

    if not settings.anthropic_api_key:
        console.print(
            "[yellow]No CR_ANTHROPIC_API_KEY set — falling back to ANTHROPIC_API_KEY "
            "from the environment.[/yellow]"
        )

    result = asyncio.run(run_review(repo=repo_ctx, pr=pr, tier=cfg))
    _render(result, cfg.model)


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
