"""Tier routing (ARCHITECTURE.md §3.2).

Cheapest stage, highest ROI. Runs on metadata alone — no model call, no clone.
T0 exits here with zero spend, which is why dependabot traffic costs nothing.

Routing keys off hunk and file counts taken straight from the diff, deliberately
NOT token counts: routing must not depend on which model's tokenizer you would
have used. (Lifted from pr-agent's `algo/model_routing.py`.)
"""

from __future__ import annotations

from dataclasses import dataclass

from cr.config import TIERS, Settings, TierConfig
from cr.config import settings as default_settings
from cr.diff import DiffSet, FileDiff


@dataclass
class TriageResult:
    tier: str
    config: TierConfig | None
    reason: str
    reviewable: list[FileDiff]
    skipped: list[FileDiff]

    @property
    def is_skip(self) -> bool:
        return self.tier == "T0"


def _is_skippable(path: str, patterns: tuple[str, ...]) -> bool:
    p = path.replace("\\", "/").lower()
    return any(pat.lower() in p for pat in patterns)


def _is_sensitive(path: str, patterns: tuple[str, ...]) -> bool:
    p = path.replace("\\", "/").lower()
    return any(pat in p for pat in patterns)


def triage(diff: DiffSet, cfg: Settings | None = None) -> TriageResult:
    s = cfg or default_settings

    reviewable: list[FileDiff] = []
    skipped: list[FileDiff] = []
    for f in diff.files:
        if _is_skippable(f.path, s.skip_patterns) or f.is_deleted:
            skipped.append(f)
        else:
            reviewable.append(f)

    if not reviewable:
        return TriageResult(
            tier="T0",
            config=None,
            reason="no reviewable files (lockfiles, generated, vendored, or deletions only)",
            reviewable=[],
            skipped=skipped,
        )

    hunks = sum(f.hunks for f in reviewable)
    n_files = len(reviewable)
    sensitive = [f.path for f in reviewable if _is_sensitive(f.path, s.sensitive_patterns)]

    if sensitive:
        return TriageResult(
            tier="T3",
            config=TIERS["T3"],
            reason=f"touches sensitive path(s): {', '.join(sorted(sensitive)[:3])}",
            reviewable=reviewable,
            skipped=skipped,
        )

    if hunks >= s.t3_min_hunks:
        return TriageResult(
            tier="T3",
            config=TIERS["T3"],
            reason=f"large change: {hunks} hunks across {n_files} files",
            reviewable=reviewable,
            skipped=skipped,
        )

    if hunks <= s.t1_max_hunks and n_files <= s.t1_max_files:
        return TriageResult(
            tier="T1",
            config=TIERS["T1"],
            reason=f"small change: {hunks} hunks in {n_files} files",
            reviewable=reviewable,
            skipped=skipped,
        )

    return TriageResult(
        tier="T2",
        config=TIERS["T2"],
        reason=f"standard change: {hunks} hunks across {n_files} files",
        reviewable=reviewable,
        skipped=skipped,
    )
