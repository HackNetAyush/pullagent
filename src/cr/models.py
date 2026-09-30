"""The finding schema — the product thesis expressed as types.

Everything here is enforced by the Anthropic API via structured outputs, not by
parsing JSON out of prose. If a model cannot fill these fields, the finding does
not exist.
"""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class Category(StrEnum):
    CORRECTNESS = "correctness"
    SECURITY = "security"
    CONCURRENCY = "concurrency"
    API_CONTRACT = "api_contract"
    PERFORMANCE = "performance"
    TEST_COVERAGE = "test_coverage"


class Severity(StrEnum):
    CRITICAL = "critical"  # data loss, security hole, production outage
    HIGH = "high"  # wrong behaviour on a reachable path
    MEDIUM = "medium"  # wrong behaviour on an edge case
    LOW = "low"  # works, but will bite later


class Evidence(BaseModel):
    """A specific place in the code that supports the claim.

    A finding without evidence is an opinion. We do not post opinions.
    """

    file: str = Field(description="Repo-relative path")
    start_line: int = Field(description="1-indexed first line of the cited span")
    end_line: int = Field(description="1-indexed last line of the cited span")
    quote: str = Field(
        description=(
            "The text of line start_line, copied character-for-character from the diff "
            "with the line number and +/- marker removed. This is checked against the "
            "diff: if it is not there, the finding loses its inline anchor."
        )
    )
    why: str = Field(description="What this span proves about the claim, in one sentence")


class Finding(BaseModel):
    """A candidate defect. Must carry a proof obligation (D2).

    `failure_scenario` is the load-bearing field: concrete inputs or state that
    produce a wrong result. If the model cannot write one, the finding is dropped
    before it ever reaches a verifier.
    """

    claim: str = Field(description="One sentence stating the defect. No hedging.")
    failure_scenario: str = Field(
        description=(
            "Concrete inputs or state that trigger this, and the wrong output or crash "
            "that results. Must be specific enough that a reader could write the test."
        )
    )
    evidence: list[Evidence] = Field(min_length=1, description="Code spans supporting the claim")
    category: Category
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0, description="Your calibrated confidence, 0-1")
    suggested_fix: str | None = Field(
        default=None, description="Minimal diff-style fix, or null if the fix is not obvious"
    )

    # Populated by our pipeline, never by the model.
    found_by: str = ""
    # 0 = ungrouped. Positive ids mark a confirmed duplicate cluster from
    # merge_colocated (see engine.py) — never sent to an LLM (excluded from
    # every model_dump() payload, same as found_by), and never used to change
    # the finding's own identity: fingerprint() always hashes the original
    # claim, so suppression memory and the review cache stay stable across a
    # merge decision.
    merge_group: int = 0

    @property
    def anchor_file(self) -> str:
        return self.evidence[0].file

    @property
    def anchor_line(self) -> int:
        return self.evidence[0].start_line

    def locality(self) -> tuple[str, int, str]:
        """Coarse identity: same file, same neighbourhood, same category.

        Two lenses describing one defect produce different prose and therefore
        different fingerprints, so fingerprinting alone lets both through. This
        is what actually collapses them.
        """
        return (self.anchor_file, self.anchor_line // 5, str(self.category))

    def fingerprint(self) -> str:
        """Stable identity across runs, for dedup and suppression memory (D5).

        Ported in spirit from pr-agent's `inline_comment_dedup.py`: normalise
        aggressively so a model restating the same defect in different prose still
        collides.
        """
        norm = re.sub(r"\s+", " ", self.claim.lower().strip())[:120]
        raw = f"{self.anchor_file}:{self.anchor_line}:{self.category}:{norm}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]


class FindingList(BaseModel):
    """Top-level structured output for a finder pass."""

    findings: list[Finding] = Field(default_factory=list)


def _requires_paired_corrections(status: str, claim: str | None, scenario: str | None) -> None:
    if status == "repairable" and not (claim and scenario):
        raise ValueError("repairable requires both corrected_claim and corrected_failure_scenario")


class Verdict(BaseModel):
    """A refutation attempt (D3). Defaults are deliberately hostile to the finding."""

    refuted: bool = Field(
        description="True if the finding is wrong, unreachable, or unprovable from the evidence"
    )
    reasoning: str = Field(description="Why. Cite the code that settles it.")
    corrected_severity: Severity | None = Field(
        default=None, description="If real but mis-rated, the correct severity"
    )
    corrected_claim: str | None = Field(
        default=None,
        description=(
            "If the defect is real but the claim overstates the failure mode, the accurate, "
            "narrower claim. Must not describe a different root cause than the original."
        ),
    )
    corrected_failure_scenario: str | None = Field(
        default=None,
        description="The accurate failure scenario matching corrected_claim. Required together.",
    )

    # Populated by our pipeline.
    lens: str = ""
    uncertain: bool = False
    infrastructure_error: bool = False

    @model_validator(mode="after")
    def _check_paired_corrections(self) -> Verdict:
        if (self.corrected_claim is None) != (self.corrected_failure_scenario is None):
            raise ValueError("corrected_claim and corrected_failure_scenario must be set together")
        return self


class BatchDecision(BaseModel):
    finding_id: int
    status: Literal["confirmed", "repairable", "refuted", "uncertain"] = Field(
        description=(
            "confirmed: real, accurately described. repairable: real, but the claim or "
            "failure_scenario overstates the failure mode — narrow it via corrected_claim/"
            "corrected_failure_scenario rather than refuting a genuine defect. refuted: no "
            "real defect, unreachable, or unsupported by the cited evidence. uncertain: "
            "essential context is absent."
        )
    )
    reasoning: str = Field(description="Cite the code and concrete trigger or counterexample.")
    corrected_severity: Severity | None = None
    corrected_claim: str | None = Field(
        default=None,
        description=(
            "Required with corrected_failure_scenario when status='repairable'. The accurate, "
            "narrower claim — must not introduce a new root cause, trigger, evidence location, "
            "or fix relative to the original finding."
        ),
    )
    corrected_failure_scenario: str | None = Field(
        default=None, description="Required with corrected_claim when status='repairable'."
    )

    @model_validator(mode="after")
    def _check_repairable(self) -> BatchDecision:
        _requires_paired_corrections(
            self.status, self.corrected_claim, self.corrected_failure_scenario
        )
        return self


class VerificationBatch(BaseModel):
    decisions: list[BatchDecision]


class MergePairDecision(BaseModel):
    left_id: int
    right_id: int
    same_defect: bool = Field(
        description=(
            "True only if left_id and right_id share the same root cause, the same "
            "triggering condition, and the same fix — not merely the same file or lines."
        )
    )
    reasoning: str


class MergeBatch(BaseModel):
    decisions: list[MergePairDecision]


class FilteredFinding(BaseModel):
    finding: Finding
    reason: str


class VerifiedFinding(BaseModel):
    finding: Finding
    verdicts: list[Verdict]

    @property
    def survived(self) -> bool:
        """Majority-refute kills it. A tie kills it too — we default to silence."""
        if not self.verdicts:
            return False
        if any(v.infrastructure_error for v in self.verdicts):
            return False
        adjudicated = [v for v in self.verdicts if v.lens == "adjudicate"]
        if adjudicated:
            return not adjudicated[-1].refuted and not adjudicated[-1].uncertain
        refuted = sum(1 for v in self.verdicts if v.refuted)
        return refuted * 2 < len(self.verdicts)

    def _correction(self, field: str) -> str | None:
        """Deterministic correction lookup.

        An adjudicate verdict, when present, is authoritative — it already
        resolved a disagreement between the initial lenses, including a
        disagreement between conflicting corrections (see engine.verify's
        `disputed` check), so it must not be outvoted by an earlier lens's
        verdict. Without an adjudicate verdict, initial-lens corrections
        never conflict (conflicting ones are what triggers adjudication in
        the first place), so picking among them in list order is safe.
        """
        adjudicated = [v for v in self.verdicts if v.lens == "adjudicate"]
        pool = adjudicated if adjudicated else [v for v in self.verdicts if not v.refuted]
        for v in reversed(pool):
            value = getattr(v, field)
            if value:
                return value
        return None

    @property
    def final_severity(self) -> Severity:
        return self._correction("corrected_severity") or self.finding.severity

    @property
    def final_claim(self) -> str:
        return self._correction("corrected_claim") or self.finding.claim

    @property
    def final_failure_scenario(self) -> str:
        return self._correction("corrected_failure_scenario") or self.finding.failure_scenario

    @property
    def rank(self) -> float:
        """Ordering for the comment budget: confidence x severity weight."""
        weights = {
            Severity.CRITICAL: 4.0,
            Severity.HIGH: 3.0,
            Severity.MEDIUM: 2.0,
            Severity.LOW: 1.0,
        }
        return self.finding.confidence * weights[self.final_severity]


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    # Subset of cache_creation_input_tokens; one-hour writes cost 2x, not 1.25x.
    cache_creation_1h_input_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens
            + other.cache_creation_input_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
            cache_creation_1h_input_tokens=self.cache_creation_1h_input_tokens
            + other.cache_creation_1h_input_tokens,
        )

    @property
    def cache_hit_ratio(self) -> float:
        """Health metric. If this is near 0 across runs, a silent invalidator shipped."""
        total = self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens
        return self.cache_read_input_tokens / total if total else 0.0

    def cost_usd(
        self,
        in_rate: float,
        out_rate: float,
        *,
        cache_write_multiplier: float = 1.25,
        cache_read_multiplier: float = 0.10,
    ) -> float:
        """Rates are $/1M tokens. Multipliers default to Anthropic's cache
        economics (writes 1.25x, reads 0.1x) — callers pricing a provider with
        different (or unconfirmed) cache pricing pass different multipliers;
        see `client.CACHE_MULTIPLIERS`."""
        return (
            self.input_tokens * in_rate
            + self.cache_creation_input_tokens * in_rate * cache_write_multiplier
            + self.cache_creation_1h_input_tokens * in_rate * (2.0 - cache_write_multiplier)
            + self.cache_read_input_tokens * in_rate * cache_read_multiplier
            + self.output_tokens * out_rate
        ) / 1_000_000


class CallTrace(BaseModel):
    label: str
    model: str
    usage: Usage = Field(default_factory=Usage)
    cost_usd: float = 0.0
    elapsed_s: float = 0.0
    error: str = ""


class ReviewResult(BaseModel):
    tier: str
    posted: list[VerifiedFinding] = Field(default_factory=list)
    # Everything not posted, for display. The three reasons below are disjoint
    # and must stay separate: lumping them together makes the kill rate mean
    # "anything we did not show you", which is a different metric entirely.
    suppressed: list[VerifiedFinding] = Field(default_factory=list)
    refuted: list[VerifiedFinding] = Field(default_factory=list)
    memory_suppressed: list[VerifiedFinding] = Field(default_factory=list)
    budget_trimmed: list[VerifiedFinding] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    # Priced per-model (see LLMClient.total_cost_usd) — usage.cost_usd(rate) alone
    # is wrong whenever a run mixes models, e.g. T3/T4's separate verifier model.
    cost_usd: float = 0.0
    elapsed_s: float = 0.0
    raw_findings: list[Finding] = Field(default_factory=list)
    # Proof-obligation drops only (missing scenario/evidence, low confidence).
    prefiltered: list[FilteredFinding] = Field(default_factory=list)
    # Every dedup mechanism (exact fingerprint, text-similarity, LLM merge) —
    # kept separate from `prefiltered` so stage attribution doesn't conflate
    # "never met the proof obligation" with "collapsed into another finding".
    deduplicated: list[FilteredFinding] = Field(default_factory=list)
    calls: list[CallTrace] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    cache_hit: bool = False
    cached_cost_usd: float = 0.0
    review_key: str = ""
    # Set only when record=True. Lets a caller outside engine.review() (the
    # benchmark's judge-scoring loop runs after this returns) attribute its own
    # extra cost back to the same ledger row instead of that spend vanishing.
    run_id: int | None = None

    @property
    def verified_count(self) -> int:
        """Findings the verifier actually judged — the denominator for kill rate.

        Budget trims are NOT included: those were confirmed real and dropped for
        space, so counting them as kills understates the verifier's pass rate.
        """
        return len(self.posted) + len(self.refuted) + len(self.budget_trimmed)

    @property
    def verifier_kill_rate(self) -> float:
        """Share of judged findings the verifier refuted.

        Below 0.4 suggests rubber-stamping; above 0.6 suggests noisy finders.
        Both readings need a meaningful sample — see MIN_KILL_RATE_SAMPLE.
        """
        total = self.verified_count
        return len(self.refuted) / total if total else 0.0


class ReplyDraft(BaseModel):
    """A response to a human who replied to one of our review comments.

    `verdict` is not decoration. `withdrawn` writes a permanent suppression for
    that finding on that repo, so the model conceding in prose and the system
    actually learning are the same action — there is no way to write "you're
    right, my mistake" and still have the finding come back next week.
    """

    reply: str = Field(
        description=(
            "The reply, as GitHub-flavoured markdown. Address what they actually said. "
            "No greeting, no sign-off, no restating their question back at them."
        )
    )
    verdict: Literal["stands", "withdrawn", "answered", "needs_human"] = Field(
        description=(
            "stands: they disagreed but the defect is still real and you can say why. "
            "withdrawn: they showed the finding was wrong — concede it. "
            "answered: they asked something and you answered; no claim was at stake. "
            "needs_human: answering would need code or context you were not shown."
        )
    )
    reason: str = Field(
        default="",
        description="One sentence of internal rationale for the verdict. Not posted.",
    )
