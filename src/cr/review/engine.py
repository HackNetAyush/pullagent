"""The review engine: find -> verify -> gate.

Stages 6, 7 and 8 of ARCHITECTURE.md §4. Everything else in the system exists to
feed this and to deliver its output.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from cr.config import T3_VERIFIER_MODEL, Settings, TierConfig
from cr.config import settings as default_settings
from cr.llm.client import LLMClient, build_pool
from cr.llm.prefix import PRContext, PrefixBuilder, RepoContext
from cr.models import Finding, FindingList, ReviewResult, Verdict, VerifiedFinding
from cr.review import prompts
from cr.store import db as store

log = logging.getLogger(__name__)


async def find(
    client: LLMClient, builder: PrefixBuilder, tier: TierConfig
) -> list[Finding]:
    """Stage 6 — N specialist lenses over one shared cached prefix.

    Staggered by `LLMClient.fanout` so the first pass writes the cache before the
    rest read it. See PIPELINE.md §2.2.
    """
    roles = [(lens, prompts.finder_instruction(lens)) for lens in tier.finders]
    # xhigh spends far more on thinking, and thinking draws from max_tokens.
    budget = 64000 if tier.effort in ("xhigh", "max") else 32000
    calls = await client.fanout(
        model=tier.model,
        schema=FindingList,
        system=builder.system(),
        message_builder=builder.messages,
        roles=roles,
        effort=tier.effort,
        max_tokens=budget,
    )

    out: list[Finding] = []
    for call in calls:
        if not isinstance(call.parsed, FindingList):
            continue
        for f in call.parsed.findings:
            f.found_by = call.label
            out.append(f)
    return out


def prefilter(findings: list[Finding], s: Settings) -> tuple[list[Finding], list[Finding]]:
    """Drop findings that fail the proof obligation (D2) before spending on verification.

    Also dedups: several lenses legitimately spot the same defect, and we should
    only pay to verify it once. Highest confidence wins.
    """
    kept: dict[str, Finding] = {}
    dropped: list[Finding] = []

    for f in findings:
        if not f.failure_scenario.strip() or len(f.failure_scenario.strip()) < 20:
            dropped.append(f)
            continue
        if f.confidence < s.min_confidence:
            dropped.append(f)
            continue
        if not f.evidence:
            dropped.append(f)
            continue

        fp = f.fingerprint()
        existing = kept.get(fp)
        if existing is None:
            kept[fp] = f
        elif f.confidence > existing.confidence:
            # The displaced duplicate is still a drop — record it, or the dedup
            # metrics under-report and you cannot tell lens overlap from silence.
            dropped.append(existing)
            kept[fp] = f
        else:
            dropped.append(f)

    # Second pass: collapse findings that differ only in wording. Without this
    # two lenses spotting one bug both get posted, which is the noise problem.
    by_locality: dict[tuple, Finding] = {}
    for f in kept.values():
        loc = f.locality()
        best = by_locality.get(loc)
        if best is None:
            by_locality[loc] = f
        elif f.confidence > best.confidence:
            dropped.append(best)
            by_locality[loc] = f
        else:
            dropped.append(f)

    return list(by_locality.values()), dropped


async def verify(
    client: LLMClient, builder: PrefixBuilder, tier: TierConfig, findings: list[Finding]
) -> list[VerifiedFinding]:
    """Stage 7 — adversarial refutation (D3).

    Each verifier reuses the same cached prefix as the finders (the role
    instruction is the only thing that differs), so this stage is cheap in input
    and dominated by its own small outputs.

    The verifier sees the same code but never the finder's reasoning — that is
    what makes the judgement independent.
    """
    model = T3_VERIFIER_MODEL if tier.name == "T3" else tier.model

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


def gate(
    verified: list[VerifiedFinding],
    tier: TierConfig,
    suppressed_fps: set[str] | None = None,
) -> tuple[list, list]:
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
    killed = memory_killed + [v for v in remaining if not v.survived]
    survivors.sort(key=lambda v: v.rank, reverse=True)
    return survivors[: tier.max_comments], killed + survivors[tier.max_comments :]


async def review(
    *,
    repo: RepoContext,
    pr: PRContext,
    tier: TierConfig,
    client: LLMClient | None = None,
    cfg: Settings | None = None,
    remember: bool = True,
    on_stage: Callable[[str], None] | None = None,
) -> ReviewResult:
    s = cfg or default_settings
    llm = client or LLMClient(pool=build_pool(s))
    started = time.monotonic()

    builder = PrefixBuilder(preamble=prompts.PREAMBLE, repo=repo, pr=pr)
    emit = on_stage or (lambda _s: None)

    emit("find")
    raw = await find(llm, builder, tier)
    log.info("found %d raw findings", len(raw))

    emit("prefilter")
    candidates, dropped = prefilter(raw, s)
    log.info("prefilter kept %d, dropped %d", len(candidates), len(dropped))

    emit("verify")
    verified = await verify(llm, builder, tier, candidates) if candidates else []

    emit("gate")
    fps = store.suppressed_fingerprints(repo.slug) if remember else set()
    posted, suppressed = gate(verified, tier, fps)
    if fps:
        store.bump_hits(repo.slug, {v.finding.fingerprint() for v in suppressed} & fps)

    return ReviewResult(
        tier=tier.name,
        posted=posted,
        suppressed=suppressed,
        usage=llm.usage,
        elapsed_s=time.monotonic() - started,
    )
