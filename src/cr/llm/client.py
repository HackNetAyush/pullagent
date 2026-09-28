"""Anthropic client wrapper.

Deliberately the native SDK, not LiteLLM: we need direct control over cache
breakpoints, effort, and thinking — exactly the things an abstraction layer
flattens away.

The important function here is `fanout`. See PIPELINE.md §2.2 "Trap 1".
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, TypeVar

from anthropic import AsyncAnthropic, AsyncAnthropicFoundry
from pydantic import BaseModel

from cr.models import Usage

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Rates in $/1M tokens, for cost accounting only.
RATES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

# Minimum cacheable prefix, per model. NOT monotonic across tiers — a 3K-token
# prefix caches on Sonnet 5 and silently does not on Haiku 4.5.
MIN_CACHEABLE: dict[str, int] = {
    "claude-opus-5": 512,
    "claude-sonnet-5": 1024,
    "claude-haiku-4-5": 4096,
}


@dataclass
class Call:
    """One model call's result plus its accounting."""

    parsed: BaseModel | None
    usage: Usage
    model: str
    label: str = ""


class ClientPool:
    """Routes each model to its configured transport. A no-op when one endpoint
    serves everything; Foundry deployments can sit behind different resources."""

    def __init__(self, default: Any, by_model: dict[str, Any] | None = None) -> None:
        self._default = default
        self._by_model = by_model or {}

    def for_model(self, model: str) -> Any:
        return self._by_model.get(model, self._default)

    @property
    def endpoints(self) -> dict[str, Any]:
        """Distinct clients, keyed by the model that selects them. Used by doctor."""
        out = dict(self._by_model)
        out.setdefault("(default)", self._default)
        return out


def _foundry(api_key: str | None, base_url: str | None, resource: str | None) -> Any:
    if not api_key:
        raise ValueError("CR_AZURE_API_KEY is required when CR_PROVIDER=foundry")
    if not (resource or base_url):
        raise ValueError("Set CR_AZURE_RESOURCE (or CR_AZURE_BASE_URL) for Foundry")
    kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    else:
        kwargs["resource"] = resource
    return AsyncAnthropicFoundry(**kwargs)


def build_pool(settings: Any) -> ClientPool:
    """Build the per-model transport map from settings."""
    provider = (getattr(settings, "provider", "anthropic") or "anthropic").lower()
    if provider != "foundry":
        return ClientPool(build_client(settings))

    finder_models = {settings.model_small, settings.model_standard, settings.model_deep}

    default = _foundry(settings.azure_api_key, settings.azure_base_url, settings.azure_resource)

    by_model: dict[str, Any] = {}
    for role, models in (
        ("finder", finder_models),
        ("verifier", {settings.model_verifier}),
    ):
        base, key = settings.endpoint_for(role)
        # Only build a separate client when this role actually differs.
        if (base, key) == (settings.azure_base_url, settings.azure_api_key):
            continue
        client = _foundry(key, base, settings.azure_resource)
        for m in models:
            by_model[m] = client
        log.info("role %s uses a dedicated endpoint (%s)", role, base or settings.azure_resource)

    return ClientPool(default, by_model)


def build_client(settings: Any) -> Any:
    """Pick the transport. Both speak the same Messages API surface.

    Foundry keeps cache_control, structured outputs and the effort ladder; it has
    no Batch API (BACKLOG.md CR-32 is first-party only).
    """
    provider = (getattr(settings, "provider", "anthropic") or "anthropic").lower()

    if provider == "foundry":
        if not settings.azure_api_key:
            raise ValueError("CR_AZURE_API_KEY is required when CR_PROVIDER=foundry")
        if not (settings.azure_resource or settings.azure_base_url):
            raise ValueError("Set CR_AZURE_RESOURCE (or CR_AZURE_BASE_URL) for Foundry")
        kwargs: dict[str, Any] = {"api_key": settings.azure_api_key}
        if settings.azure_base_url:
            kwargs["base_url"] = settings.azure_base_url
        else:
            kwargs["resource"] = settings.azure_resource
        log.info("using Microsoft Foundry (%s)", settings.azure_resource or settings.azure_base_url)
        return AsyncAnthropicFoundry(**kwargs)

    if provider != "anthropic":
        raise ValueError(
            f"Unknown CR_PROVIDER={provider!r}. Supported: 'anthropic', 'foundry'."
        )

    key = settings.anthropic_api_key
    return AsyncAnthropic(api_key=key) if key else AsyncAnthropic()


class LLMClient:
    """Thin wrapper. One instance per review run."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        max_concurrency: int = 8,
        client: Any | None = None,
        pool: ClientPool | None = None,
    ) -> None:
        if pool is not None:
            self._pool = pool
        elif client is not None:
            self._pool = ClientPool(client)
        else:
            base = AsyncAnthropic(api_key=api_key) if api_key else AsyncAnthropic()
            self._pool = ClientPool(base)
        self._sem = asyncio.Semaphore(max_concurrency)
        self.usage = Usage()

    def _for(self, model: str) -> Any:
        return self._pool.for_model(model)

    def _track(self, raw: Any) -> Usage:
        u = Usage(
            input_tokens=getattr(raw, "input_tokens", 0) or 0,
            output_tokens=getattr(raw, "output_tokens", 0) or 0,
            cache_creation_input_tokens=getattr(raw, "cache_creation_input_tokens", 0) or 0,
            cache_read_input_tokens=getattr(raw, "cache_read_input_tokens", 0) or 0,
        )
        self.usage = self.usage + u
        return u

    async def warm(self, payload: dict[str, Any], model: str) -> Usage:
        """Write the repo-level prefix to cache without generating output.

        Singleflight this per repo — see PIPELINE.md §4.2.
        """
        async with self._sem:
            resp = await self._for(model).messages.create(model=model, **payload)
        u = self._track(resp.usage)
        log.info("prewarm model=%s cache_write=%d", model, u.cache_creation_input_tokens)
        return u

    async def parse(
        self,
        *,
        model: str,
        schema: type[T],
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        effort: str = "high",
        max_tokens: int = 16000,
        label: str = "",
    ) -> Call:
        """One structured-output call. Schema is enforced API-side, so no
        JSON-scraping. Never disable thinking to save money — lower `effort`."""
        async with self._sem:
            resp = await self._for(model).messages.parse(
                model=model,
                max_tokens=max_tokens,
                output_format=schema,
                system=system,
                messages=messages,
                output_config={"effort": effort},
            )
        u = self._track(resp.usage)
        log.debug(
            "call label=%s model=%s in=%d out=%d cache_read=%d",
            label,
            model,
            u.input_tokens,
            u.output_tokens,
            u.cache_read_input_tokens,
        )
        return Call(parsed=resp.parsed_output, usage=u, model=model, label=label)

    async def fanout(
        self,
        *,
        model: str,
        schema: type[T],
        system: list[dict[str, Any]],
        message_builder: Any,
        roles: list[tuple[str, str]],
        effort: str = "high",
        max_tokens: int = 16000,
        warm_timeout_s: float = 90.0,
    ) -> list[Call]:
        """Run N passes over one shared cached prefix, staggering the first.

        THE TRAP (PIPELINE.md §2.2): a cache entry is only readable once the first
        response *begins streaming*. Firing all N simultaneously on a cold prefix
        means all N pay full price and the cache is written N times and read zero
        times — with no error and no warning. The 82% saving silently becomes 0%.

        So: start pass #1, wait until its first event proves prefill is done and
        the cache is written, then fan out the rest. Costs ~1.5s of latency.

        `roles` is a list of (label, instruction). `message_builder(instruction)`
        returns the messages array.
        """
        if not roles:
            return []

        cache_ready = asyncio.Event()
        first_label, first_instruction = roles[0]

        async def _first() -> Call:
            try:
                async with self._sem, self._for(model).messages.stream(
                    model=model,
                    max_tokens=max_tokens,
                    output_format=schema,
                    system=system,
                    messages=message_builder(first_instruction),
                    output_config={"effort": effort},
                ) as stream:
                    async for _event in stream:
                        # First event => prefill complete => prefix is cached
                        # and readable by everyone else.
                        cache_ready.set()
                        break
                    final = await stream.get_final_message()
            finally:
                # Never leave the others blocked if this pass dies.
                cache_ready.set()

            u = self._track(final.usage)
            parsed = getattr(final, "parsed_output", None)
            return Call(parsed=parsed, usage=u, model=model, label=first_label)

        task = asyncio.create_task(_first())
        try:
            await asyncio.wait_for(cache_ready.wait(), timeout=warm_timeout_s)
        except TimeoutError:
            log.warning("cache warm timed out after %.0fs; fanning out cold", warm_timeout_s)

        rest = await asyncio.gather(
            *(
                self.parse(
                    model=model,
                    schema=schema,
                    system=system,
                    messages=message_builder(instruction),
                    effort=effort,
                    max_tokens=max_tokens,
                    label=label,
                )
                for label, instruction in roles[1:]
            ),
            return_exceptions=True,
        )

        results: list[Call] = [await task]
        for r in rest:
            if isinstance(r, BaseException):
                log.error("fanout pass failed: %s", r)
                continue
            results.append(r)

        ratio = self.usage.cache_hit_ratio
        if len(roles) > 1 and ratio < 0.2:
            log.warning(
                "cache hit ratio %.1f%% after fanout — suspect a silent invalidator "
                "or a prefix below the model minimum (%d tokens for %s)",
                ratio * 100,
                MIN_CACHEABLE.get(model, 1024),
                model,
            )
        return results

    def cost_usd(self, model: str) -> float:
        in_rate, out_rate = RATES.get(model, (3.00, 15.00))
        return self.usage.cost_usd(in_rate, out_rate)
