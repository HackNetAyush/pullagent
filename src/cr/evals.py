"""Eval harness.

Without this you tune prompts by vibes, which is how a reviewer ends up at 15%
noise and nobody notices for three months.

A fixture is a real PR plus the defects a human knows are in it. Scoring is
keyword-based per expected bug rather than exact-text matching, because two
correct descriptions of the same off-by-one share almost no wording.

Run-to-run variance is real (we have measured 3/3 and 2/3 on the same input), so
`--runs N` reports the spread instead of one lucky number.
"""

from __future__ import annotations

import asyncio
import json
import logging
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from cr.config import TIERS, Settings, TierConfig
from cr.diff import DiffSet, parse
from cr.github import GitHubPR, PRRef
from cr.llm.client import RATES
from cr.llm.prefix import PRContext, RepoContext
from cr.models import ReviewResult
from cr.review.engine import review as run_review
from cr.triage import triage
from cr.warm import context_for_pr

log = logging.getLogger(__name__)


@dataclass
class Expected:
    id: str
    file: str
    match: list[str]
    description: str = ""
    min_matches: int = 2


@dataclass
class Fixture:
    name: str
    pr: str
    expected: list[Expected]
    source: str = ""

    @classmethod
    def load(cls, path: Path) -> Fixture:
        d = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            name=d["name"],
            pr=d["pr"],
            source=d.get("source", ""),
            expected=[Expected(**e) for e in d["expected"]],
        )

    @property
    def ref(self) -> PRRef:
        owner_repo, number = self.pr.split("#")
        owner, repo = owner_repo.split("/")
        return PRRef(owner, repo, int(number))


@dataclass
class RunScore:
    found: set[str] = field(default_factory=set)
    posted: int = 0
    extra: list[str] = field(default_factory=list)
    cost: float = 0.0
    elapsed: float = 0.0
    cache_ratio: float = 0.0

    def recall(self, total: int) -> float:
        return len(self.found) / total if total else 0.0

    @property
    def precision(self) -> float:
        """Share of posted comments that matched a known bug.

        Caveat worth stating: an unmatched finding is not necessarily wrong — it
        may be a real defect nobody labelled. Treat this as a lower bound.
        """
        return len(self.found) / self.posted if self.posted else 0.0


def score_run(fixture: Fixture, result: ReviewResult) -> RunScore:
    s = RunScore(posted=len(result.posted))
    matched_findings: set[int] = set()

    for exp in fixture.expected:
        needles = [m.lower() for m in exp.match]
        for i, vf in enumerate(result.posted):
            f = vf.finding
            if exp.file and exp.file not in f.anchor_file:
                continue
            blob = f"{f.claim} {f.failure_scenario} {f.suggested_fix or ''}".lower()
            if sum(1 for n in needles if n in blob) >= exp.min_matches:
                s.found.add(exp.id)
                # Mark every match, not just the first. Two findings describing
                # the same bug are a duplication problem, not unlabelled findings,
                # and counting them as extras understates precision.
                matched_findings.add(i)

    for i, vf in enumerate(result.posted):
        if i not in matched_findings:
            s.extra.append(vf.finding.claim[:90])

    u = result.usage
    s.cache_ratio = u.cache_hit_ratio
    s.elapsed = result.elapsed_s
    return s


async def _one_run(
    fixture: Fixture, tier: TierConfig, settings: Settings, *, use_graph: bool
) -> ReviewResult:
    ref = fixture.ref
    token = settings.github_token
    with GitHubPR(ref, token or "") as gh:
        meta = gh.metadata()
        diffset = DiffSet(files=parse(gh.diff()))

    decision = triage(diffset)
    reviewable = {f.path for f in decision.reviewable}

    slice_text = ""
    if use_graph:
        slice_text = context_for_pr(
            ref.slug, ref.number, meta["head"]["sha"], meta["base"]["sha"], reviewable, token
        )

    pr_ctx = PRContext(
        title=meta.get("title") or "",
        description=(meta.get("body") or "")[:4000],
        diff=DiffSet(files=[f for f in diffset.files if f.path in reviewable]).render(),
        graph_slice=slice_text,
    )
    # Suppression memory is deliberately off: an eval must measure the reviewer,
    # not whatever a human happened to dismiss on this PR earlier.
    return await run_review(
        repo=RepoContext(slug=ref.slug), pr=pr_ctx, tier=tier, remember=False
    )


def evaluate(
    fixture: Fixture,
    settings: Settings,
    *,
    tier_name: str = "T2",
    runs: int = 1,
    use_graph: bool = True,
) -> list[RunScore]:
    tier = TIERS[tier_name]
    scores: list[RunScore] = []
    for i in range(runs):
        log.info("run %d/%d", i + 1, runs)
        result = asyncio.run(_one_run(fixture, tier, settings, use_graph=use_graph))
        s = score_run(fixture, result)
        s.cost = result.usage.cost_usd(*RATES.get(tier.model, (3.0, 15.0)))
        scores.append(s)
    return scores


def summarise(fixture: Fixture, scores: list[RunScore]) -> dict:
    total = len(fixture.expected)
    recalls = [s.recall(total) for s in scores]
    ever = set().union(*(s.found for s in scores)) if scores else set()
    always = set.intersection(*(s.found for s in scores)) if scores else set()
    return {
        "expected": total,
        "runs": len(scores),
        "recall_mean": statistics.mean(recalls) if recalls else 0.0,
        "recall_min": min(recalls) if recalls else 0.0,
        "recall_max": max(recalls) if recalls else 0.0,
        "found_every_run": sorted(always),
        "found_at_least_once": sorted(ever),
        "never_found": sorted({e.id for e in fixture.expected} - ever),
        "precision_mean": statistics.mean([s.precision for s in scores]) if scores else 0.0,
        "posted_mean": statistics.mean([s.posted for s in scores]) if scores else 0.0,
        "cost_mean": statistics.mean([s.cost for s in scores]) if scores else 0.0,
        "cache_ratio_mean": statistics.mean([s.cache_ratio for s in scores]) if scores else 0.0,
    }


def load_all(directory: Path) -> list[Fixture]:
    return [Fixture.load(p) for p in sorted(directory.glob("*.json"))]
