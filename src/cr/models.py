"""The finding schema — the product thesis expressed as types.

Everything here is enforced by the Anthropic API via structured outputs, not by
parsing JSON out of prose. If a model cannot fill these fields, the finding does
not exist.
"""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum

from pydantic import BaseModel, Field


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

    @property
    def anchor_file(self) -> str:
        return self.evidence[0].file

    @property
    def anchor_line(self) -> int:
        return self.evidence[0].start_line

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


class Verdict(BaseModel):
    """A refutation attempt (D3). Defaults are deliberately hostile to the finding."""

    refuted: bool = Field(
        description="True if the finding is wrong, unreachable, or unprovable from the evidence"
    )
    reasoning: str = Field(description="Why. Cite the code that settles it.")
    corrected_severity: Severity | None = Field(
        default=None, description="If real but mis-rated, the correct severity"
    )

    # Populated by our pipeline.
    lens: str = ""


class VerifiedFinding(BaseModel):
    finding: Finding
    verdicts: list[Verdict]

    @property
    def survived(self) -> bool:
        """Majority-refute kills it. A tie kills it too — we default to silence."""
        if not self.verdicts:
            return False
        refuted = sum(1 for v in self.verdicts if v.refuted)
        return refuted * 2 < len(self.verdicts)

    @property
    def final_severity(self) -> Severity:
        for v in self.verdicts:
            if not v.refuted and v.corrected_severity:
                return v.corrected_severity
        return self.finding.severity

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

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens
            + other.cache_creation_input_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
        )

    @property
    def cache_hit_ratio(self) -> float:
        """Health metric. If this is near 0 across runs, a silent invalidator shipped."""
        total = self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens
        return self.cache_read_input_tokens / total if total else 0.0

    def cost_usd(self, in_rate: float, out_rate: float) -> float:
        """Rates are $/1M tokens. Cache writes bill at 1.25x, reads at 0.1x."""
        return (
            self.input_tokens * in_rate
            + self.cache_creation_input_tokens * in_rate * 1.25
            + self.cache_read_input_tokens * in_rate * 0.10
            + self.output_tokens * out_rate
        ) / 1_000_000


class ReviewResult(BaseModel):
    tier: str
    posted: list[VerifiedFinding] = Field(default_factory=list)
    suppressed: list[VerifiedFinding] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    elapsed_s: float = 0.0

    @property
    def verifier_kill_rate(self) -> float:
        """Watch this from day one. Below 0.4 the verifier is rubber-stamping;
        above 0.6 the finders are too noisy. It tells you which half to fix."""
        total = len(self.posted) + len(self.suppressed)
        return len(self.suppressed) / total if total else 0.0
