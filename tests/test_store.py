"""Suppression memory — the durable half of learning from merged PRs."""

from __future__ import annotations

import pytest

from cr.config import TIERS
from cr.models import Category, Evidence, Finding, Severity, Verdict, VerifiedFinding
from cr.review.engine import gate
from cr.store import db as store


@pytest.fixture
def url(tmp_path):
    store.reset_for_tests()
    yield f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    store.reset_for_tests()


def _vf(claim: str = "off-by-one", line: int = 10) -> VerifiedFinding:
    return VerifiedFinding(
        finding=Finding(
            claim=claim,
            failure_scenario="An empty list makes the index wrap to the last element.",
            evidence=[Evidence(file="a.py", start_line=line, end_line=line, quote="", why="bound")],
            category=Category.CORRECTNESS,
            severity=Severity.HIGH,
            confidence=0.9,
        ),
        verdicts=[Verdict(refuted=False, reasoning="confirmed")],
    )


def test_suppression_round_trips(url) -> None:
    vf = _vf()
    fp = vf.finding.fingerprint()
    assert store.suppress("acme/api", fp, reason="resolved", url=url) is True
    assert store.suppressed_fingerprints("acme/api", url) == {fp}


def test_suppression_is_idempotent(url) -> None:
    fp = _vf().finding.fingerprint()
    assert store.suppress("acme/api", fp, reason="resolved", url=url) is True
    assert store.suppress("acme/api", fp, reason="thumbs_down", url=url) is False


def test_suppression_is_scoped_per_repo(url) -> None:
    """One team rejecting a finding must not silence it for another."""
    fp = _vf().finding.fingerprint()
    store.suppress("acme/api", fp, reason="resolved", url=url)
    assert store.suppressed_fingerprints("other/repo", url) == set()


def test_gate_drops_suppressed_findings() -> None:
    vf = _vf()
    fp = vf.finding.fingerprint()

    assert len(gate([vf], TIERS["T2"], set()).posted) == 1

    g = gate([vf], TIERS["T2"], {fp})
    assert g.posted == []
    # Memory suppression is its own bucket — it must not inflate the kill rate.
    assert len(g.memory_suppressed) == 1
    assert g.refuted == []


def test_suppression_does_not_hide_a_different_finding() -> None:
    """Fingerprints are per claim+location; suppressing one must not blanket a file."""
    a, b = _vf("off-by-one", 10), _vf("null deref", 40)
    g = gate([a, b], TIERS["T2"], {a.finding.fingerprint()})
    assert [v.finding.claim for v in g.posted] == ["null deref"]


def test_stats_counts_runs_and_suppressions(url) -> None:
    from cr.models import ReviewResult, Usage

    store.suppress("acme/api", "abc123abc123", reason="resolved", url=url)
    store.record_run(
        "acme/api",
        ReviewResult(tier="T2", posted=[_vf()], suppressed=[], usage=Usage(input_tokens=10)),
        model="claude-sonnet-5",
        cost=0.25,
        url=url,
    )
    d = store.stats("acme/api", url)
    assert d["runs"] == 1
    assert d["posted"] == 1
    assert d["suppressions"] == 1
    assert d["cost_usd"] == pytest.approx(0.25)


def test_store_failure_never_breaks_a_review() -> None:
    """Bookkeeping must degrade, not crash — an unreachable DB is not a review error."""
    store.reset_for_tests()
    assert store.suppressed_fingerprints("acme/api", "postgresql://nobody@127.0.0.1:1/x") == set()
    store.reset_for_tests()


def test_every_source_lands_in_the_ledger(url) -> None:
    """The ledger lives in the engine so no command can forget to record. This
    guards the regression where only `review-pr` wrote and local reviews, evals
    and benchmarks were invisible — a 15x undercount of real spend."""
    from cr.models import ReviewResult, Usage

    for src in ("pr", "local", "eval", "bench"):
        rid = store.start_run("acme/api", tier="T2", model="m", source=src, url=url)
        assert rid is not None
        store.finish_run(
            rid, ReviewResult(tier="T2", usage=Usage(output_tokens=100)), cost=0.10, url=url
        )

    d = store.stats("acme/api", url)
    assert d["runs"] == 4
    assert d["cost_usd"] == pytest.approx(0.40)


def test_actor_column_exists_for_auth(url) -> None:
    """Reserved now so adding auth is not a schema migration."""
    from cr.store.models import Run

    rid = store.start_run("acme/api", tier="T1", model="m", actor="user:42", url=url)
    with store.session(url) as s:
        assert s.get(Run, rid).actor == "user:42"


def test_cache_hit_replay_records_the_original_cost(url) -> None:
    """A cache-hit review legitimately costs $0 / 0 tokens. Without recording
    cache_hit and cached_cost_usd the ledger cannot tell that apart from a
    review that simply found nothing — both would render identically."""
    from cr.models import ReviewResult, Usage
    from cr.store.models import Run

    rid = store.start_run("acme/api", tier="T2", model="m", url=url)
    store.finish_run(
        rid,
        ReviewResult(tier="T2", usage=Usage(), cache_hit=True, cached_cost_usd=0.93),
        cost=0.0,
        url=url,
    )
    with store.session(url) as s:
        run = s.get(Run, rid)
        assert run.cache_hit is True
        assert run.cached_cost_usd == pytest.approx(0.93)
        assert run.cost_usd == 0.0
