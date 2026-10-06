"""The one LLM entry point every review stage uses.

`LLMClient.parse` and `LLMClient.fanout` take a model name and a Pydantic
schema and return a validated instance, whichever provider serves that model.
The registry (`llm/registry.py`) says what a model can do; the transports
(`llm/transports.py`) speak each wire protocol; this module holds the policy
that must be identical everywhere — concurrency, truncation and refusal
handling, cost accounting, and the cache-aware fan-out.

Deliberately native SDKs, not LiteLLM: Claude's cache breakpoints, effort and
thinking controls are what make a review affordable, and an abstraction layer
flattens exactly those away. Each provider keeps its own SDK; only the request
shape is shared.

The important function here is `fanout`. See PIPELINE.md §2.2 "Trap 1".
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel

from cr.llm.registry import PROVIDERS, CacheMode, ModelSpec, Wire, resolve, validate_resource
from cr.llm.transports import (
    Request,
    Transport,
    anthropic_client,
    anthropic_usage,
    openai_client,
    transport_for,
)
from cr.models import CallTrace, Usage

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class OutputBudgetExceeded(ValueError):
    """Reasoning/output exhausted the request before its structured answer completed."""


class ModelRefused(RuntimeError):
    """The model declined to answer. Not retried: the same prompt declines again."""


@dataclass
class Call:
    """One model call's result plus its accounting."""

    parsed: BaseModel | None
    usage: Usage
    model: str
    label: str = ""


class ClientPool:
    """Routes each model to a provider client.

    Claude models go to the configured Claude endpoint (`claude_provider`),
    optionally a different deployment per role — Foundry resources can sit
    behind different keys. Every other provider gets one client, built on
    first use, so a deployment only needs credentials for providers it
    actually calls.
    """

    def __init__(
        self,
        default: Any = None,
        by_model: dict[str, Any] | None = None,
        *,
        claude_provider: str = "anthropic",
        factory: Callable[[str], Any] | None = None,
    ) -> None:
        self._default = default
        self._by_model = by_model or {}
        self.claude_provider = claude_provider
        self._factory = factory
        self._clients: dict[str, Any] = {}

    def client(self, key: str) -> Any:
        """The client for a provider id, or for a customer connection's endpoint key."""
        if key not in self._clients:
            if self._factory is None:
                label = PROVIDERS[key].label if key in PROVIDERS else key
                raise ValueError(f"no client configured for {label}")
            self._clients[key] = self._factory(key)
        return self._clients[key]

    def for_model(self, model: str) -> Any:
        """The Claude-endpoint client for this model name."""
        if model in self._by_model:
            return self._by_model[model]
        if self._default is None:
            self._default = self.client(self.claude_provider)
        return self._default

    def transport(self, spec: ModelSpec) -> Transport:
        if spec.endpoint:
            return transport_for(spec.provider, self.client(spec.endpoint))
        if spec.provider == self.claude_provider:
            return transport_for(spec.provider, self.for_model(spec.model))
        return transport_for(spec.provider, self.client(spec.provider))


def make_client(settings: Any, provider_id: str) -> Any:
    """Build one provider's SDK client from settings. Base URLs come from the
    registry; the only operator overrides are the Azure ones, which are
    deployment configuration, not user input."""
    if provider_id == "anthropic":
        return anthropic_client("anthropic", api_key=settings.anthropic_api_key)
    if provider_id == "foundry":
        return anthropic_client(
            "foundry",
            api_key=settings.azure_api_key,
            resource=settings.azure_resource,
            base_url=settings.azure_base_url,
        )
    provider = PROVIDERS[provider_id]
    if provider_id == "azure_openai":
        base = settings.azure_openai_base_url or provider.url(settings.azure_resource)
        key = settings.azure_openai_api_key or settings.azure_api_key
    else:
        base = provider.url()
        key = getattr(settings, provider.key_setting, None)
    return openai_client(provider_id, api_key=key, base_url=base)


def build_pool(settings: Any) -> ClientPool:
    """The per-deployment client pool."""
    claude = (getattr(settings, "provider", "anthropic") or "anthropic").lower()
    if claude not in ("anthropic", "foundry"):
        raise ValueError(f"Unknown CR_PROVIDER={claude!r}. Supported: 'anthropic', 'foundry'.")

    by_model: dict[str, Any] = {}
    if claude == "foundry":
        finder_models = {settings.model_small, settings.model_standard, settings.model_deep}
        for role, models in (("finder", finder_models), ("verifier", {settings.model_verifier})):
            base, key = settings.endpoint_for(role)
            # Only build a separate client when this role actually differs.
            if (base, key) == (settings.azure_base_url, settings.azure_api_key):
                continue
            client = anthropic_client(
                "foundry", api_key=key, resource=settings.azure_resource, base_url=base
            )
            for m in models:
                by_model[m] = client
            log.info(
                "role %s uses a dedicated endpoint (%s)", role, base or settings.azure_resource
            )

    return ClientPool(
        None, by_model, claude_provider=claude, factory=lambda pid: make_client(settings, pid)
    )


def provider_status(settings: Any) -> dict[str, dict[str, Any]]:
    """Which providers this deployment can call, without building a client
    and without ever returning a credential."""

    def has(value: Any) -> bool:
        return bool(value) and "FILL ME" not in str(value)

    def resource_ok() -> bool:
        try:
            validate_resource(settings.azure_resource or "")
        except ValueError:
            return False
        return True

    out: dict[str, dict[str, Any]] = {}
    for pid, p in PROVIDERS.items():
        if pid == "anthropic":
            ok = has(settings.anthropic_api_key) or has(os.environ.get("ANTHROPIC_API_KEY"))
            missing = [] if ok else [p.key_env]
        elif pid == "foundry":
            missing = [] if has(settings.azure_api_key) else ["CR_AZURE_API_KEY"]
            if not (has(settings.azure_base_url) or resource_ok()):
                missing.append("CR_AZURE_RESOURCE")
        elif pid == "azure_openai":
            missing = (
                []
                if has(settings.azure_openai_api_key) or has(settings.azure_api_key)
                else ["CR_AZURE_OPENAI_API_KEY"]
            )
            if not (has(settings.azure_openai_base_url) or resource_ok()):
                missing.append("CR_AZURE_RESOURCE")
        else:
            missing = [] if has(getattr(settings, p.key_setting, None)) else [p.key_env]
        out[pid] = {"configured": not missing, "missing": missing}
    return out


class LLMClient:
    """Thin wrapper. One instance per review run."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        max_concurrency: int = 2,
        client: Any | None = None,
        pool: ClientPool | None = None,
        resolver: Callable[[str], ModelSpec] | None = None,
    ) -> None:
        if pool is not None:
            self._pool = pool
        elif client is not None:
            self._pool = ClientPool(client)
        else:
            self._pool = ClientPool(anthropic_client("anthropic", api_key=api_key))
        # Customer connections name models their own way (`conn:<id>:<model>`);
        # a resolver turns those into specs. Without one, the registry does.
        self._resolver = resolver
        self._sem = asyncio.Semaphore(max_concurrency)
        self._provider_sems: dict[str, asyncio.Semaphore] = {}
        self.usage_by_model: dict[str, Usage] = {}
        self.calls: list[CallTrace] = []

    def spec(self, model: str) -> ModelSpec:
        if self._resolver is not None:
            return self._resolver(model)
        return resolve(model, claude_provider=self._pool.claude_provider)

    def _provider_limit(self, spec: ModelSpec) -> asyncio.Semaphore:
        """Per-endpoint cap, under the run-wide one. Always acquired after
        `_sem`, never before, so the two cannot deadlock."""
        key = spec.endpoint or spec.provider
        sem = self._provider_sems.get(key)
        if sem is None:
            sem = asyncio.Semaphore(PROVIDERS[spec.provider].max_concurrency)
            self._provider_sems[key] = sem
        return sem

    def trace(
        self, label: str, model: str, started: float, usage: Usage | None = None, error: str = ""
    ) -> None:
        u = usage or Usage()
        self.calls.append(
            CallTrace(
                label=label,
                model=model,
                usage=u,
                elapsed_s=time.monotonic() - started,
                cost_usd=self.spec(model).cost_usd(u),
                error=error,
            )
        )

    @property
    def usage(self) -> Usage:
        """Aggregate across every model this client has called. Fine for display
        (total tokens), but never price it with one rate — see `total_cost_usd`."""
        total = Usage()
        for u in self.usage_by_model.values():
            total = total + u
        return total

    def record(self, model: str, usage: Usage) -> None:
        """Fold usage into this client's per-model ledger."""
        self.usage_by_model[model] = self.usage_by_model.get(model, Usage()) + usage

    async def warm(self, payload: dict[str, Any], model: str) -> Usage:
        """Write the repo-level prefix to cache without generating output.

        Only meaningful where caching is explicit. Singleflight this per repo —
        see PIPELINE.md §4.2.
        """
        spec = self.spec(model)
        if spec.wire is not Wire.ANTHROPIC or spec.cache is not CacheMode.EXPLICIT:
            return Usage()
        async with self._sem:
            resp = await self._pool.for_model(spec.model).messages.create(
                model=spec.model, **payload
            )
        u = anthropic_usage(resp.usage)
        self.record(model, u)
        log.info("prewarm model=%s cache_write=%d", model, u.cache_creation_input_tokens)
        return u

    async def _complete(
        self,
        *,
        model: str,
        schema: type[T],
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        effort: str,
        max_tokens: int,
        label: str,
        on_started: Callable[[], None] | None = None,
    ) -> Call:
        started = time.monotonic()
        u: Usage | None = None
        spec = self.spec(model)
        req = Request(
            spec=spec,
            schema=schema,
            system=system,
            messages=messages,
            # Effort is sent only where the model takes it, at the strongest
            # level it supports that does not exceed the tier's request.
            effort=spec.clamp_effort(effort),
            max_tokens=spec.clamp_max_tokens(max_tokens),
            on_started=on_started,
        )
        try:
            async with self._sem, self._provider_limit(spec):
                done = await self._pool.transport(spec).complete(req)
            u = done.usage
            self.record(model, u)
            if done.truncated:
                raise OutputBudgetExceeded(f"{label} exhausted max_tokens={req.max_tokens}")
            if done.refusal:
                raise ModelRefused(f"{label}: {done.refusal}")
            if not isinstance(done.parsed, schema):
                raise ValueError(done.parse_error or "missing structured output")
        except Exception as exc:
            self.trace(label, model, started, u, type(exc).__name__)
            raise
        self.trace(label, model, started, u)
        log.debug(
            "call label=%s model=%s in=%d out=%d cache_read=%d",
            label,
            model,
            u.input_tokens,
            u.output_tokens,
            u.cache_read_input_tokens,
        )
        return Call(parsed=done.parsed, usage=u, model=model, label=label)

    async def parse(
        self,
        *,
        model: str,
        schema: type[T],
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        effort: str = "high",
        max_tokens: int = 32000,
        label: str = "",
    ) -> Call:
        """One structured-output call, on whichever provider serves `model`.
        The schema is enforced provider-side, so no JSON-scraping. Never
        disable thinking to save money — lower `effort`."""
        return await self._complete(
            model=model,
            schema=schema,
            system=system,
            messages=messages,
            effort=effort,
            max_tokens=max_tokens,
            label=label,
        )

    async def fanout(
        self,
        *,
        model: str,
        schema: type[T],
        system: list[dict[str, Any]],
        message_builder: Any,
        roles: list[tuple[str, str]],
        effort: str = "high",
        max_tokens: int = 32000,
        warm_timeout_s: float = 90.0,
    ) -> list[Call]:
        """Run N passes over one shared prefix. A failed pass loses one lens,
        never the run.

        THE TRAP (PIPELINE.md §2.2): with explicit caching, a cache entry is
        only readable once the first response *begins streaming*. Firing all N
        simultaneously on a cold prefix means all N pay full price and the
        cache is written N times and read zero times — with no error and no
        warning. So for those models: start pass #1, wait until its first
        event proves prefill is done, then fan out the rest (~1.5s latency).

        Models without explicit caching gain nothing from waiting, so they
        fan out at once.

        `roles` is a list of (label, instruction). `message_builder(instruction)`
        returns the messages array.
        """
        if not roles:
            return []
        spec = self.spec(model)

        def call(label: str, instruction: str) -> Any:
            return self.parse(
                model=model,
                schema=schema,
                system=system,
                messages=message_builder(instruction),
                effort=effort,
                max_tokens=max_tokens,
                label=label,
            )

        if spec.cache is not CacheMode.EXPLICIT:
            outcomes = await asyncio.gather(
                *(call(label, instruction) for label, instruction in roles),
                return_exceptions=True,
            )
            return self._survivors(outcomes)

        cache_ready = asyncio.Event()
        first_label, first_instruction = roles[0]

        async def first() -> Call:
            try:
                return await self._complete(
                    model=model,
                    schema=schema,
                    system=system,
                    messages=message_builder(first_instruction),
                    effort=effort,
                    max_tokens=max_tokens,
                    label=first_label,
                    on_started=cache_ready.set,
                )
            finally:
                # Never leave the others blocked if this pass dies.
                cache_ready.set()

        task = asyncio.create_task(first())
        try:
            await asyncio.wait_for(cache_ready.wait(), timeout=warm_timeout_s)
        except TimeoutError:
            log.warning("cache warm timed out after %.0fs; fanning out cold", warm_timeout_s)

        rest = await asyncio.gather(
            *(call(label, instruction) for label, instruction in roles[1:]),
            return_exceptions=True,
        )
        try:
            head: list[Call | BaseException] = [await task]
        except Exception as e:  # noqa: BLE001
            # Usually a truncated structured output: thinking and the answer
            # share max_tokens, so a large diff can cut the JSON mid-string.
            head = [e]
        results = self._survivors([*head, *rest])

        ratio = self.usage.cache_hit_ratio
        if len(roles) > 1 and ratio < 0.2:
            log.warning(
                "cache hit ratio %.1f%% after fanout — suspect a silent invalidator "
                "or a prefix below the model minimum (%d tokens for %s)",
                ratio * 100,
                spec.min_cacheable,
                model,
            )
        return results

    @staticmethod
    def _survivors(outcomes: list[Any]) -> list[Call]:
        out: list[Call] = []
        for r in outcomes:
            if isinstance(r, BaseException):
                log.error("fanout pass failed: %s", r)
                continue
            out.append(r)
        return out

    def cost_usd(self, model: str) -> float:
        """Cost for one model's tracked usage. Wrong for a whole run if the run
        mixed models (a separate verifier model) — use `total_cost_usd` there."""
        return self.spec(model).cost_usd(self.usage_by_model.get(model, Usage()))

    def total_cost_usd(self) -> float:
        """Sum of every model's cost, each priced at its own rate. This is the
        correct figure for a whole review run."""
        return sum(self.cost_usd(model) for model in self.usage_by_model)
