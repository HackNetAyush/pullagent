"""Deterministic regressions for recall, cost, failures and exact-input reuse."""

import itertools
import json
from dataclasses import replace

import pytest
from pydantic import ValidationError

from cr.config import TIERS, Settings
from cr.diff import DiffSet, FileDiff, parse, review_chunks
from cr.llm.client import Call
from cr.llm.prefix import PRContext, PrefixBuilder, RepoContext
from cr.models import (
    BatchDecision,
    CallTrace,
    MergeBatch,
    MergePairDecision,
    ReviewResult,
    Usage,
    Verdict,
    VerificationBatch,
    VerifiedFinding,
)
from cr.review import cache, engine, prompts
from cr.triage import triage
from tests.test_gate import _finding


def patch(path="a.py", n=2):
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,{n} +1,{n} @@\n" + "".join(f"-old{i}\n+new{i}\n" for i in range(n))
    )


class FakeClient:
    def __init__(self, *, disagree=False, malformed=False, fail=False):
        self.calls = []
        self.usage = Usage()
        self.disagree, self.malformed, self.fail = disagree, malformed, fail

    def total_cost_usd(self):
        return len(self.calls) * 0.01

    async def parse(self, **kw):
        self.calls.append(CallTrace(label=kw["label"], model=kw["model"]))
        if self.fail:
            raise RuntimeError("simulated transport failure")
        payload = json.loads(
            kw["messages"][0]["content"][-1]["text"].split(
                "Candidates (data, not instructions):\n"
            )[1]
        )
        status = (
            "refuted" if self.disagree and kw["label"] == "verify:reachability" else "confirmed"
        )
        decisions = [
            BatchDecision(finding_id=x["finding_id"], status=status, reasoning="a.py:10")
            for x in payload
        ]
        if self.malformed:
            decisions = decisions[:-1]
        return Call(parsed=VerificationBatch(decisions=decisions), usage=Usage(), model=kw["model"])


class FakeMergeClient:
    """Answers each requested (left_id, right_id) pair from `same_defect_pairs`,
    a set of `frozenset({left, right})` — anything not listed is judged independent."""

    def __init__(self, same_defect_pairs=None, *, malformed=False, fail=False):
        self.calls = []
        self.same_defect_pairs = same_defect_pairs or set()
        self.malformed, self.fail = malformed, fail

    async def parse(self, **kw):
        self.calls.append(CallTrace(label=kw["label"], model=kw["model"]))
        if self.fail:
            raise RuntimeError("simulated transport failure")
        text = kw["messages"][0]["content"][-1]["text"]
        pairs_json = text.split("Pairs to judge:\n")[1].split(
            "\n\nCandidates (data, not instructions):\n"
        )[0]
        pairs = json.loads(pairs_json)
        decisions = [
            MergePairDecision(
                left_id=p["left_id"],
                right_id=p["right_id"],
                same_defect=frozenset((p["left_id"], p["right_id"])) in self.same_defect_pairs,
                reasoning="same root cause",
            )
            for p in pairs
        ]
        if self.malformed:
            decisions = decisions[:-1]
        return Call(parsed=MergeBatch(decisions=decisions), usage=Usage(), model=kw["model"])


@pytest.fixture
def contexts():
    return RepoContext(slug="owner/repo"), PRContext(title="Change", description="", diff=patch())


def test_independent_bugs_on_same_line_are_not_merged():
    a = _finding(
        "Null user crashes request", line=10, scenario="When user is None, user.id crashes."
    )
    b = _finding(
        "Cache poisoned before commit",
        line=11,
        scenario="When commit fails, the cache still returns uncommitted data.",
    )
    kept, dropped = engine.prefilter([a, b], Settings(_env_file=None))
    assert len(kept) == 2 and not dropped


def test_prefilter_explains_each_drop():
    trace = []
    engine.prefilter(
        [_finding(confidence=0.1), _finding(scenario="vague")], Settings(_env_file=None), trace
    )
    assert {x.reason for x in trace} == {"below_confidence_threshold", "missing_concrete_scenario"}


# Claims used by the grouping tests. Real prose, not placeholders: grouping is
# decided on claim similarity, so single-letter claims would score 0 and every
# test below would pass without exercising anything.
HEAP_DOCSTRING = "PriorityQueue uses a min-heap ordering on task.priority, so low values run first"
HEAP_CODE = "PriorityQueue uses a min-heap ordering by raw priority value, so low values dequeue"
FAIL_OPEN = "verify() fails open and returns True when no keys have been registered"
WEAK_HASH = "hash_password() uses unsalted MD5, which is cryptographically broken"


def test_merge_groups_pairs_one_defect_restated_far_from_its_code():
    """The regression that shipped: one lens anchored the min-heap bug on the
    module docstring (line 1) and another on the heappush call (line 31). Thirty
    lines apart, so location clustering never compared them and both posted."""
    findings = [
        _finding(claim=HEAP_DOCSTRING, file="queue.py", line=1),
        _finding(claim=HEAP_CODE, file="queue.py", line=31),
    ]
    assert engine._merge_groups(findings) == [[0, 1]]


def test_merge_groups_keeps_adjacent_but_unrelated_findings_apart():
    """The other half of the regression: proximity alone chained 18 independent
    findings on one file into a single cluster, which then blew the size cap and
    skipped merge entirely. Adjacent lines must not imply one defect."""
    findings = [
        _finding(claim=FAIL_OPEN, file="auth.py", line=19),
        _finding(claim=WEAK_HASH, file="auth.py", line=21),
    ]
    assert engine._merge_groups(findings) == []


def test_merge_groups_never_spans_files():
    findings = [
        _finding(claim=FAIL_OPEN, file="auth.py", line=19),
        _finding(claim=FAIL_OPEN, file="other.py", line=19),
    ]
    assert engine._merge_groups(findings) == []


def test_merge_groups_trims_an_oversized_group_to_its_best_members():
    """A pile-up degrades to judging the most confident members. It must never
    go back to skipping the group outright — that is what let four copies of one
    bug through while crowding a real one off the comment budget."""
    findings = [
        _finding(claim=FAIL_OPEN, file="auth.py", line=19 + i, confidence=0.4 + i / 100)
        for i in range(engine.MAX_MERGE_GROUP + 4)
    ]
    groups = engine._merge_groups(findings)
    assert len(groups) == 1
    assert len(groups[0]) == engine.MAX_MERGE_GROUP
    # The four least confident are the ones left independent.
    assert set(groups[0]) == set(range(4, engine.MAX_MERGE_GROUP + 4))


async def test_merge_colocated_tags_llm_confirmed_duplicate_without_deleting(contexts):
    """Two lenses paraphrasing one bug at the same location: same_defect()'s exact-text
    check would never merge these (different wording), but the merge stage should tag
    them as one group — without deleting either, so both still reach verify()."""
    repo, pr = contexts
    a = _finding(claim="join breaks early", file="flusher.py", line=336, confidence=0.85)
    b = _finding(claim="join stops terminating", file="flusher.py", line=337, confidence=0.72)
    client = FakeMergeClient({frozenset((0, 1))})
    result = await engine.merge_colocated(
        client, PrefixBuilder(prompts.PREAMBLE, repo, pr), TIERS["T2"], [a, b]
    )
    assert result == [a, b]  # nothing deleted
    assert a.merge_group != 0
    assert a.merge_group == b.merge_group
    assert len(client.calls) == 1


async def test_merge_colocated_keeps_independent_findings_at_same_location(contexts):
    repo, pr = contexts
    a = _finding(claim="missing validation", file="flusher.py", line=51)
    b = _finding(claim="unrelated race condition", file="flusher.py", line=52)
    client = FakeMergeClient(set())
    result = await engine.merge_colocated(
        client, PrefixBuilder(prompts.PREAMBLE, repo, pr), TIERS["T2"], [a, b]
    )
    assert result == [a, b]
    assert a.merge_group == 0
    assert b.merge_group == 0


async def test_merge_colocated_skips_the_call_when_nothing_shares_a_location(contexts):
    repo, pr = contexts
    a = _finding(claim="a", file="x.py", line=10)
    b = _finding(claim="b", file="y.py", line=10)
    client = FakeMergeClient()
    result = await engine.merge_colocated(
        client, PrefixBuilder(prompts.PREAMBLE, repo, pr), TIERS["T2"], [a, b]
    )
    assert result == [a, b]
    assert client.calls == []


@pytest.mark.parametrize("kwargs", [{"malformed": True}, {"fail": True}])
async def test_merge_colocated_fails_open_on_bad_response(contexts, kwargs):
    """A wrong/incomplete merge response — including for a plain two-member
    cluster — must never tag a group; equivalence has to be established, not
    assumed, even when only one pair is involved."""
    repo, pr = contexts
    a = _finding(claim=FAIL_OPEN, file="auth.py", line=19)
    b = _finding(claim=FAIL_OPEN + " for every caller", file="auth.py", line=20)
    client = FakeMergeClient(**kwargs)
    result = await engine.merge_colocated(
        client, PrefixBuilder(prompts.PREAMBLE, repo, pr), TIERS["T2"], [a, b]
    )
    assert result == [a, b]
    assert a.merge_group == 0
    assert b.merge_group == 0


async def test_merge_colocated_merges_a_confirmed_three_way_clique(contexts):
    repo, pr = contexts
    a = _finding(claim=FAIL_OPEN, file="auth.py", line=19)
    b = _finding(claim=FAIL_OPEN + " for every caller", file="auth.py", line=20)
    c = _finding(claim=FAIL_OPEN + " on an empty key set", file="auth.py", line=21)
    client = FakeMergeClient({frozenset((0, 1)), frozenset((0, 2)), frozenset((1, 2))})
    result = await engine.merge_colocated(
        client, PrefixBuilder(prompts.PREAMBLE, repo, pr), TIERS["T2"], [a, b, c]
    )
    assert result == [a, b, c]
    assert a.merge_group == b.merge_group == c.merge_group != 0


async def test_merge_colocated_refuses_to_bridge_independent_defects_through_a_composite_finding(
    contexts,
):
    """A composite finding that mentions two distinct bugs can make each of them
    look like its duplicate without the two bugs ever being judged against each
    other. Requiring a full pairwise clique (not just a connected star through
    the composite) must refuse to merge the two unrelated bugs together."""
    repo, pr = contexts
    a = _finding(claim=FAIL_OPEN, file="auth.py", line=10)
    b = _finding(claim=FAIL_OPEN + " and the key set is never locked", file="auth.py", line=12)
    composite = _finding(
        claim=FAIL_OPEN + " and the key set is never locked for every caller",
        file="auth.py",
        line=11,
    )
    # a-composite and b-composite confirmed; a-b never judged against each other.
    client = FakeMergeClient({frozenset((0, 2)), frozenset((1, 2))})
    result = await engine.merge_colocated(
        client, PrefixBuilder(prompts.PREAMBLE, repo, pr), TIERS["T2"], [a, b, composite]
    )
    assert result == [a, b, composite]
    assert a.merge_group == 0
    assert b.merge_group == 0
    assert composite.merge_group == 0


async def test_merge_colocated_incomplete_pair_matrix_fails_open_for_a_larger_cluster(contexts):
    repo, pr = contexts
    a = _finding(claim=FAIL_OPEN, file="auth.py", line=19)
    b = _finding(claim=FAIL_OPEN + " for every caller", file="auth.py", line=20)
    c = _finding(claim=FAIL_OPEN + " on an empty key set", file="auth.py", line=21)
    client = FakeMergeClient(
        {frozenset((0, 1)), frozenset((0, 2)), frozenset((1, 2))}, malformed=True
    )
    result = await engine.merge_colocated(
        client, PrefixBuilder(prompts.PREAMBLE, repo, pr), TIERS["T2"], [a, b, c]
    )
    assert result == [a, b, c]
    assert a.merge_group == b.merge_group == c.merge_group == 0


async def test_large_group_is_split_across_calls_not_abandoned(contexts):
    """A group too big for one prompt is batched, not skipped.

    The previous cap did the opposite: more than six co-located findings meant
    no merge call at all, so the densest pile-ups — exactly where duplicates
    live — were the only ones never deduped. On a real PR that shipped one auth
    bug as four comments and pushed a genuine race off the comment budget.
    """
    repo, pr = contexts
    n = 9
    findings = [
        _finding(claim=f"{FAIL_OPEN} via caller number {i}", file="auth.py", line=19 + i)
        for i in range(n)
    ]
    every_pair = {frozenset(p) for p in itertools.combinations(range(n), 2)}
    client = FakeMergeClient(every_pair)

    result = await engine.merge_colocated(
        client, PrefixBuilder(prompts.PREAMBLE, repo, pr), TIERS["T2"], findings
    )

    assert result == findings
    # 36 pairs at 24 per call — two calls, and every pair judged, or the clique
    # check below could not hold.
    assert len(client.calls) == 2
    groups = {f.merge_group for f in findings}
    assert groups != {0} and len(groups) == 1


async def test_group_larger_than_the_member_cap_still_merges_what_it_can(contexts):
    repo, pr = contexts
    n = engine.MAX_MERGE_GROUP + 3
    findings = [
        _finding(
            claim=f"{FAIL_OPEN} via caller number {i}",
            file="auth.py",
            line=19 + i,
            confidence=0.4 + i / 100,
        )
        for i in range(n)
    ]
    client = FakeMergeClient({frozenset(p) for p in itertools.combinations(range(n), 2)})
    await engine.merge_colocated(
        client, PrefixBuilder(prompts.PREAMBLE, repo, pr), TIERS["T2"], findings
    )
    merged = [f for f in findings if f.merge_group]
    assert len(merged) == engine.MAX_MERGE_GROUP
    # The trimmed remainder stays independent rather than vanishing.
    assert len(findings) == n


async def test_merge_batch_failure_does_not_incomplete_the_review(monkeypatch, tmp_path, contexts):
    """A garbled merge-batch response (observed live: a repetition-loop JSON
    truncation) must not trip the same "don't post, don't score" gate as a
    failed finder or verifier — merge_colocated already fails open, so the
    worst case is a little redundancy, not lost or wrongly-passed coverage."""
    monkeypatch.setattr(cache, "default_cache_dir", lambda: tmp_path)
    repo, pr = contexts
    a = _finding(claim="a", file="x.py", line=10, confidence=0.8)
    b = _finding(claim="b", file="x.py", line=11, confidence=0.7)

    async def find(*args):
        return [a, b]

    monkeypatch.setattr(engine, "find", find)

    class MixedClient:
        def __init__(self):
            self.calls = []
            self.usage = Usage()

        def total_cost_usd(self):
            return len(self.calls) * 0.01

        async def parse(self, **kw):
            self.calls.append(CallTrace(label=kw["label"], model=kw["model"]))
            if kw["label"] == "merge":
                raise RuntimeError("simulated malformed merge response")
            payload = json.loads(
                kw["messages"][0]["content"][-1]["text"].split(
                    "Candidates (data, not instructions):\n"
                )[1]
            )
            decisions = [
                BatchDecision(finding_id=x["finding_id"], status="confirmed", reasoning="ok")
                for x in payload
            ]
            return Call(
                parsed=VerificationBatch(decisions=decisions), usage=Usage(), model=kw["model"]
            )

    result = await engine.review(
        repo=repo,
        pr=pr,
        tier=TIERS["T2"],
        client=MixedClient(),
        record=False,
        remember=False,
        head_sha="head",
    )
    assert not result.errors
    assert len(result.posted) == 2


async def test_twelve_candidates_need_four_verifier_calls(contexts):
    repo, pr = contexts
    client = FakeClient()
    result = await engine.verify(
        client,
        PrefixBuilder(prompts.PREAMBLE, repo, pr),
        TIERS["T2"],
        [_finding(claim=f"bug {i}") for i in range(12)],
    )
    assert len(client.calls) == 4  # previously 24
    assert all(v.survived for v in result)


async def test_disagreement_is_adjudicated_not_silently_discarded(contexts):
    repo, pr = contexts
    client = FakeClient(disagree=True)
    result = await engine.verify(
        client, PrefixBuilder(prompts.PREAMBLE, repo, pr), TIERS["T2"], [_finding()]
    )
    assert len(client.calls) == 3
    assert result[0].survived
    assert len(result[0].verdicts) == 3


@pytest.mark.parametrize("kwargs", [{"malformed": True}, {"fail": True}])
async def test_failed_or_incomplete_verifier_never_passes(contexts, kwargs):
    repo, pr = contexts
    result = await engine.verify(
        FakeClient(**kwargs), PrefixBuilder(prompts.PREAMBLE, repo, pr), TIERS["T2"], [_finding()]
    )
    assert not result[0].survived
    assert any(v.infrastructure_error for v in result[0].verdicts)


# --- resolve_merge_groups: verify-before-delete, pick representative after ---


def test_resolve_merge_groups_prefers_the_survivor_over_a_refuted_higher_confidence_one():
    """Every alternative was already verified in merge_colocated's new tag-don't-delete
    contract, so "retry the best discarded alternative" is just picking among verdicts
    already in hand — no extra LLM call needed."""
    a = _finding(claim="high-confidence overclaim", confidence=0.95, line=10)
    b = _finding(claim="accurate lower-confidence version", confidence=0.6, line=11)
    a.merge_group = b.merge_group = 1
    verified = [
        VerifiedFinding(finding=a, verdicts=[Verdict(refuted=True, reasoning="overclaim refuted")]),
        VerifiedFinding(finding=b, verdicts=[Verdict(refuted=False, reasoning="confirmed")]),
    ]
    trace = []
    kept = engine.resolve_merge_groups(verified, trace)
    assert len(kept) == 1
    assert kept[0].finding is b
    assert trace[0].finding is a
    assert trace[0].reason == "duplicate_same_root_cause_llm"


def test_resolve_merge_groups_ties_break_by_original_order():
    a = _finding(claim="a", confidence=0.7, line=10)
    b = _finding(claim="b", confidence=0.7, line=11)
    a.merge_group = b.merge_group = 1
    verified = [
        VerifiedFinding(finding=a, verdicts=[Verdict(refuted=False, reasoning="ok")]),
        VerifiedFinding(finding=b, verdicts=[Verdict(refuted=False, reasoning="ok")]),
    ]
    kept = engine.resolve_merge_groups(verified)
    assert kept == [verified[0]]


def test_resolve_merge_groups_drop_does_not_inflate_verifier_kill_rate():
    a = _finding(claim="a", confidence=0.9, line=10)
    b = _finding(claim="b", confidence=0.5, line=11)
    a.merge_group = b.merge_group = 1
    verified = [
        VerifiedFinding(finding=a, verdicts=[Verdict(refuted=False, reasoning="ok")]),
        VerifiedFinding(finding=b, verdicts=[Verdict(refuted=False, reasoning="ok")]),
    ]
    trace = []
    kept = engine.resolve_merge_groups(verified, trace)
    result = ReviewResult(tier="T2", posted=kept, deduplicated=trace)
    assert result.verified_count == 1  # the dropped duplicate is not a judged candidate
    assert result.verifier_kill_rate == 0.0


async def test_multi_chunk_verification_preserves_merge_group_identity(contexts):
    """Simulates review()'s chunked verify path: two merge-group siblings get
    verified by two SEPARATE verify() calls (as happens when their evidence
    lives in different diff chunks), and the resulting VerifiedFinding lists
    are concatenated the same way `verified.extend(...)` does in review().
    merge_group lives on the Finding object itself, so it must survive
    untouched regardless of which verify() call processed which sibling."""
    repo, pr = contexts
    a = _finding(claim="a", file="x.py", line=10, confidence=0.9)
    b = _finding(claim="b", file="x.py", line=11, confidence=0.6)
    a.merge_group = b.merge_group = 7
    builder = PrefixBuilder(prompts.PREAMBLE, repo, pr)
    verified = []
    verified.extend(await engine.verify(FakeClient(), builder, TIERS["T2"], [a]))
    verified.extend(await engine.verify(FakeClient(), builder, TIERS["T2"], [b]))
    kept = engine.resolve_merge_groups(verified)
    assert len(kept) == 1
    assert kept[0].finding is a  # both survived; higher confidence wins


# --- repairable verdicts: narrow an overclaim instead of only refute/confirm ---


def test_batch_decision_rejects_partial_repairable_correction():
    with pytest.raises(ValidationError):
        BatchDecision(
            finding_id=0, status="repairable", reasoning="x", corrected_claim="only claim"
        )


def test_verdict_rejects_a_lone_corrected_claim_without_a_scenario():
    with pytest.raises(ValidationError):
        Verdict(refuted=False, reasoning="x", corrected_claim="claim only")


class RepairClient:
    """Every lens reports the same correction for the given finding_ids — no
    disagreement, so no adjudication call is expected."""

    def __init__(self, corrections):
        self.calls = []
        self.usage = Usage()
        self.corrections = corrections  # finding_id -> (claim, scenario)

    def total_cost_usd(self):
        return 0.0

    async def parse(self, **kw):
        self.calls.append(CallTrace(label=kw["label"], model=kw["model"]))
        payload = json.loads(
            kw["messages"][0]["content"][-1]["text"].split(
                "Candidates (data, not instructions):\n"
            )[1]
        )
        decisions = []
        for x in payload:
            fid = x["finding_id"]
            if fid in self.corrections:
                claim, scenario = self.corrections[fid]
                decisions.append(
                    BatchDecision(
                        finding_id=fid,
                        status="repairable",
                        reasoning="narrowed",
                        corrected_claim=claim,
                        corrected_failure_scenario=scenario,
                    )
                )
            else:
                decisions.append(BatchDecision(finding_id=fid, status="confirmed", reasoning="ok"))
        return Call(parsed=VerificationBatch(decisions=decisions), usage=Usage(), model=kw["model"])


async def test_repairable_verdict_narrows_an_overclaim_without_mutating_the_finding(contexts):
    repo, pr = contexts
    f = _finding(claim="parsing always fails", scenario="Every call to parse() throws.")
    original_fingerprint = f.fingerprint()
    client = RepairClient(
        {0: ("parsing silently corrupts data", "Malformed input silently drops fields, no throw.")}
    )
    builder = PrefixBuilder(prompts.PREAMBLE, repo, pr)
    result = await engine.verify(client, builder, TIERS["T2"], [f])
    vf = result[0]
    assert vf.survived  # repairable is not refuted — the defect is real
    assert vf.final_claim == "parsing silently corrupts data"
    assert vf.final_failure_scenario == "Malformed input silently drops fields, no throw."
    # The raw finding, and its identity, are untouched by a correction —
    # suppression memory and the review cache must not shift underneath it.
    assert vf.finding.claim == "parsing always fails"
    assert vf.finding.fingerprint() == original_fingerprint


class ConflictingRepairClient:
    """Two lenses propose different corrected claims for the same finding —
    a genuine disagreement that must route through adjudication rather than
    being resolved by verdict-list (i.e. asyncio.gather completion) order."""

    def __init__(self):
        self.calls = []
        self.usage = Usage()

    def total_cost_usd(self):
        return 0.0

    async def parse(self, **kw):
        self.calls.append(CallTrace(label=kw["label"], model=kw["model"]))
        payload = json.loads(
            kw["messages"][0]["content"][-1]["text"].split(
                "Candidates (data, not instructions):\n"
            )[1]
        )
        if kw["label"] == "adjudicate":
            decisions = [
                BatchDecision(
                    finding_id=x["finding_id"],
                    status="repairable",
                    reasoning="adjudicated",
                    corrected_claim="the adjudicated claim",
                    corrected_failure_scenario="the adjudicated scenario",
                )
                for x in payload
            ]
        else:
            claim = "claim A" if kw["label"] == "verify:correctness" else "claim B"
            decisions = [
                BatchDecision(
                    finding_id=x["finding_id"],
                    status="repairable",
                    reasoning="r",
                    corrected_claim=claim,
                    corrected_failure_scenario=f"{claim} scenario",
                )
                for x in payload
            ]
        return Call(parsed=VerificationBatch(decisions=decisions), usage=Usage(), model=kw["model"])


async def test_conflicting_corrections_trigger_adjudication_not_a_race(contexts):
    repo, pr = contexts
    client = ConflictingRepairClient()
    result = await engine.verify(
        client, PrefixBuilder(prompts.PREAMBLE, repo, pr), TIERS["T2"], [_finding()]
    )
    assert any(c.label == "adjudicate" for c in client.calls)
    assert result[0].final_claim == "the adjudicated claim"
    assert result[0].final_failure_scenario == "the adjudicated scenario"


# --- documented, unresolved gap: this change does not fix under-merging ---


@pytest.mark.xfail(
    reason="under-merging gap (Grafana: three duplicate TOCTOU comments), "
    "not addressed by this change — tracked separately",
    strict=True,
)
def test_three_independently_worded_non_colocated_findings_for_one_toctou_still_post_separately():
    a = _finding(
        claim="check-then-act race on the session flag",
        scenario="Thread A reads session.active as True, thread B clears it, A proceeds anyway.",
        file="worker.py",
        line=40,
    )
    b = _finding(
        claim="session flag read without holding the lock that guards its writer",
        scenario="A stale read of session.active lets a second worker start after cleanup began.",
        file="worker.py",
        line=140,
    )
    c = _finding(
        claim="TOCTOU: session state can change between the check and the use",
        scenario="Reading session.active and later acting on it are not atomic, so a concurrent "
        "clear is missed and the worker proceeds on a session that is already gone.",
        file="worker.py",
        line=240,
    )
    kept, _ = engine.prefilter([a, b, c], Settings(_env_file=None))
    assert len(kept) == 1  # not yet true — same_defect()/_cluster_by_location can't see this


async def test_exact_repeat_is_free_and_refresh_is_not(monkeypatch, tmp_path, contexts):
    monkeypatch.setattr(cache, "default_cache_dir", lambda: tmp_path)
    repo, pr = contexts
    count = 0

    async def find(*args):
        nonlocal count
        count += 1
        return [_finding()]

    monkeypatch.setattr(engine, "find", find)
    kw = dict(
        repo=repo,
        pr=pr,
        tier=TIERS["T2"],
        remember=False,
        record=False,
        cfg=Settings(_env_file=None),
        head_sha="head",
    )
    first = await engine.review(client=FakeClient(), **kw)
    second = await engine.review(client=FakeClient(), **kw)
    assert first.posted == second.posted
    assert second.cache_hit and second.cost_usd == 0 and second.usage == Usage()
    assert second.cached_cost_usd == first.cost_usd > 0
    assert count == 1
    await engine.review(client=FakeClient(), use_cache=False, **kw)
    await engine.review(client=FakeClient(), source="eval", **kw)
    assert count == 3


async def test_failures_are_traced_and_not_cached(monkeypatch, tmp_path, contexts):
    monkeypatch.setattr(cache, "default_cache_dir", lambda: tmp_path)

    async def find(*args):
        return [_finding()]

    monkeypatch.setattr(engine, "find", find)
    repo, pr = contexts
    result = await engine.review(
        repo=repo,
        pr=pr,
        tier=TIERS["T2"],
        client=FakeClient(fail=True),
        remember=False,
        record=False,
        head_sha="head",
    )
    assert result.errors and not result.posted
    assert cache.load(result.review_key, 86400) is None


def test_context_policy_and_snapshot_invalidate_cache(contexts):
    repo, pr = contexts
    tier, cfg = TIERS["T2"], Settings(_env_file=None)
    key = cache.review_key(repo, pr, tier, cfg, "head")
    assert key != cache.review_key(repo, replace(pr, graph_slice="new callee"), tier, cfg, "head")
    assert key != cache.review_key(repo, pr, tier, cfg, "other")
    assert key != cache.review_key(
        repo, pr, tier.model_copy(update={"max_comments": 2}), cfg, "head"
    )
    assert key != cache.review_key(
        repo, pr, tier, cfg.model_copy(update={"min_confidence": 0.9}), "head"
    )


def test_expired_corrupt_and_failed_cache_entries_are_misses(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "default_cache_dir", lambda: tmp_path)
    cache.save(ReviewResult(tier="T2", review_key="key"))
    assert cache.load("key", 86400) is not None
    assert cache.load("key", 0) is None
    (tmp_path / "reviews" / "key.json").write_text("broken", encoding="utf-8")
    assert cache.load("key", 86400) is None
    cache.save(ReviewResult(tier="T2", review_key="failed", errors=["failed"]))
    assert cache.load("failed", 86400) is None


def test_corrected_text_survives_cache_round_trip(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "default_cache_dir", lambda: tmp_path)
    f = _finding(claim="parsing always fails")
    vf = VerifiedFinding(
        finding=f,
        verdicts=[
            Verdict(
                refuted=False,
                reasoning="narrowed",
                corrected_claim="parsing silently corrupts data",
                corrected_failure_scenario="Malformed input silently drops fields.",
            )
        ],
    )
    cache.save(ReviewResult(tier="T2", review_key="repaired", posted=[vf]))
    loaded = cache.load("repaired", 86400)
    assert loaded is not None
    reloaded = loaded.posted[0]
    assert reloaded.final_claim == "parsing silently corrupts data"
    assert reloaded.final_failure_scenario == "Malformed input silently drops fields."
    assert reloaded.finding.claim == "parsing always fails"
    assert reloaded.finding.fingerprint() == f.fingerprint()


def test_all_files_and_large_hunk_lines_reach_chunks():
    text = patch("first.py", 80) + patch("last.py", 2)
    chunks = review_chunks(text, 500)
    assert len(chunks) > 1 and all(len(c) <= 500 for c in chunks)
    files = [f for c in chunks for f in parse(c)]
    assert {f.path for f in files} == {"first.py", "last.py"}
    assert set.union(*(f.commentable for f in files if f.path == "first.py")) == set(range(1, 81))
    assert sum(f.added for f in files) == 82
    assert sum(f.removed for f in files) == 82


def test_new_file_chunk_coordinates_and_no_silent_render_truncation():
    f = FileDiff(path="new.py", patch="@@ -0,0 +1,80 @@\n" + "+new\n" * 80, is_new=True)
    d = DiffSet(files=[f])
    chunks = review_chunks(d.render(), 300)
    assert set.union(*(p.commentable for c in chunks for p in parse(c))) == set(range(1, 81))
    with pytest.raises(ValueError):
        d.render(max_chars=20)


def test_deletions_are_reviewed_not_silently_skipped():
    text = "--- a/auth.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-guard()\n-authorize()\n"
    d = DiffSet(files=parse(text))
    assert not triage(d).is_skip
    assert "-authorize()" in d.render()


def test_one_hour_cache_writes_are_not_underpriced():
    usage = Usage(cache_creation_input_tokens=1000, cache_creation_1h_input_tokens=400)
    assert usage.cost_usd(3, 15) == pytest.approx((600 * 1.25 + 400 * 2) * 3 / 1e6)


def test_related_context_includes_callee_body(tmp_path):
    from cr.graph import build, render_slice

    (tmp_path / "api.py").write_text("def response():\n    return {'data': []}\n", encoding="utf-8")
    (tmp_path / "use.py").write_text(
        "from api import response\nresult = response()\n", encoding="utf-8"
    )
    graph = build(tmp_path)
    assert "return {'data': []}" in render_slice(graph, {"use.py"}, [], root=tmp_path)


async def test_cached_result_reapplies_current_suppression(monkeypatch, tmp_path, contexts):
    monkeypatch.setattr(cache, "default_cache_dir", lambda: tmp_path)
    repo, pr = contexts
    finding = _finding()

    async def find(*args):
        return [finding]

    monkeypatch.setattr(engine, "find", find)
    fps = set()
    monkeypatch.setattr(engine.store, "suppressed_fingerprints", lambda slug: fps)
    monkeypatch.setattr(engine.store, "bump_hits", lambda *args: None)
    kw = dict(
        repo=repo,
        pr=pr,
        tier=TIERS["T2"],
        record=False,
        cfg=Settings(_env_file=None),
        head_sha="head",
    )
    first = await engine.review(client=FakeClient(), **kw)
    assert len(first.posted) == 1
    fps.add(finding.fingerprint())
    second = await engine.review(client=FakeClient(), **kw)
    assert second.cache_hit and not second.posted and len(second.memory_suppressed) == 1


def test_graph_reuse_repairs_non_pr_files_and_pins_history(monkeypatch, tmp_path):
    from contextlib import contextmanager

    from cr import graph, warm
    from cr import repo as repo_module

    (tmp_path / "changed.py").write_text("def changed(): return 1\n", encoding="utf-8")
    (tmp_path / "caller.py").write_text("def new_caller(): return changed()\n", encoding="utf-8")
    old = graph.RepoGraph(commit="old", defs={"stale": [["caller.py", 1]]}, refs={}, files=2)
    history_refs = []

    class FakeRepo:
        def __init__(self, root):
            self.root = root

        def ensure(self, *args, **kw):
            return tmp_path

        def has_commit(self, *args):
            return True

        def mirror_path(self, *args):
            return tmp_path

        def co_change(self, *args, ref):
            history_refs.append(ref)
            return []

        @contextmanager
        def worktree(self, *args):
            yield tmp_path

    saved = []
    monkeypatch.setattr(warm, "RepoCache", FakeRepo)
    monkeypatch.setattr(graph, "load", lambda path: old if "base" in str(path) else None)
    monkeypatch.setattr(graph, "save", lambda value, path: saved.append(value))
    monkeypatch.setattr(repo_module, "_run", lambda *args, **kw: "changed.py\ncaller.py\n")
    warm.context_for_pr("o/r", 1, "head", "base", {"changed.py"}, cache_root=tmp_path)
    assert history_refs == ["head"]
    assert saved and saved[-1].commit == "head"
    assert "stale" not in saved[-1].defs and "new_caller" in saved[-1].defs


def test_gold_recall_does_not_inflate_from_multiple_candidate_matches():
    from scripts.coderabbit_bench_v2 import summarize

    gold = [{"category": "bug"}, {"category": "style"}]
    score = summarize(gold, [{0, 1}, {2}], {0, 1, 2})
    assert score["strict"] == {"matched": 1, "golden": 1, "recall": 1.0}
    assert score["all"]["matched"] == 2
    assert "precision" not in score


async def test_truncation_keeps_usage_and_an_actionable_error():
    from types import SimpleNamespace

    from cr.llm.client import LLMClient, OutputBudgetExceeded
    from cr.models import FindingList

    class Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get_final_message(self):
            return SimpleNamespace(
                usage=Usage(input_tokens=10, output_tokens=16000),
                stop_reason="max_tokens",
                parsed_output=None,
            )

    client = LLMClient(
        client=SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: Stream()))
    )
    with pytest.raises(OutputBudgetExceeded):
        await client.parse(
            model="claude-sonnet-5",
            schema=FindingList,
            system=[],
            messages=[],
            max_tokens=16000,
            label="correctness",
        )
    assert client.usage.output_tokens == 16000
    assert client.calls[0].error == "OutputBudgetExceeded"
    assert client.total_cost_usd() > 0


def test_t2_does_not_reintroduce_the_live_truncation_regression():
    assert TIERS["T2"].finder_max_tokens >= 32000


def test_cli_never_posts_an_incomplete_review(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from cr import cli

    class FakePR:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def metadata(self):
            return {"head": {"sha": "head"}, "base": {"sha": "base"}}

        def diff(self):
            return patch()

        def posted_fingerprints(self):
            pytest.fail("Incomplete review reached the posting stage")

        def submit_review(self, *args):
            pytest.fail("Incomplete review was posted")

    async def incomplete(**kw):
        return ReviewResult(tier="T2", errors=["correctness: OutputBudgetExceeded"])

    monkeypatch.setattr(cli, "_require_credentials", lambda: None)
    monkeypatch.setattr(cli.settings, "github_token", "test-token")
    monkeypatch.setattr(cli, "GitHubPR", FakePR)
    monkeypatch.setattr(cli, "analyse", lambda *args: SimpleNamespace(rules=(), output=""))
    monkeypatch.setattr(cli, "run_review", incomplete)
    trace = tmp_path / "trace.json"
    output = CliRunner().invoke(
        cli.app, ["review-pr", "--pr", "o/r#1", "--no-graph", "--tier", "T2", "--trace", str(trace)]
    )
    assert output.exit_code == 1
    assert "INCOMPLETE" in output.stdout
    assert "No findings survived" not in output.stdout
    assert ReviewResult.model_validate_json(trace.read_text()).errors
