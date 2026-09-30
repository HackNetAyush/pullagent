"""T4's finder transport: OpenAI Responses API, for Azure-Foundry-hosted
first-party OpenAI models (currently GPT-6 Luna).

Deliberately NOT a generalisation of `LLMClient`/`ClientPool` (see client.py's
module docstring on why the Anthropic path stays hand-rolled): this is a third
wire protocol, used by exactly one tier, with no shared-prefix cache to
stagger for (see `find_via_responses`). Teaching the Anthropic hot path to be
polymorphic across three protocols for that would be a bigger, riskier change
than keeping this as its own small path.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel

from cr.config import Settings, TierConfig
from cr.llm.client import Call, LLMClient
from cr.llm.prefix import PrefixBuilder
from cr.models import Finding, FindingList, Usage
from cr.review import prompts

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Models that speak the Responses API rather than Anthropic's Messages API.
# `find()` in review/engine.py branches on this set.
# RESPONSES_API_MODELS: frozenset[str] = frozenset({"gpt-6-luna"})
RESPONSES_API_MODELS = frozenset({"gpt-6-luna", "gpt-5.6-luna"})


def to_responses_input(
    system_blocks: list[dict[str, Any]], message_blocks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Anthropic-shaped `PrefixBuilder.system()`/`.messages()` blocks -> a flat
    Responses API `input=` list.

    Pure format translation: PrefixBuilder stays the single source of prompt
    content. `cache_control` blocks have no equivalent here and are dropped by
    only reading `["text"]` off each block, not the whole dict.
    """
    system_text = "\n\n".join(b["text"] for b in system_blocks)
    items: list[dict[str, Any]] = [
        {"role": "system", "content": [{"type": "input_text", "text": system_text}]}
    ]
    for msg in message_blocks:
        parts = [{"type": "input_text", "text": c["text"]} for c in msg["content"]]
        items.append({"role": msg["role"], "content": parts})
    return items


def _strict_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """Pydantic schema -> OpenAI strict `json_schema` shape: every object gets
    `additionalProperties: false` and every property listed in `required`
    (optionality is expressed via a nullable type, not by omission — this is
    the same convention `openai-python`'s own `.parse()` helper uses, which we
    can't rely on directly here; see `ResponsesAdapter.structured_call`)."""
    raw = schema.model_json_schema()

    def tighten(node: Any) -> None:
        if isinstance(node, dict):
            props = node.get("properties")
            if props is not None:
                node["additionalProperties"] = False
                node["required"] = list(props.keys())
            for v in node.values():
                tighten(v)
        elif isinstance(node, list):
            for item in node:
                tighten(item)

    tighten(raw)
    return raw


def _usage_from_responses(raw: Any) -> Usage:
    """Maps Responses API usage onto our `Usage` shape.

    `input_tokens_details.cached_tokens` (if the endpoint reports it) is a
    *subset* of `input_tokens`, unlike Anthropic where the two are already
    disjoint fields — so it's subtracted out here before splitting into our
    two separate buckets. The observed count is still recorded as a normal
    cache read, for visibility in `cr doctor`/`_render` — but
    `client.CACHE_MULTIPLIERS` prices this model's cache reads at 1.0x (no
    discount) until we've confirmed a real rate for this deployment, so
    recording it doesn't silently borrow Anthropic's 0.10x economics.
    """
    details = getattr(raw, "input_tokens_details", None)
    cached = (getattr(details, "cached_tokens", 0) or 0) if details else 0
    total_input = getattr(raw, "input_tokens", 0) or 0
    return Usage(
        input_tokens=total_input - cached,
        output_tokens=getattr(raw, "output_tokens", 0) or 0,
        cache_read_input_tokens=cached,
    )


class ResponsesAdapter:
    """Just what `find_via_responses` (and doctor.py's T4 probe) need — not a
    general-purpose client."""

    def __init__(self, api_key: str, base_url: str | None) -> None:
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def structured_call(
        self,
        *,
        model: str,
        schema: type[T],
        input: list[dict[str, Any]],
        effort: str,
        max_tokens: int,
        label: str = "",
    ) -> Call:
        """Raw `responses.create`, not the `.parse()` convenience wrapper:
        `openai-python` issue #2532 — `.parse()` can error when Pydantic
        structured output and `reasoning.effort` are combined. We build the
        strict JSON schema and validate the returned JSON ourselves instead."""
        resp = await self._client.responses.create(
            model=model,
            input=input,
            text={
                "format": {
                    "type": "json_schema",
                    "name": schema.__name__,
                    "schema": _strict_schema(schema),
                    "strict": True,
                }
            },
            reasoning={"effort": effort},
            max_output_tokens=max_tokens,
        )
        parsed = schema.model_validate_json(resp.output_text)
        usage = _usage_from_responses(resp.usage)
        return Call(parsed=parsed, usage=usage, model=model, label=label)


def build_adapter(settings: Settings) -> ResponsesAdapter:
    base_url, api_key = settings.openai_endpoint()
    if not api_key:
        raise ValueError(
            "CR_OPENAI_API_KEY (or CR_AZURE_API_KEY) is required for Responses-API models"
        )
    return ResponsesAdapter(api_key=api_key, base_url=base_url)


async def find_via_responses(
    llm: LLMClient,
    adapter: ResponsesAdapter,
    builder: PrefixBuilder,
    tier: TierConfig,
    max_tokens: int,
) -> list[Finding]:
    """T4's finder stage.

    No shared-prefix cache exists across this provider boundary (there's no
    Responses-API equivalent of the "first stream event proves the cache is
    warm" signal `LLMClient.fanout` relies on — see its docstring), so every
    lens runs as an independent, full-price, concurrent call. Same output
    contract as `engine.find()`, so `prefilter()`/`verify()` need no changes.
    """
    roles = [(lens, prompts.finder_instruction(lens)) for lens in tier.finders]

    async def one(label: str, instruction: str) -> Call:
        input_ = to_responses_input(builder.system(), builder.messages(instruction))
        started = time.monotonic()
        try:
            async with llm._sem:
                call = await adapter.structured_call(
                    model=tier.model,
                    schema=FindingList,
                    input=input_,
                    effort=tier.effort,
                    max_tokens=max_tokens,
                    label=label,
                )
            llm.trace(label, tier.model, started, call.usage)
            return call
        except Exception as exc:
            llm.trace(label, tier.model, started, error=type(exc).__name__)
            raise

    results = await asyncio.gather(
        *(one(label, instruction) for label, instruction in roles),
        return_exceptions=True,
    )

    out: list[Finding] = []
    for r in results:
        if isinstance(r, BaseException):
            log.error("T4 finder pass failed: %s", r)
            continue
        llm.record(r.model, r.usage)
        if isinstance(r.parsed, FindingList):
            for f in r.parsed.findings:
                f.found_by = r.label
                out.append(f)
    return out
