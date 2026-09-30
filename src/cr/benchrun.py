"""Run a mined benchmark: review each PR at the reviewed commit, judge agreement."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from cr.bench import JUDGE_PREAMBLE, Match, judge_prompt
from cr.config import TIERS, Settings, TierConfig
from cr.diff import DiffSet, parse
from cr.llm.client import LLMClient, build_pool
from cr.llm.prefix import PRContext, RepoContext
from cr.review.engine import review as run_review
from cr.triage import triage
from cr.warm import context_for_pr

log = logging.getLogger(__name__)
API = "https://api.github.com"


@dataclass
class PRResult:
    pr: str
    title: str
    expected: int = 0
    matched: int = 0
    posted: int = 0
    cost: float = 0.0
    tier: str = ""
    skipped: str = ""
    details: list[dict[str, Any]] = field(default_factory=list)

    @property
    def recall(self) -> float:
        return self.matched / self.expected if self.expected else 0.0


def compare_diff(slug: str, base: str, head: str, token: str) -> str:
    """Diff exactly the range the human reviewed, not the merged result."""
    owner, repo = slug.split("/")
    with httpx.Client(timeout=60.0, follow_redirects=True) as c:
        r = c.get(
            f"{API}/repos/{owner}/{repo}/compare/{base}...{head}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.v3.diff",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "cr-bench",
            },
        )
        r.raise_for_status()
        return r.text


async def _judge(
    llm: LLMClient, model: str, human: dict[str, Any], findings: list[dict[str, Any]]
) -> Match | None:
    call = await llm.parse(
        model=model,
        schema=Match,
        system=[{"type": "text", "text": JUDGE_PREAMBLE}],
        messages=[
            {
                "role": "user",
                "content": [{"type": "text", "text": judge_prompt(human, findings)}],
            }
        ],
        effort="low",
        max_tokens=1200,
        label="judge",
    )
    return call.parsed if isinstance(call.parsed, Match) else None


async def run_pr(
    entry: dict[str, Any],
    slug: str,
    tier: TierConfig,
    settings: Settings,
    *,
    use_graph: bool,
    max_diff_chars: int,
) -> PRResult:
    token = settings.github_token or ""
    res = PRResult(pr=entry["pr"], title=entry.get("title", ""), tier=tier.name)
    humans = entry.get("human_reviews", [])
    res.expected = len(humans)

    try:
        raw = compare_diff(slug, entry["base_sha"], entry["head_sha"], token)
    except Exception as e:  # noqa: BLE001
        res.skipped = f"diff unavailable: {e}"
        return res

    files = parse(raw)
    if not files:
        res.skipped = "empty or unparseable diff"
        return res

    diffset = DiffSet(files=files, base=entry["base_sha"], head=entry["head_sha"])
    decision = triage(diffset)
    if decision.is_skip:
        res.skipped = f"T0: {decision.reason}"
        return res

    reviewable = {f.path for f in decision.reviewable}
    body = DiffSet(files=[f for f in diffset.files if f.path in reviewable]).render()

    number = int(entry["pr"].split("#")[1])
    slice_text = ""
    if use_graph:
        slice_text = context_for_pr(
            slug, number, entry["head_sha"], entry["base_sha"], reviewable, token
        )

    pr_ctx = PRContext(
        title=entry.get("title") or entry["pr"],
        description="",
        diff=body,
        graph_slice=slice_text,
    )

    result = await run_review(
        repo=RepoContext(slug=slug),
        pr=pr_ctx,
        tier=tier,
        remember=False,
        source="bench",
        head_sha=entry["head_sha"],
        cfg=settings.model_copy(update={"finder_chunk_chars": max_diff_chars}),
    )
    res.posted = len(result.posted)
    res.cost = result.cost_usd
    if result.errors:
        res.skipped = "incomplete review: " + "; ".join(result.errors)
        return res

    findings = [
        {
            "file": v.finding.anchor_file,
            "line": v.finding.anchor_line,
            "claim": v.final_claim,
            "failure_scenario": v.final_failure_scenario,
        }
        for v in result.posted
    ]

    # The judge always runs on a fixed Claude model, independent of tier.model:
    # it has to stay Anthropic-shaped regardless of which finder model is under
    # test (T4's isn't), and judging shouldn't use the same model being graded.
    judge_model = settings.model_standard
    judge_llm = LLMClient(pool=build_pool(settings), max_concurrency=4)
    verdicts = await asyncio.gather(
        *(_judge(judge_llm, judge_model, h, findings) for h in humans),
        return_exceptions=True,
    )
    for h, m in zip(humans, verdicts, strict=False):
        ok = isinstance(m, Match) and m.same_concern
        if ok:
            res.matched += 1
        res.details.append(
            {
                "id": h.get("id"),
                "file": h.get("file"),
                "human": (h.get("comment") or "")[:180],
                "matched": ok,
                "why": m.reasoning[:180] if isinstance(m, Match) else str(m)[:180],
                "url": h.get("url", ""),
            }
        )
    res.cost += judge_llm.cost_usd(judge_model)
    return res


def run_benchmark(
    fixture_path: Path,
    settings: Settings,
    *,
    tier_name: str = "T2",
    limit: int | None = None,
    use_graph: bool = True,
    max_diff_chars: int = 60_000,
) -> tuple[dict[str, Any], list[PRResult]]:
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    slug = data["repo"]
    tier = TIERS[tier_name]
    entries = data["prs"][:limit] if limit else data["prs"]

    async def _all() -> list[PRResult]:
        out: list[PRResult] = []
        for i, entry in enumerate(entries, 1):
            log.info("[%d/%d] %s", i, len(entries), entry["pr"])
            out.append(
                await run_pr(
                    entry,
                    slug,
                    tier,
                    settings,
                    use_graph=use_graph,
                    max_diff_chars=max_diff_chars,
                )
            )
        return out

    results: list[PRResult] = asyncio.run(_all())

    scored = [r for r in results if not r.skipped]
    expected = sum(r.expected for r in scored)
    matched = sum(r.matched for r in scored)
    posted = sum(r.posted for r in scored)
    return (
        {
            "repo": slug,
            "prs_attempted": len(results),
            "prs_scored": len(scored),
            "skipped": len(results) - len(scored),
            "human_comments": expected,
            "matched": matched,
            "recall": matched / expected if expected else 0.0,
            "posted": posted,
            "precision": matched / posted if posted else 0.0,
            "cost": sum(r.cost for r in results),
        },
        results,
    )
