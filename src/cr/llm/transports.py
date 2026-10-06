"""Provider transports: one structured call, three wire protocols.

Every stage of the review — finders, merge, verifiers, adjudication, replies —
asks for the same thing: "here is a system prompt and a message, give me back
an instance of this Pydantic schema". A transport turns that request into one
provider's wire format and the answer back into a `Completion`.

Transports translate; they do not decide. Whether truncation is an error, how
usage is priced, when to stagger a fan-out — all of that stays in `LLMClient`,
so the policy is written once rather than once per provider.

Prompt content is always built by `PrefixBuilder` in Anthropic's block shape
(the shape the cache layout is designed around). The OpenAI transports flatten
it in order, which keeps the stable prefix first — exactly what providers with
automatic prefix caching reward.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel

from cr.llm.registry import (
    PROVIDERS,
    EffortStyle,
    ModelSpec,
    Provider,
    StructuredStyle,
    Wire,
    validate_resource,
)
from cr.models import Usage


@dataclass
class Request:
    spec: ModelSpec
    schema: type[BaseModel]
    system: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    # Already clamped to what the model supports; None means "send nothing".
    effort: str | None
    max_tokens: int
    # Called once the provider has started answering. For explicit caching
    # that moment is when the prefix becomes readable by other calls.
    on_started: Callable[[], None] | None = None


@dataclass
class Completion:
    parsed: BaseModel | None
    usage: Usage
    # The answer was cut off by the output limit before it completed.
    truncated: bool = False
    # The model declined; the provider's explanation, verbatim.
    refusal: str | None = None
    # Why `parsed` is None when it is neither truncated nor refused.
    parse_error: str = ""


class Transport(Protocol):
    async def complete(self, req: Request) -> Completion: ...


# --- Anthropic Messages (Anthropic, Claude on Foundry) -----------------------


def anthropic_usage(raw: Any) -> Usage:
    return Usage(
        input_tokens=getattr(raw, "input_tokens", 0) or 0,
        output_tokens=getattr(raw, "output_tokens", 0) or 0,
        cache_creation_input_tokens=getattr(raw, "cache_creation_input_tokens", 0) or 0,
        cache_read_input_tokens=getattr(raw, "cache_read_input_tokens", 0) or 0,
        cache_creation_1h_input_tokens=getattr(
            getattr(raw, "cache_creation", None), "ephemeral_1h_input_tokens", 0
        )
        or 0,
    )


class AnthropicTransport:
    """Always streams: the SDK's non-streaming path refuses, client-side, any
    `max_tokens` that implies more than ten minutes of worst-case generation,
    and the finder budgets are well past that line."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def complete(self, req: Request) -> Completion:
        kwargs: dict[str, Any] = {
            "model": req.spec.model,
            "max_tokens": req.max_tokens,
            "output_format": req.schema,
            "system": req.system,
            "messages": req.messages,
        }
        if req.effort:
            kwargs["output_config"] = {"effort": req.effort}
        async with self.client.messages.stream(**kwargs) as stream:
            if req.on_started is not None:
                async for _event in stream:
                    # First event => prefill is done and the prefix is cached.
                    req.on_started()
                    break
            final = await stream.get_final_message()
        stop = getattr(final, "stop_reason", None)
        parsed = getattr(final, "parsed_output", None)
        return Completion(
            parsed=parsed if isinstance(parsed, req.schema) else None,
            usage=anthropic_usage(final.usage),
            truncated=stop == "max_tokens",
            refusal="the model declined to answer" if stop == "refusal" else None,
        )


# --- OpenAI-shaped helpers ----------------------------------------------------


def strict_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """Pydantic -> the JSON schema OpenAI-style strict mode accepts.

    Uses the same transformation `openai-python` applies inside its own
    `.parse()` helpers: every object closed with `additionalProperties: false`,
    every property required (optional fields stay nullable), `None` defaults
    dropped and `$ref`s with siblings inlined. Groq and OpenRouter document
    the same strict-mode rules, so one schema serves all three.
    """
    from openai.lib._pydantic import to_strict_json_schema  # the SDK's own helper

    return to_strict_json_schema(schema)


def _text(blocks: list[dict[str, Any]]) -> str:
    # Only `text` is read, so `cache_control` markers drop away naturally.
    return "\n\n".join(b["text"] for b in blocks if b.get("text"))


def to_responses_input(
    system_blocks: list[dict[str, Any]], message_blocks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """PrefixBuilder blocks -> a Responses API `input=` list."""
    items: list[dict[str, Any]] = [
        {"role": "system", "content": [{"type": "input_text", "text": _text(system_blocks)}]}
    ]
    for msg in message_blocks:
        parts = [{"type": "input_text", "text": c["text"]} for c in msg["content"]]
        items.append({"role": msg["role"], "content": parts})
    return items


def to_chat_messages(
    system_blocks: list[dict[str, Any]], message_blocks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """PrefixBuilder blocks -> Chat Completions `messages=`. Plain strings, not
    content-part arrays: every OpenAI-compatible server accepts those."""
    out: list[dict[str, Any]] = [{"role": "system", "content": _text(system_blocks)}]
    for msg in message_blocks:
        content = msg["content"]
        out.append(
            {
                "role": msg["role"],
                "content": content if isinstance(content, str) else _text(content),
            }
        )
    return out


def _usage_with_cached_subset(input_total: int, output: int, cached: int) -> Usage:
    """OpenAI-shaped usage reports cached tokens as a *subset* of input
    tokens; ours keeps the two disjoint, as Anthropic's does."""
    return Usage(
        input_tokens=max(input_total - cached, 0),
        output_tokens=output,
        cache_read_input_tokens=cached,
    )


def _parse(schema: type[BaseModel], text: str | None) -> tuple[BaseModel | None, str]:
    if not text:
        return None, "empty response"
    try:
        return schema.model_validate_json(text), ""
    except ValueError as exc:
        return None, f"response did not match {schema.__name__}: {str(exc)[:200]}"


# --- OpenAI Responses (OpenAI, Azure OpenAI) ---------------------------------


class ResponsesTransport:
    """Raw `responses.create`, not `.parse()`: openai-python issue #2532 —
    `.parse()` can fail when structured output and `reasoning.effort` are
    combined. The schema is built and the JSON validated here instead."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def complete(self, req: Request) -> Completion:
        kwargs: dict[str, Any] = {
            "model": req.spec.model,
            "input": to_responses_input(req.system, req.messages),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": req.schema.__name__,
                    "schema": strict_schema(req.schema),
                    "strict": True,
                }
            },
            "max_output_tokens": req.max_tokens,
        }
        if req.effort:
            kwargs["reasoning"] = {"effort": req.effort}
        resp = await self.client.responses.create(**kwargs)
        if req.on_started is not None:
            req.on_started()

        raw = getattr(resp, "usage", None)
        details = getattr(raw, "input_tokens_details", None)
        usage = _usage_with_cached_subset(
            getattr(raw, "input_tokens", 0) or 0,
            getattr(raw, "output_tokens", 0) or 0,
            (getattr(details, "cached_tokens", 0) or 0) if details else 0,
        )
        incomplete = getattr(resp, "incomplete_details", None)
        truncated = (
            getattr(resp, "status", None) == "incomplete"
            and getattr(incomplete, "reason", None) == "max_output_tokens"
        )
        refusal = _responses_refusal(resp)
        if truncated or refusal:
            return Completion(parsed=None, usage=usage, truncated=truncated, refusal=refusal)
        parsed, error = _parse(req.schema, getattr(resp, "output_text", None))
        return Completion(parsed=parsed, usage=usage, parse_error=error)


def _responses_refusal(resp: Any) -> str | None:
    for item in getattr(resp, "output", None) or []:
        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", None) == "refusal":
                return getattr(part, "refusal", None) or "the model declined to answer"
    return None


# --- OpenAI-compatible Chat Completions (OpenRouter, Groq, NVIDIA) -----------


class ChatTransport:
    """Non-streaming on purpose: Groq does not support streaming together with
    structured outputs, and a single code path is easier to trust."""

    def __init__(self, client: Any, provider: Provider) -> None:
        self.client = client
        self.provider = provider

    def build(self, req: Request) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": req.spec.model,
            "messages": to_chat_messages(req.system, req.messages),
            "max_tokens": req.max_tokens,
        }
        extra: dict[str, Any] = json.loads(json.dumps(self.provider.extra_body))
        schema = strict_schema(req.schema)
        if self.provider.structured is StructuredStyle.NVEXT_GUIDED_JSON:
            extra.setdefault("nvext", {})["guided_json"] = schema
        else:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": req.schema.__name__, "schema": schema, "strict": True},
            }
        if req.effort:
            if self.provider.effort is EffortStyle.REASONING_EFFORT:
                kwargs["reasoning_effort"] = req.effort
            elif self.provider.effort is EffortStyle.OPENROUTER:
                extra["reasoning"] = {"effort": req.effort}
        if extra:
            kwargs["extra_body"] = extra
        return kwargs

    async def complete(self, req: Request) -> Completion:
        resp = await self.client.chat.completions.create(**self.build(req))
        if req.on_started is not None:
            req.on_started()

        raw = getattr(resp, "usage", None)
        details = getattr(raw, "prompt_tokens_details", None)
        usage = _usage_with_cached_subset(
            getattr(raw, "prompt_tokens", 0) or 0,
            getattr(raw, "completion_tokens", 0) or 0,
            (getattr(details, "cached_tokens", 0) or 0) if details else 0,
        )
        choices = getattr(resp, "choices", None) or []
        if not choices:
            return Completion(parsed=None, usage=usage, parse_error="response had no choices")
        choice = choices[0]
        message = getattr(choice, "message", None)
        refusal = getattr(message, "refusal", None)
        truncated = getattr(choice, "finish_reason", None) == "length"
        if truncated or refusal:
            return Completion(parsed=None, usage=usage, truncated=truncated, refusal=refusal)
        parsed, error = _parse(req.schema, getattr(message, "content", None))
        return Completion(parsed=parsed, usage=usage, parse_error=error)


# --- construction -------------------------------------------------------------


def _anthropic_http() -> Any:
    # Both SDKs follow redirects by default. A provider endpoint has no reason
    # to redirect an API call, and following one is how a request ends up
    # somewhere the base-URL allow-list never approved.
    from anthropic import DefaultAsyncHttpxClient

    return DefaultAsyncHttpxClient(follow_redirects=False)


def _openai_http() -> Any:
    from openai import DefaultAsyncHttpxClient

    return DefaultAsyncHttpxClient(follow_redirects=False)


def anthropic_client(
    provider_id: str,
    *,
    api_key: str | None,
    resource: str | None = None,
    base_url: str | None = None,
) -> Any:
    """A Claude client for `anthropic` or `foundry`. `base_url` is an operator
    setting (CR_AZURE_BASE_URL), never user input."""
    provider = PROVIDERS[provider_id]
    if provider_id == "foundry":
        from anthropic import AsyncAnthropicFoundry

        if not api_key:
            raise ValueError("CR_AZURE_API_KEY is required when CR_PROVIDER=foundry")
        if not (resource or base_url):
            raise ValueError("Set CR_AZURE_RESOURCE (or CR_AZURE_BASE_URL) for Foundry")
        kwargs: dict[str, Any] = {
            "api_key": api_key,
            "max_retries": provider.max_retries,
            "http_client": _anthropic_http(),
        }
        if base_url:
            kwargs["base_url"] = base_url
        else:
            # The SDK builds the URL from this; validating it here keeps the
            # host under services.ai.azure.com whatever the setting contains.
            kwargs["resource"] = validate_resource(resource or "")
        return AsyncAnthropicFoundry(**kwargs)

    from anthropic import AsyncAnthropic

    kwargs = {"max_retries": provider.max_retries, "http_client": _anthropic_http()}
    if api_key:
        kwargs["api_key"] = api_key
    return AsyncAnthropic(**kwargs)


def openai_client(provider_id: str, *, api_key: str | None, base_url: str) -> Any:
    from openai import AsyncOpenAI

    provider = PROVIDERS[provider_id]
    if not api_key:
        raise ValueError(f"{provider.label} needs {provider.key_env}")
    return AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        max_retries=provider.max_retries,
        http_client=_openai_http(),
    )


def transport_for(provider_id: str, client: Any) -> Transport:
    provider = PROVIDERS[provider_id]
    if provider.wire is Wire.ANTHROPIC:
        return AnthropicTransport(client)
    if provider.wire is Wire.OPENAI_RESPONSES:
        return ResponsesTransport(client)
    return ChatTransport(client, provider)
