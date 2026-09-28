"""Tests for triage and the precision gate — the product thesis as assertions."""

from __future__ import annotations

from cr.config import TIERS, settings
from cr.diff import DiffSet, FileDiff
from cr.models import Category, Evidence, Finding, Severity, Verdict, VerifiedFinding
from cr.review.engine import gate, prefilter
from cr.triage import triage


def _finding(
    claim: str = "off-by-one in the loop bound",
    *,
    confidence: float = 0.8,
    scenario: str = "With items of length 0 the loop indexes items[-1] and wraps.",
    severity: Severity = Severity.HIGH,
    file: str = "a.py",
    line: int = 10,
) -> Finding:
    return Finding(
        claim=claim,
        failure_scenario=scenario,
        evidence=[Evidence(file=file, start_line=line, end_line=line + 2, why="the bound")],
        category=Category.CORRECTNESS,
        severity=severity,
        confidence=confidence,
    )


def _hunk_patch(n: int) -> str:
    return "".join(f"@@ -{i} +{i} @@\n+x\n" for i in range(1, n + 1))


# --- triage -----------------------------------------------------------------


def test_lockfile_only_change_is_free() -> None:
    d = DiffSet(files=[FileDiff(path="pnpm-lock.yaml", patch=_hunk_patch(50))])
    r = triage(d)
    assert r.is_skip and r.tier == "T0"


def test_sensitive_path_forces_deep_tier_regardless_of_size() -> None:
    d = DiffSet(files=[FileDiff(path="src/auth/session.py", patch=_hunk_patch(1))])
    assert triage(d).tier == "T3"


def test_small_change_routes_cheap() -> None:
    d = DiffSet(files=[FileDiff(path="src/util.py", patch=_hunk_patch(3))])
    assert triage(d).tier == "T1"


def test_large_change_routes_deep() -> None:
    d = DiffSet(files=[FileDiff(path="src/util.py", patch=_hunk_patch(60))])
    assert triage(d).tier == "T3"


def test_generated_files_are_excluded_but_siblings_still_reviewed() -> None:
    d = DiffSet(
        files=[
            FileDiff(path="api_pb2.py", patch=_hunk_patch(20)),
            FileDiff(path="src/handler.py", patch=_hunk_patch(4)),
        ]
    )
    r = triage(d)
    assert not r.is_skip
    assert [f.path for f in r.reviewable] == ["src/handler.py"]


# --- prefilter (D2: proof obligation) ---------------------------------------


def test_finding_without_a_real_failure_scenario_is_dropped() -> None:
    kept, dropped = prefilter([_finding(scenario="looks wrong")], settings)
    assert kept == [] and len(dropped) == 1


def test_low_confidence_is_dropped_before_paying_to_verify() -> None:
    kept, _ = prefilter([_finding(confidence=0.1)], settings)
    assert kept == []


def test_same_defect_from_two_lenses_is_verified_once() -> None:
    a = _finding(confidence=0.6)
    b = _finding(confidence=0.9)  # same claim/file/line -> same fingerprint
    kept, dropped = prefilter([a, b], settings)
    assert len(kept) == 1
    assert kept[0].confidence == 0.9  # highest confidence wins
    assert len(dropped) == 1


# --- gate (D3: adversarial verification) ------------------------------------


def _verified(refuted: list[bool], **kw) -> VerifiedFinding:
    return VerifiedFinding(
        finding=_finding(**kw),
        verdicts=[Verdict(refuted=r, reasoning="x") for r in refuted],
    )


def test_majority_refute_kills_the_finding() -> None:
    assert not _verified([True, True, False]).survived


def test_tie_kills_the_finding() -> None:
    """We default to silence. A tie is not good enough to spend a developer's trust."""
    assert not _verified([True, False]).survived


def test_minority_refute_survives() -> None:
    assert _verified([True, False, False]).survived


def test_no_verdicts_never_survives() -> None:
    """A verification stage that failed entirely must not pass findings through."""
    assert not VerifiedFinding(finding=_finding(), verdicts=[]).survived


def test_comment_budget_caps_output_and_keeps_the_best() -> None:
    tier = TIERS["T1"]  # max_comments=3
    items = [
        _verified([False], claim=f"bug {i}", confidence=c, severity=s, line=i)
        for i, (c, s) in enumerate(
            [
                (0.5, Severity.LOW),
                (0.9, Severity.CRITICAL),
                (0.6, Severity.MEDIUM),
                (0.95, Severity.HIGH),
                (0.4, Severity.LOW),
            ]
        )
    ]
    posted, suppressed = gate(items, tier)
    assert len(posted) == 3
    assert len(suppressed) == 2
    # Ranked by confidence x severity weight, best first.
    assert posted[0].finding.claim == "bug 1"  # 0.9 * critical
    assert posted[1].finding.claim == "bug 3"  # 0.95 * high


def test_killed_findings_are_reported_as_suppressed_not_dropped() -> None:
    posted, suppressed = gate([_verified([True, True])], TIERS["T2"])
    assert posted == []
    assert len(suppressed) == 1


def test_paraphrases_of_one_defect_collapse() -> None:
    """Two lenses describing the same bug produce different fingerprints, so
    fingerprint dedup alone lets both through and the PR gets two comments."""
    a = _finding("Send button disabled state uses draft.length", confidence=0.6, line=66)
    b = _finding(
        "The send button's disabled state is computed from draft.length===0",
        confidence=0.9,
        line=67,
    )
    kept, dropped = prefilter([a, b], settings)
    assert len(kept) == 1
    assert kept[0].confidence == 0.9
    assert len(dropped) == 1


def test_distinct_defects_in_one_file_both_survive() -> None:
    """Locality dedup must not merge genuinely different bugs."""
    a = _finding("off-by-one", line=10)
    b = _finding("null deref", line=200)
    kept, _ = prefilter([a, b], settings)
    assert len(kept) == 2
