"""Connectivity probe: reachability, structured outputs, and whether prompt
caching actually works — for any model in the registry, on any provider.

Caching is the number to watch. On explicit-cache providers (Claude) every
cost figure in the docs assumes it; if it is off, they are wrong by ~5x.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from pydantic import BaseModel

from cr.config import Settings
from cr.llm.client import LLMClient, build_pool
from cr.llm.prefix import PRContext, PrefixBuilder, RepoContext
from cr.llm.registry import PROVIDERS, CacheMode

# The cacheable prefix minimum is 1024 tokens on Sonnet 5, so a probe has to be
# genuinely large or it silently will not cache and the result is meaningless.
_FILLER = (
    "This repository follows conventional commits, keeps business logic out of "
    "controllers, prefers composition over inheritance, and requires a regression "
    "test for every bug fix. Error handling happens at system boundaries only. "
)

# Room for a reasoning model to think briefly before a two-field answer.
_PROBE_MAX_TOKENS = 2048


class Probe(BaseModel):
    ok: bool
    note: str


@dataclass
class DoctorReport:
    provider: str = ""
    model: str = ""
    wire: str = ""
    cache_mode: str = ""
    known: bool = True
    reachable: bool = False
    structured_outputs: bool = False
    effort_supported: bool = False
    effort_accepted: bool = False
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def caching_works(self) -> bool:
        return self.cache_read_tokens > 0

    @property
    def caching_expected(self) -> bool:
        return self.cache_mode == CacheMode.EXPLICIT.value

    @property
    def healthy(self) -> bool:
        return self.reachable and self.structured_outputs


def _builder() -> PrefixBuilder:
    return PrefixBuilder(
        preamble="You are a diagnostic probe. Answer exactly as instructed.",
        repo=RepoContext(slug="cr/doctor", conventions=_FILLER * 20),
        pr=PRContext(
            title="Connectivity probe",
            description="Not a real review.",
            diff="@@ -1,1 +1,1 @@\n-old\n+new\n",
        ),
    )


async def _probe(settings: Settings, model: str) -> DoctorReport:
    r = DoctorReport(model=model)

    unfilled = settings.unfilled()
    if unfilled:
        r.errors.append(
            "Still on the .env template placeholder: "
            + ", ".join(unfilled)
            + ". Fill these in before probing — a placeholder key surfaces as an "
            "authentication error that reads like a broken endpoint."
        )
        return r

    try:
        client = LLMClient(pool=build_pool(settings), max_concurrency=2)
    except ValueError as e:
        r.errors.append(str(e))
        return r
    return await probe(client, model, r)


async def probe(
    client: LLMClient,
    model: str,
    r: DoctorReport | None = None,
    *,
    check_cache: bool = True,
) -> DoctorReport:
    """Two identical structured calls through `client`: the first proves the
    endpoint, the key and schema-enforced output; the second shows whether the
    prefix was cached. Works on any pool — the deployment's or a customer's.
    `check_cache=False` skips the second call when only reachability matters."""
    r = r or DoctorReport(model=model)
    try:
        spec = client.spec(model)
    except ValueError as e:
        r.errors.append(str(e))
        return r

    r.provider = PROVIDERS[spec.provider].label
    r.wire = spec.wire.value
    r.cache_mode = spec.cache.value
    r.known = spec.known
    r.effort_supported = spec.supports_effort

    builder = _builder()
    instruction = 'Reply with ok=true and note="probe".'

    # Call 1 — writes the cache where caching is explicit.
    try:
        first = await client.parse(
            model=model,
            schema=Probe,
            system=builder.system(),
            messages=builder.messages(instruction),
            effort="low",
            max_tokens=_PROBE_MAX_TOKENS,
            label="doctor-1",
        )
        r.reachable = True
        r.effort_accepted = r.effort_supported
        r.structured_outputs = isinstance(first.parsed, Probe)
        r.cache_write_tokens = first.usage.cache_creation_input_tokens
    except Exception as e:  # noqa: BLE001 - a probe reports every failure verbatim
        r.errors.append(f"{type(e).__name__}: {e}")
        r.cost_usd = client.total_cost_usd()
        return r

    if not check_cache:
        r.cost_usd = client.total_cost_usd()
        return r

    # Call 2 — identical prefix, so it must read the cache. This is the real test.
    try:
        second = await client.parse(
            model=model,
            schema=Probe,
            system=builder.system(),
            messages=builder.messages(instruction),
            effort="low",
            max_tokens=_PROBE_MAX_TOKENS,
            label="doctor-2",
        )
        r.cache_read_tokens = second.usage.cache_read_input_tokens
    except Exception as e:  # noqa: BLE001
        r.errors.append(f"second call failed: {type(e).__name__}: {e}")

    r.cost_usd = client.total_cost_usd()
    return r


def run(settings: Settings, model: str) -> DoctorReport:
    return asyncio.run(_probe(settings, model))
