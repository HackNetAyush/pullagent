"""Connectivity and capability probe.

Answers three questions in about ten seconds and a fraction of a cent:

1. Can we reach the provider at all, with these credentials?
2. Does this deployment accept the request shape we depend on — structured
   outputs and the effort parameter?
3. **Is prompt caching actually working?** On Microsoft Foundry caching is a beta
   capability, so this is a real question rather than a formality. If it is off,
   every cost figure in the docs is wrong by roughly 5x and you want to know now
   rather than from an invoice.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from pydantic import BaseModel

from cr.config import Settings
from cr.llm.client import LLMClient, build_client
from cr.llm.prefix import PRContext, PrefixBuilder, RepoContext

# The cacheable prefix minimum is 1024 tokens on Sonnet 5, so a probe has to be
# genuinely large or it silently will not cache and the result is meaningless.
_FILLER = (
    "This repository follows conventional commits, keeps business logic out of "
    "controllers, prefers composition over inheritance, and requires a regression "
    "test for every bug fix. Error handling happens at system boundaries only. "
)


class Probe(BaseModel):
    ok: bool
    note: str


@dataclass
class DoctorReport:
    provider: str = ""
    model: str = ""
    reachable: bool = False
    structured_outputs: bool = False
    effort_accepted: bool = False
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def caching_works(self) -> bool:
        return self.cache_read_tokens > 0

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
    r = DoctorReport(provider=settings.provider, model=model)

    try:
        client = LLMClient(client=build_client(settings), max_concurrency=2)
    except ValueError as e:
        r.errors.append(str(e))
        return r

    builder = _builder()
    instruction = 'Reply with ok=true and note="probe".'

    # Call 1 — writes the cache.
    try:
        first = await client.parse(
            model=model,
            schema=Probe,
            system=builder.system(),
            messages=builder.messages(instruction),
            effort="low",
            max_tokens=256,
            label="doctor-1",
        )
        r.reachable = True
        r.effort_accepted = True
        r.structured_outputs = isinstance(first.parsed, Probe)
        r.cache_write_tokens = first.usage.cache_creation_input_tokens
    except Exception as e:  # noqa: BLE001 - a probe reports every failure verbatim
        r.errors.append(f"{type(e).__name__}: {e}")
        return r

    # Call 2 — identical prefix, so it must read the cache. This is the real test.
    try:
        second = await client.parse(
            model=model,
            schema=Probe,
            system=builder.system(),
            messages=builder.messages(instruction),
            effort="low",
            max_tokens=256,
            label="doctor-2",
        )
        r.cache_read_tokens = second.usage.cache_read_input_tokens
    except Exception as e:  # noqa: BLE001
        r.errors.append(f"second call failed: {type(e).__name__}: {e}")

    r.cost_usd = client.cost_usd(model)
    return r


def run(settings: Settings, model: str) -> DoctorReport:
    return asyncio.run(_probe(settings, model))
