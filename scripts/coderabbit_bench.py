"""Compare `cr` against CodeRabbit on withmartian/code-review-benchmark's
golden-comment dataset: https://github.com/withmartian/code-review-benchmark

Read-only against GitHub — fetches PR diff/metadata only, never posts a
comment or review. Safe to run against the real upstream PRs (they're already
merged, and this never calls GitHubPR.submit_review or posted_fingerprints).

CodeRabbit's numbers are NOT re-generated here: the benchmark already ran
CodeRabbit on all 50 PRs and judged the results (offline/results/<model>/
evaluations.json). We read those directly. `cr`'s own findings are judged with
`cr`'s existing Claude-Sonnet-5 Match judge (see cr.bench) rather than the
benchmark's own Martian-gateway judge, which needs a separate paid API key
this project isn't configured with. Same methodology (semantic "same
concern?" judging), same precision/recall formula `cr bench` already reports
elsewhere (precision = matched-golden-comments / total-findings-posted, a
lower bound since an unmatched finding may be a real, unlabelled defect).

Usage:
    uv run python scripts/coderabbit_bench.py --benchmark-repo <path-to-clone> [--tier T2]

Requires the benchmark repo cloned locally (not vendored into this project):
    git clone https://github.com/withmartian/code-review-benchmark
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from cr.bench import JUDGE_PREAMBLE, Match, judge_prompt
from cr.config import TIERS, settings
from cr.diff import DiffSet, parse
from cr.github import GitHubPR, PRRef
from cr.llm.client import LLMClient, build_pool
from cr.llm.prefix import PRContext, RepoContext
from cr.review.engine import review as run_review
from cr.triage import triage

log = logging.getLogger(__name__)

# Picked from the benchmark's 50-PR set: real upstream URL (not a fork), has a
# coderabbit review already collected there, non-trivial golden-comment count.
# Discourse's golden comments key off a bare commit SHA rather than a PR
# number, so it needs different handling and is left out of this pass.
PICKS: list[tuple[str, int, str]] = [
    ("keycloak/keycloak", 37429, "keycloak"),
    ("getsentry/sentry", 93824, "sentry"),
    ("grafana/grafana", 79265, "grafana"),
    ("calcom/cal.com", 11059, "cal_dot_com"),
]

JUDGE_MODEL_DIR = "anthropic_claude-sonnet-4-5-20250929"


def _golden_comments(benchmark_repo: Path, repo_key: str, pr_url: str) -> list[dict[str, Any]]:
    path = benchmark_repo / "offline" / "golden_comments" / f"{repo_key}.json"
    entries = json.loads(path.read_text(encoding="utf-8"))
    for e in entries:
        if e["url"] == pr_url:
            return e["comments"]
    raise ValueError(f"{pr_url} not found in {path}")


def _coderabbit_eval(benchmark_repo: Path, pr_url: str) -> dict[str, Any] | None:
    path = benchmark_repo / "offline" / "results" / JUDGE_MODEL_DIR / "evaluations.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get(pr_url, {}).get("coderabbit")


async def _review(slug: str, number: int, tier_name: str, token: str) -> tuple[list[dict], float]:
    """Fetch diff + metadata, run cr's engine, return findings + cost. No writes."""
    owner, repo = slug.split("/")
    tier = TIERS[tier_name]
    with GitHubPR(PRRef(owner, repo, number), token) as gh:
        meta = gh.metadata()
        diffset = DiffSet(files=parse(gh.diff()), base=meta["base"]["sha"], head=meta["head"]["sha"])

    decision = triage(diffset)
    reviewable = {f.path for f in decision.reviewable}
    pr_ctx = PRContext(
        title=meta.get("title") or f"PR #{number}",
        description=(meta.get("body") or "")[:4000],
        diff=DiffSet(files=[f for f in diffset.files if f.path in reviewable]).render(),
    )
    result = await run_review(
        repo=RepoContext(slug=slug),
        pr=pr_ctx,
        tier=tier,
        remember=False,
        record=False,
        source="eval",
    )
    findings = [
        {
            "file": v.finding.anchor_file,
            "line": v.finding.anchor_line,
            "claim": v.finding.claim,
            "failure_scenario": v.finding.failure_scenario,
        }
        for v in result.posted
    ]
    return findings, result.cost_usd


async def _judge_against_golden(
    judge_llm: LLMClient, judge_model: str, golden: list[dict[str, Any]], findings: list[dict]
) -> int:
    """How many golden comments does at least one cr finding match? (-> recall numerator)"""

    async def one(g: dict[str, Any]) -> bool:
        call = await judge_llm.parse(
            model=judge_model,
            schema=Match,
            system=[{"type": "text", "text": JUDGE_PREAMBLE}],
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": judge_prompt({"file": None, "line": None, "comment": g["comment"]}, findings),
                        }
                    ],
                }
            ],
            effort="low",
            max_tokens=1200,
            label="judge",
        )
        return isinstance(call.parsed, Match) and call.parsed.same_concern

    results = await asyncio.gather(*(one(g) for g in golden))
    return sum(results)


async def main(benchmark_repo: Path, tier_name: str) -> None:
    token = settings.github_token
    if not token:
        raise SystemExit("GITHUB_TOKEN is not set — add it to .env")

    judge_llm = LLMClient(pool=build_pool(settings), max_concurrency=4)
    rows: list[dict[str, Any]] = []
    out_path = Path(__file__).with_name("coderabbit_bench_results.jsonl")

    header = (
        f"{'PR':<28} {'golden':>6} {'cr posted':>9} {'cr recall':>9} {'cr prec':>8}"
        f" {'crab recall':>11} {'crab prec':>9}"
    )
    print(header)

    for slug, number, repo_key in PICKS:
        pr_url = f"https://github.com/{slug}/pull/{number}"
        log.info("reviewing %s#%d", slug, number)

        # Each PR is independent real money + a real network call to someone
        # else's repo — one failure (rename, rate limit, transient API error)
        # must not discard the PRs that already succeeded before it.
        try:
            findings, cost = await _review(slug, number, tier_name, token)
            golden = _golden_comments(benchmark_repo, repo_key, pr_url)
            matched = await _judge_against_golden(judge_llm, settings.model_standard, golden, findings)
        except Exception as e:  # noqa: BLE001 - report and move on, never abort the batch
            log.error("skipping %s#%d: %s", slug, number, e)
            print(f"{slug + '#' + str(number):<28} FAILED: {e}")
            continue

        row = {
            "pr": f"{slug}#{number}",
            "golden": len(golden),
            "cr_posted": len(findings),
            "cr_recall": matched / len(golden) if golden else 0.0,
            "cr_precision": matched / len(findings) if findings else 0.0,
            "cr_cost": cost,
            "coderabbit": _coderabbit_eval(benchmark_repo, pr_url),
        }
        rows.append(row)
        with out_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")

        crab = row["coderabbit"] or {}
        crab_recall = f"{crab.get('recall', 0):.0%}" if crab else "n/a"
        crab_precision = f"{crab.get('precision', 0):.0%}" if crab else "n/a"
        print(
            f"{row['pr']:<28} {row['golden']:>6} {row['cr_posted']:>9} "
            f"{row['cr_recall']:>9.0%} {row['cr_precision']:>8.0%} "
            f"{crab_recall:>11} {crab_precision:>9}"
        )

    total_cost = sum(r["cr_cost"] for r in rows) + judge_llm.total_cost_usd()
    print(f"\ncr judge model: {settings.model_standard} | judged against golden comments directly")
    print(f"coderabbit numbers: published, judged by their {JUDGE_MODEL_DIR}")
    print(f"total cr spend this run: ${total_cost:.3f}")
    print(f"results also written to {out_path}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-repo", type=Path, required=True)
    parser.add_argument("--tier", default="T2")
    args = parser.parse_args()
    asyncio.run(main(args.benchmark_repo, args.tier))
