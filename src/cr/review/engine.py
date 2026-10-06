"""The review engine: find -> verify -> gate.

Stages 6, 7 and 8 of ARCHITECTURE.md §4. Everything else in the system exists to
feed this and to deliver its output.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from cr.config import Settings, TierConfig
from cr.config import settings as default_settings
from cr.diff import review_chunks
from cr.llm.client import LLMClient, build_pool
from cr.llm.prefix import PRContext, PrefixBuilder, RepoContext
from cr.models import (
    FilteredFinding,
    Finding,
    FindingList,
    MergeBatch,
    ModelCost,
    ReviewResult,
    Usage,
    Verdict,
    VerificationBatch,
    VerifiedFinding,
)
from cr.review import cache, prompts
from cr.store import db as store

log = logging.getLogger(__name__)


async def find(
    client: LLMClient, builder: PrefixBuilder, tier: TierConfig, s: Settings
) -> list[Finding]:
    """Stage 6 — N specialist lenses over one shared prefix.

    On models with explicit caching, `LLMClient.fanout` staggers the first pass
    so it writes the cache before the rest read it (PIPELINE.md §2.2); on every
    other provider the lenses run at once. Either way the output contract is
    the same, so nothing downstream knows which provider found what.

    Lenses that share a model and effort fan out together over one prefix; a
    custom tier that gives a lens its own model runs that lens as its own
    group, concurrently with the rest.
    """
    # xhigh spends far more on thinking, and thinking draws from max_tokens.
    budget = tier.finder_max_tokens

    groups: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for lens in tier.finders:
        # An owner-written lens carries its own instruction; built-ins use ours.
        instruction = tier.finder_prompts.get(lens) or prompts.finder_instruction(lens)
        groups.setdefault(tier.finder_route(lens), []).append((lens, instruction))
    batches = await asyncio.gather(
        *(
            client.fanout(
                model=model,
                schema=FindingList,
                system=builder.system(),
                message_builder=builder.messages,
                roles=roles,
                effort=effort,
                max_tokens=budget,
            )
            for (model, effort), roles in groups.items()
        )
    )

    out: list[Finding] = []
    for call in (c for batch in batches for c in batch):
        if not isinstance(call.parsed, FindingList):
            continue
        for f in call.parsed.findings:
            f.found_by = call.label
            out.append(f)
    return out


def prefilter(
    findings: list[Finding],
    s: Settings,
    trace: list[FilteredFinding] | None = None,
    dedup_trace: list[FilteredFinding] | None = None,
) -> tuple[list[Finding], list[Finding]]:
    """Drop findings that fail the proof obligation (D2) before spending on verification.

    Also dedups: several lenses legitimately spot the same defect, and we should
    only pay to verify it once. Highest confidence wins.

    `trace` records proof-obligation drops (D2); `dedup_trace` records dedup
    drops (exact fingerprint, then text-similarity). Kept separate so stage
    attribution can tell "never a real finding" apart from "collapsed into
    another finding" — conflating them previously misreported an LLM-merge
    loss as a prefilter loss (see merge_colocated / resolve_merge_groups,
    which append to this same dedup_trace).
    """
    kept: dict[str, Finding] = {}
    dropped: list[Finding] = []

    def drop(f: Finding, reason: str, *, dedup: bool = False) -> None:
        dropped.append(f)
        target = dedup_trace if dedup else trace
        if target is not None:
            target.append(FilteredFinding(finding=f, reason=reason))

    for f in findings:
        if not f.failure_scenario.strip() or len(f.failure_scenario.strip()) < 20:
            drop(f, "missing_concrete_scenario")
            continue
        if f.confidence < s.min_confidence:
            drop(f, "below_confidence_threshold")
            continue
        if not f.evidence:
            drop(f, "missing_evidence")
            continue

        fp = f.fingerprint()
        existing = kept.get(fp)
        if existing is None:
            kept[fp] = f
        elif f.confidence > existing.confidence:
            # The displaced duplicate is still a drop — record it, or the dedup
            # metrics under-report and you cannot tell lens overlap from silence.
            drop(existing, "duplicate_fingerprint", dedup=True)
            kept[fp] = f
        else:
            drop(f, "duplicate_fingerprint", dedup=True)

    # Second pass: collapse findings that differ only in wording. Without this
    # two lenses spotting one bug both get posted, which is the noise problem.
    unique: list[Finding] = []
    for f in sorted(kept.values(), key=lambda f: f.confidence, reverse=True):
        if any(same_defect(f, other) for other in unique):
            drop(f, "duplicate_claim_or_fix", dedup=True)
        else:
            unique.append(f)
    return unique, dropped


# How close two findings' anchor lines must be to even be considered the same
# location. Proximity alone is never proof — same_defect() still requires prose
# or fix identity on top of it — but merge_colocated() escalates anything within
# this window that the cheap check let through to an explicit model judgment.
#
# Measured on Sentry #93824: independent lenses citing one root cause (a loop
# that abandons remaining processes on deadline) anchored anywhere from line
# 329 to 347 depending on which specific line each lens picked as evidence —
# an 18-line spread with a max single gap of 7 between consecutive citations.
# At the old window (5), line 329 formed its own isolated cluster and was
# never even compared against the other two citations of the same bug.
LOCATION_WINDOW = 10

# Proximity alone is too weak to group on. Measured on a repo with eight
# planted bugs: on one 60-line file, every finding sat within LOCATION_WINDOW
# of a neighbour, so chain-linking collapsed 18 independent findings into a
# single cluster — which then blew the size cap and skipped merge entirely,
# precisely where the duplicates were densest. Four restatements of one
# auth bug and two of one crypto bug shipped as six comments, and crowded a
# genuine race condition off the comment budget.
#
# So candidate pairs are drawn on *claim similarity*, with proximity as a
# bonus rather than a gate. Calibrated against 187 real pairs from that run
# (42 truly duplicate, 145 independent): true duplicates scored 0.321 and up,
# independent pairs 0.367 and down. A floor of 0.30 catches every duplicate
# and sends 14 borderline pairs to the model, which is exactly the judgment
# call the model is there to make.
MERGE_SIMILARITY_FLOOR = 0.30
MERGE_PROXIMITY_BONUS = 0.15

# Words that carry no signal about *which* defect is being described.
_MERGE_STOPWORDS = frozenset(
    # fmt: off
    [
        "the",
        "a",
        "an",
        "is",
        "are",
        "to",
        "of",
        "in",
        "on",
        "and",
        "or",
        "for",
        "that",
        "this",
        "it",
        "its",
        "be",
        "so",
        "with",
        "when",
        "no",
        "not",
        "any",
        "which",
        "while",
        "into",
        "from",
        "has",
        "have",
        "as",
        "at",
        "by",
        "but",
        "can",
        "could",
        "would",
        "should",
        "will",
        "may",
        "might",
        "than",
        "then",
        "there",
        "their",
        "they",
        "you",
        "your",
        "we",
        "our",
    ]
    # fmt: on
)

# Bounds on the merge stage. A group is a connected component of the similarity
# graph and is judged as a full clique, so its cost is quadratic in its size —
# these keep one pathological file from dominating a review's spend. Unlike the
# old size cap, exceeding them degrades explicitly (the excess stays unmerged
# and is logged) instead of silently skipping the whole group.
MAX_MERGE_GROUP = 12
MAX_MERGE_PAIRS_PER_CALL = 24
MAX_MERGE_PAIRS = 120


def same_defect(a: Finding, b: Finding) -> bool:
    """Conservative prose + fix identity, inspired by PR-Agent's dual fingerprints.

    Proximity alone is never proof: one call can have several independent bugs.
    """
    if a.anchor_file != b.anchor_file or abs(a.anchor_line - b.anchor_line) > LOCATION_WINDOW:
        return False

    def norm(text: str) -> str:
        return re.sub(r"\s+", " ", text.strip())

    x, y = (set(re.findall(r"[a-z0-9_]+", f.claim.lower())) for f in (a, b))
    if (
        a.suggested_fix
        and b.suggested_fix
        and len(a.suggested_fix) > 30
        and norm(a.suggested_fix) == norm(b.suggested_fix)
        and x
        and y
        and len(x & y) / len(x | y) >= 0.65
    ):
        return True
    if norm(a.failure_scenario).lower() != norm(b.failure_scenario).lower():
        return False
    return bool(x and y) and len(x & y) / min(len(x), len(y)) >= 0.8


def _merge_tokens(f: Finding) -> frozenset[str]:
    """Content words of a finding's claim, for cheap similarity."""
    words = re.findall(r"[a-z0-9_]+", f.claim.lower())
    return frozenset(w for w in words if len(w) > 2 and w not in _MERGE_STOPWORDS)


def pair_score(a: Finding, b: Finding) -> float:
    """How likely two findings describe one defect, judged lexically.

    Jaccard over claim content words, plus a bonus when the anchors nearly
    coincide. Proximity is corroboration, never sufficient on its own: two
    unrelated bugs three lines apart are common, and a docstring restatement
    of the same bug thirty lines away is too (that is how a `min-heap
    ordering` finding anchored at line 1 and its twin at line 31 both shipped).
    """
    if a.anchor_file != b.anchor_file:
        return 0.0
    x, y = _merge_tokens(a), _merge_tokens(b)
    score = len(x & y) / len(x | y) if (x and y) else 0.0
    if abs(a.anchor_line - b.anchor_line) <= LOCATION_WINDOW:
        score += MERGE_PROXIMITY_BONUS
    return score


def _merge_groups(findings: list[Finding]) -> list[list[int]]:
    """Connected components of the "might be the same defect" graph.

    Every internal pair of a returned group is judged, which is what keeps
    `merge_colocated`'s clique requirement meaningful: a component whose
    members were never all compared could not be checked for one.

    Oversized components are trimmed to their highest-confidence members
    rather than dropped — a group of twenty restatements should still collapse
    nineteen of them, and the survivors merely stay independent.
    """
    parent = list(range(len(findings)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    by_file: dict[str, list[int]] = {}
    for i, f in enumerate(findings):
        by_file.setdefault(f.anchor_file, []).append(i)

    best: dict[int, float] = {}
    for idxs in by_file.values():
        for a, b in itertools.combinations(idxs, 2):
            score = pair_score(findings[a], findings[b])
            if score < MERGE_SIMILARITY_FLOOR:
                continue
            ra, rb = find(a), find(b)
            parent[ra] = rb
            best[rb] = max(best.get(rb, 0.0), best.get(ra, 0.0), score)

    components: dict[int, list[int]] = {}
    for i in range(len(findings)):
        components.setdefault(find(i), []).append(i)

    groups: list[tuple[float, list[int]]] = []
    for root, members in components.items():
        if len(members) < 2:
            continue
        if len(members) > MAX_MERGE_GROUP:
            members.sort(key=lambda i: findings[i].confidence, reverse=True)
            dropped = members[MAX_MERGE_GROUP:]
            members = members[:MAX_MERGE_GROUP]
            log.warning(
                "merge group on %s has %d members; judging the %d most confident, "
                "%d stay independent",
                findings[members[0]].anchor_file,
                len(members) + len(dropped),
                MAX_MERGE_GROUP,
                len(dropped),
            )
        groups.append((best.get(root, 0.0), sorted(members)))

    # Spend the pair budget on the most promising groups first.
    groups.sort(key=lambda g: g[0], reverse=True)
    out: list[list[int]] = []
    spent = 0
    for _score, members in groups:
        cost = len(members) * (len(members) - 1) // 2
        if spent + cost > MAX_MERGE_PAIRS:
            log.warning(
                "merge pair budget %d reached; %d finding(s) on %s stay independent",
                MAX_MERGE_PAIRS,
                len(members),
                findings[members[0]].anchor_file,
            )
            continue
        spent += cost
        out.append(members)
    return out


async def merge_colocated(
    client: LLMClient,
    builder: PrefixBuilder,
    tier: TierConfig,
    findings: list[Finding],
) -> list[Finding]:
    """Stage 6.5 — tag paraphrased duplicates that prefilter's text check missed.

    Independent lenses often describe one real bug in different words: same file,
    same lines, different prose and different suggested fix, so same_defect()'s
    exact-text bar never fires and every lens's restatement gets verified and
    ranked as its own finding. Left uncaught, several restatements of one bug can
    dominate the comment budget and push out distinct, lower-confidence findings
    that never had a duplicate. This stage only spends a call on findings that
    already share a location — the common case where nothing collides costs nothing.

    This stage never deletes a finding — it only tags confirmed duplicate-group
    membership onto `Finding.merge_group`. Every candidate, including the ones
    that will turn out to be duplicates, still goes to verify(); the losing
    members of a group are removed only after verification, by
    resolve_merge_groups. That is what lets a wrongly-discarded alternative
    survive (its sibling being refuted doesn't take it down with it) and what
    lets the representative be chosen using verifier outcome, not just raw
    finder confidence.

    Candidate groups come from `_merge_groups`: connected components of a
    claim-similarity graph, not raw line proximity. Every pair inside a group
    is judged, so a group of nine can legitimately contain three different
    defects — the model's answers, not the grouping, decide what merges.

    Confirmed pairs are then unioned into components, and a component merges
    only if every pair inside it was confirmed — a full clique, not merely a
    chain through one bridging candidate. A composite finding that lumps two
    unrelated bugs together can make each of them look like its duplicate
    without the two ever being compared; requiring a clique refuses to bridge
    them. Anything short of a clique fails open for that component.
    """
    groups = _merge_groups(findings)
    if not groups:
        return findings

    model, effort = tier.checker()

    async def judge(ids: list[int], pairs: list[tuple[int, int]]) -> set[frozenset[int]]:
        """One model call over a batch of pairs. Raises if the answer is unusable."""
        exclude = {"found_by", "merge_group"}
        payload = [
            {"finding_id": i, "finding": findings[i].model_dump(exclude=exclude)} for i in ids
        ]
        pairs_payload = [{"left_id": a, "right_id": b} for a, b in pairs]
        call = await client.parse(
            model=model,
            schema=MergeBatch,
            system=builder.system(),
            messages=builder.messages(
                prompts.merge_instruction(json.dumps(payload), json.dumps(pairs_payload))
            ),
            effort=effort,
            max_tokens=tier.verifier_max_tokens,
            label="merge",
        )
        if not isinstance(call.parsed, MergeBatch):
            raise ValueError("missing merge batch")
        got = {frozenset((d.left_id, d.right_id)) for d in call.parsed.decisions}
        if got != {frozenset(p) for p in pairs} or len(call.parsed.decisions) != len(pairs):
            raise ValueError("merge pairs missing, duplicated, or unexpected")
        return {frozenset((d.left_id, d.right_id)) for d in call.parsed.decisions if d.same_defect}

    async def resolve(ids: list[int]) -> None:
        all_pairs = list(itertools.combinations(ids, 2))
        # A big group is split across calls rather than abandoned. The clique
        # check below needs every internal pair judged, so a partial answer is
        # useless — hence one failed batch fails the whole group open.
        batches = [
            all_pairs[i : i + MAX_MERGE_PAIRS_PER_CALL]
            for i in range(0, len(all_pairs), MAX_MERGE_PAIRS_PER_CALL)
        ]
        try:
            results = await asyncio.gather(
                *(judge(sorted({i for p in batch for i in p}), batch) for batch in batches)
            )
        except Exception as exc:
            log.error("merge batch failed: %s", exc)
            return  # fail open — every candidate in this group stays independent

        edges: set[frozenset[int]] = set()
        for result in results:
            edges |= result

        parent = {i: i for i in ids}

        def find(i: int) -> int:
            while parent[i] != i:
                i = parent[i]
            return i

        for a, b in (tuple(e) for e in edges):
            parent[find(a)] = find(b)

        components: dict[int, list[int]] = {}
        for i in ids:
            components.setdefault(find(i), []).append(i)

        for members in components.values():
            if len(members) < 2:
                continue
            # Require every pair in the component to be a confirmed edge — a
            # full clique. A star (one bridging node, leaves not confirmed
            # against each other) fails this and merges nothing.
            is_clique = all(
                frozenset((a, b)) in edges
                for idx, a in enumerate(members)
                for b in members[idx + 1 :]
            )
            if not is_clique:
                continue
            group_id = min(members) + 1
            for i in members:
                findings[i].merge_group = group_id

    await asyncio.gather(*(resolve(g) for g in groups))
    return findings


def resolve_merge_groups(
    verified: list[VerifiedFinding], dedup_trace: list[FilteredFinding] | None = None
) -> list[VerifiedFinding]:
    """Pick one representative per confirmed duplicate group, after verification.

    Every group member was already verified (see merge_colocated), so "retry
    the best discarded alternative" needs no extra call: if the top-confidence
    member was refuted but another member of its group survived, that survivor
    already outranks it on the `survived` sort key below. Non-representative
    members are dropped here, before gate() ever sees them — leaving them in
    as "refuted" would double-count one defect against the verifier kill rate.
    """
    groups: dict[int, list[VerifiedFinding]] = {}
    singles: list[VerifiedFinding] = []
    for vf in verified:
        gid = vf.finding.merge_group
        if gid:
            groups.setdefault(gid, []).append(vf)
        else:
            singles.append(vf)

    def rank_key(vf: VerifiedFinding) -> tuple:
        return (vf.survived, len(vf.finding.evidence), vf.rank, vf.finding.confidence)

    kept = list(singles)
    for members in groups.values():
        best = max(members, key=rank_key)
        kept.append(best)
        for vf in members:
            if vf is not best and dedup_trace is not None:
                dedup_trace.append(
                    FilteredFinding(finding=vf.finding, reason="duplicate_same_root_cause_llm")
                )
    return kept


async def verify(
    client: LLMClient, builder: PrefixBuilder, tier: TierConfig, findings: list[Finding]
) -> list[VerifiedFinding]:
    """Stage 7 — adversarial refutation (D3).

    When `tier.verifier_model` is unset, the verifier reuses the same cached
    prefix as the finders (the role instruction is the only thing that
    differs), so this stage is cheap in input and dominated by its own small
    outputs. Tiers that escalate verification to a different model (T3) or a
    different provider entirely (T4) don't get that discount; their cost model
    should assume a fresh warm here, not a free ride. Any registered model can
    verify — the provider is resolved per call, not per stage.

    The verifier sees the same code but never the finder's reasoning — that is
    what makes the judgement independent.
    """
    by_id: dict[int, list[Verdict]] = {i: [] for i in range(len(findings))}

    async def batch(ids: list[int], lens: str, adjudicate: bool = False) -> None:
        # Each lens may run on its own model; adjudication weighs every lens's
        # verdict, so it runs on the tier's verifier defaults.
        model, effort = tier.checker() if adjudicate else tier.verifier_route(lens)
        payload = []
        for i in ids:
            item = {
                "finding_id": i,
                "finding": findings[i].model_dump(exclude={"found_by", "merge_group"}),
            }
            if adjudicate:
                item["prior_assessments"] = [v.model_dump() for v in by_id[i]]
            payload.append(item)
        label = "adjudicate" if adjudicate else f"verify:{lens}"
        try:
            call = await client.parse(
                model=model,
                schema=VerificationBatch,
                system=builder.system(),
                messages=builder.messages(
                    prompts.batch_verifier_instruction(
                        lens,
                        json.dumps(payload, ensure_ascii=False),
                        None if adjudicate else tier.verifier_prompts.get(lens),
                    )
                ),
                effort=effort,
                max_tokens=tier.verifier_max_tokens,
                label=label,
            )
            if not isinstance(call.parsed, VerificationBatch):
                raise ValueError("missing verification batch")
            decisions = call.parsed.decisions
            if sorted(d.finding_id for d in decisions) != sorted(ids):
                raise ValueError("verification IDs missing, duplicated, or unexpected")
            for d in decisions:
                repairable = d.status == "repairable"
                verdict = Verdict(
                    refuted=d.status not in ("confirmed", "repairable"),
                    uncertain=d.status == "uncertain",
                    reasoning=d.reasoning,
                    corrected_severity=d.corrected_severity,
                    corrected_claim=d.corrected_claim if repairable else None,
                    corrected_failure_scenario=d.corrected_failure_scenario if repairable else None,
                    lens=label,
                )
                by_id[d.finding_id].append(verdict)
        except Exception as exc:
            log.error("%s batch failed: %s", label, exc)
            for i in ids:
                by_id[i].append(
                    Verdict(
                        refuted=True,
                        reasoning=f"{label}: {type(exc).__name__}",
                        lens=label,
                        infrastructure_error=True,
                    )
                )

    size = tier.verification_batch_size
    chunks = [list(range(i, min(i + size, len(findings)))) for i in range(0, len(findings), size)]
    jobs = [(ids, lens) for ids in chunks for lens in tier.verifier_lenses]

    # A new structured schema may need a separate cache warm. Finish one batch
    # before fanning out the rest so cold verifiers do not all rewrite it.
    def corrections_conflict(vs: list[Verdict]) -> bool:
        """Two lenses proposing different corrections is itself a disagreement —
        picking between them by verdict-list order would be a race (asyncio.gather
        completion order, not lens order), so route it through adjudication like
        any other disputed verdict instead."""
        return (
            len({v.corrected_severity for v in vs if v.corrected_severity}) > 1
            or len({v.corrected_claim for v in vs if v.corrected_claim}) > 1
            or len({v.corrected_failure_scenario for v in vs if v.corrected_failure_scenario}) > 1
        )

    if jobs:
        await batch(*jobs[0])
        await asyncio.gather(*(batch(ids, lens) for ids, lens in jobs[1:]))
    disputed = [
        i
        for i, vs in by_id.items()
        if not any(v.infrastructure_error for v in vs)
        and (
            any(v.uncertain for v in vs)
            or len({v.refuted for v in vs}) > 1
            or corrections_conflict(vs)
        )
    ]
    await asyncio.gather(
        *(batch(disputed[i : i + size], "evidence", True) for i in range(0, len(disputed), size))
    )
    return [VerifiedFinding(finding=f, verdicts=by_id[i]) for i, f in enumerate(findings)]


async def verify_individually(
    client: LLMClient, builder: PrefixBuilder, tier: TierConfig, findings: list[Finding]
) -> list[VerifiedFinding]:
    """Legacy verifier retained for controlled ablation runs."""
    model = tier.verifier_model or tier.model

    async def one(finding: Finding) -> VerifiedFinding:
        payload = finding.model_dump_json(indent=2, exclude={"found_by"})
        calls = await asyncio.gather(
            *(
                client.parse(
                    model=model,
                    schema=Verdict,
                    # system must stay byte-identical to the finders' or the
                    # prefix match breaks at byte 0 and nothing reads the cache.
                    # The verifier role goes in the trailing uncached block.
                    system=builder.system(),
                    messages=builder.messages(
                        prompts.VERIFIER_PREAMBLE
                        + "\n\n"
                        + prompts.verifier_instruction(lens, payload)
                    ),
                    effort=tier.effort,
                    max_tokens=12000,
                    label=f"verify:{lens}",
                )
                for lens in tier.verifier_lenses
            ),
            return_exceptions=True,
        )

        verdicts: list[Verdict] = []
        for lens, call in zip(tier.verifier_lenses, calls, strict=False):
            if isinstance(call, BaseException):
                log.error("verifier %s failed: %s", lens, call)
                # A verifier that errored must not silently pass the finding.
                verdicts.append(
                    Verdict(refuted=True, reasoning=f"verifier error: {call}", lens=lens)
                )
                continue
            if isinstance(call.parsed, Verdict):
                v = call.parsed
                v.lens = lens
                verdicts.append(v)

        return VerifiedFinding(finding=finding, verdicts=verdicts)

    return list(await asyncio.gather(*(one(f) for f in findings)))


@dataclass
class GateResult:
    """Why each finding did or did not get posted. Kept disjoint on purpose."""

    posted: list[VerifiedFinding] = field(default_factory=list)
    refuted: list[VerifiedFinding] = field(default_factory=list)
    memory_suppressed: list[VerifiedFinding] = field(default_factory=list)
    budget_trimmed: list[VerifiedFinding] = field(default_factory=list)

    @property
    def not_posted(self) -> list[VerifiedFinding]:
        return self.refuted + self.memory_suppressed + self.budget_trimmed


def gate(
    verified: list[VerifiedFinding],
    tier: TierConfig,
    suppressed_fps: set[str] | None = None,
) -> GateResult:
    """Stage 8 — survivors, ranked, capped.

    The comment budget is a feature. Anything past the cap is real but not worth
    a developer's attention today; it goes to the collapsed section.
    """
    fps = suppressed_fps or set()
    # A human already rejected these. Re-posting them is how a reviewer loses trust.
    memory_killed = [v for v in verified if v.finding.fingerprint() in fps]
    remaining = [v for v in verified if v.finding.fingerprint() not in fps]
    if memory_killed:
        log.info("suppression memory dropped %d finding(s)", len(memory_killed))

    survivors = [v for v in remaining if v.survived]
    refuted = [v for v in remaining if not v.survived]
    survivors.sort(key=lambda v: v.rank, reverse=True)
    return GateResult(
        posted=survivors[: tier.max_comments],
        refuted=refuted,
        memory_suppressed=memory_killed,
        budget_trimmed=survivors[tier.max_comments :],
    )


def model_costs(llm: Any) -> list[ModelCost]:
    """Per-model spend for one run, from the client's call traces."""
    out: dict[str, ModelCost] = {}
    for c in getattr(llm, "calls", []) or []:
        mc = out.get(c.model)
        if mc is None:
            label, provider, priced = c.model, "", True
            if hasattr(llm, "spec"):
                try:
                    spec = llm.spec(c.model)
                    label, provider, priced = spec.label, spec.provider, spec.priced
                except ValueError:
                    pass
            mc = out[c.model] = ModelCost(
                model=c.model, label=label, provider=provider, priced=priced
            )
        mc.calls += 1
        mc.input_tokens += (
            c.usage.input_tokens
            + c.usage.cache_read_input_tokens
            + c.usage.cache_creation_input_tokens
        )
        mc.output_tokens += c.usage.output_tokens
        mc.cost_usd += c.cost_usd
    return list(out.values())


async def review(
    *,
    repo: RepoContext,
    pr: PRContext,
    tier: TierConfig,
    client: LLMClient | None = None,
    cfg: Settings | None = None,
    remember: bool = True,
    on_stage: Callable[[str], None] | None = None,
    # Reports the ledger row id the moment it exists. A caller that can be
    # cancelled mid-review (the GitHub App supersedes a review when a new
    # commit lands) has no result to read `run_id` off, and would otherwise
    # leave the row marked running forever.
    on_start: Callable[[int | None], None] | None = None,
    record: bool = True,
    source: str = "pr",
    pr_number: int | None = None,
    head_sha: str = "",
    actor: str = "",
    use_cache: bool | None = None,
) -> ReviewResult:
    s = cfg or default_settings
    llm = client or LLMClient(pool=build_pool(s), max_concurrency=s.max_concurrency)
    started = time.monotonic()

    # The ledger lives here rather than in each command, so a new caller cannot
    # forget to record and silently vanish from the dashboard.
    run_id = (
        store.start_run(
            repo.slug,
            tier=tier.name,
            model=tier.model,
            pr_number=pr_number,
            head_sha=head_sha,
            source=source,
            actor=actor,
            billing="byok" if tier.custom else "managed",
        )
        if record
        else None
    )
    if on_start:
        on_start(run_id)

    def emit(stage: str) -> None:
        store.set_stage(run_id, stage)
        if on_stage:
            on_stage(stage)

    builder = PrefixBuilder(preamble=prompts.PREAMBLE, repo=repo, pr=pr)
    key = cache.review_key(repo, pr, tier, s, head_sha)
    # Fresh measurements by default for every benchmark/eval run.
    cache_enabled = s.review_cache and (
        use_cache if use_cache is not None else source == "pr" and bool(head_sha)
    )
    cached = cache.load(key, s.review_cache_ttl_s) if cache_enabled else None
    if cached is not None:
        fps = store.suppressed_fingerprints(repo.slug) if remember else set()
        # Memory and presentation policy are always reapplied at replay time.
        verified = cached.posted + cached.budget_trimmed + cached.refuted + cached.memory_suppressed
        g = gate(verified, tier, fps)
        cached.posted, cached.refuted = g.posted, g.refuted
        cached.budget_trimmed, cached.memory_suppressed = g.budget_trimmed, g.memory_suppressed
        cached.suppressed = g.not_posted
        cached.cache_hit, cached.cached_cost_usd = True, cached.cost_usd
        cached.usage, cached.cost_usd, cached.calls = Usage(), 0.0, []
        cached.elapsed_s = time.monotonic() - started
        cached.run_id = run_id
        if fps:
            store.bump_hits(repo.slug, {v.finding.fingerprint() for v in g.memory_suppressed})
        store.finish_run(run_id, cached, cost=0.0)
        return cached

    try:
        emit("find")
        chunks = review_chunks(pr.diff, s.finder_chunk_chars)
        raw = []
        for chunk in chunks:
            chunk_pr = replace(pr, diff=chunk)
            chunk_builder = PrefixBuilder(preamble=prompts.PREAMBLE, repo=repo, pr=chunk_pr)
            raw.extend(await find(llm, chunk_builder, tier, s))
        if len(chunks) > 1:
            log.info("covered all diff files in %d chunks", len(chunks))
    except Exception as exc:
        store.finish_run(
            run_id,
            ReviewResult(tier=tier.name, usage=llm.usage, cost_usd=llm.total_cost_usd()),
            cost=llm.total_cost_usd(),
            error=str(exc),
        )
        raise
    log.info("found %d raw findings", len(raw))

    emit("prefilter")
    filter_trace: list[FilteredFinding] = []
    dedup_trace: list[FilteredFinding] = []
    candidates, dropped = prefilter(raw, s, filter_trace, dedup_trace)
    log.info("prefilter kept %d, dropped %d", len(candidates), len(dropped))

    emit("merge")
    candidates = await merge_colocated(llm, builder, tier, candidates)

    emit("verify")
    verified = []
    if len(chunks) == 1:
        verified = await verify(llm, builder, tier, candidates) if candidates else []
    else:
        # Verification gets every chunk containing a candidate's cited evidence.
        # Cross-file evidence is retained; unrelated patches need not be repeated.
        from cr.diff import parse

        for chunk in chunks:
            paths = {f.path for f in parse(chunk)}
            group = [
                f
                for f in candidates
                if f.anchor_file in paths and not any(v.finding == f for v in verified)
            ]
            if not group:
                continue
            evidence_paths = {e.file for f in group for e in f.evidence}
            relevant = [c for c in chunks if evidence_paths & {f.path for f in parse(c)}]
            vb = PrefixBuilder(
                preamble=prompts.PREAMBLE, repo=repo, pr=replace(pr, diff="\n".join(relevant))
            )
            verified.extend(await verify(llm, vb, tier, group))

        for f in candidates:
            if not any(v.finding == f for v in verified):
                verified.append(
                    VerifiedFinding(
                        finding=f,
                        verdicts=[
                            Verdict(
                                refuted=True,
                                infrastructure_error=True,
                                lens="context",
                                reasoning="Candidate anchor not present in any reviewed patch",
                            )
                        ],
                    )
                )

    before_dedup = len(verified)
    verified = resolve_merge_groups(verified, dedup_trace)
    if len(verified) < before_dedup:
        log.info("merge collapsed %d confirmed duplicate(s)", before_dedup - len(verified))

    emit("gate")
    fps = store.suppressed_fingerprints(repo.slug) if remember else set()
    g = gate(verified, tier, fps)
    if fps:
        store.bump_hits(repo.slug, {v.finding.fingerprint() for v in g.memory_suppressed})

    result = ReviewResult(
        tier=tier.name,
        posted=g.posted,
        suppressed=g.not_posted,
        refuted=g.refuted,
        memory_suppressed=g.memory_suppressed,
        budget_trimmed=g.budget_trimmed,
        usage=llm.usage,
        cost_usd=llm.total_cost_usd(),
        elapsed_s=time.monotonic() - started,
        raw_findings=raw,
        prefiltered=filter_trace,
        deduplicated=dedup_trace,
        calls=getattr(llm, "calls", []),
        # A failed merge call fails open (its group's candidates just stay
        # independent — see merge_colocated) and touches nothing a reader relies
        # on for correctness, unlike a failed finder (missed coverage) or
        # verifier (a bad finding could wrongly survive). It must not trip the
        # same "don't post, don't score" gate as those — it costs a little
        # redundancy, not soundness.
        errors=[
            f"{c.label}: {c.error}"
            for c in getattr(llm, "calls", [])
            if c.error and c.label != "merge"
        ]
        + [v.reasoning for vf in verified for v in vf.verdicts if v.infrastructure_error],
        review_key=key,
        run_id=run_id,
        model_costs=model_costs(llm),
    )
    if cache_enabled:
        cache.save(result)
    store.finish_run(run_id, result, cost=result.cost_usd, error="; ".join(result.errors))
    return result
