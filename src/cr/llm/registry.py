"""The model registry: every provider CR can call, and every model it knows.

One entry per (provider, model) pair. Everything that used to be a loose
per-model dict — rates, cache multipliers, the minimum cacheable prefix, which
models speak the Responses API — is a field here, so adding a model is one
entry instead of five edits that drift apart.

Two rules hold for the whole file:

1. **Base URLs are fixed in code.** No caller supplies a URL. The one endpoint
   that varies per customer (Azure) is built from a resource *name* that is
   validated against Azure's own naming rules before it touches a template, so
   the host is always under a Microsoft domain. User-entered endpoints are
   deliberately out of scope until there is SSRF protection to put behind them.

2. **The curated list only contains models with schema-enforced JSON output.**
   Every stage of the review parses structured output; a model that can only
   promise "valid JSON, probably" is a different code path (deferred).

Prices are list prices in USD per million tokens. Sources and dates are on each
group so a stale number can be traced and refreshed rather than trusted.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from enum import StrEnum

log = logging.getLogger(__name__)


class Wire(StrEnum):
    """The request/response protocol a provider speaks."""

    ANTHROPIC = "anthropic_messages"
    OPENAI_RESPONSES = "openai_responses"
    OPENAI_CHAT = "openai_chat"


class CacheMode(StrEnum):
    """How a model's prompt cache behaves, which decides how CR fans out.

    Only EXPLICIT caching rewards the staggered fan-out in `LLMClient.fanout`:
    there, a cache entry is readable once the first response starts streaming,
    so waiting for it turns N full-price prefills into one write and N-1 reads.
    AUTOMATIC caches benefit from the same stable prefix layout with no extra
    work, and NONE gains nothing from waiting, so both fan out at once.
    """

    EXPLICIT = "explicit"
    AUTOMATIC = "automatic"
    NONE = "none"


class StructuredStyle(StrEnum):
    """How a provider is asked for schema-enforced JSON."""

    ANTHROPIC = "anthropic_output_format"
    JSON_SCHEMA = "json_schema"
    NVEXT_GUIDED_JSON = "nvext_guided_json"


class EffortStyle(StrEnum):
    """How a provider is told how hard to reason."""

    ANTHROPIC = "output_config.effort"
    RESPONSES = "reasoning.effort"
    REASONING_EFFORT = "reasoning_effort"
    OPENROUTER = "reasoning.effort (OpenRouter)"
    NONE = "none"


# Ordered weakest to strongest. A tier asks for one of these; a model that
# lacks it gets the strongest level it has that does not exceed the request.
EFFORT_ORDER: tuple[str, ...] = ("minimal", "low", "medium", "high", "xhigh", "max")

# Azure resource names: 2-64 characters, letters, digits and hyphens, no
# leading or trailing hyphen. Anything else never reaches a URL template —
# this is the check that keeps a "resource name" from carrying a host or path.
_AZURE_RESOURCE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


class InvalidResourceName(ValueError):
    """An Azure resource name that would not stay inside the URL template."""


def validate_resource(name: str) -> str:
    value = (name or "").strip().lower()
    if not _AZURE_RESOURCE.fullmatch(value) or len(value) < 2:
        raise InvalidResourceName(
            f"{name!r} is not a valid Azure resource name "
            "(2-64 letters, digits or hyphens, no leading or trailing hyphen)"
        )
    return value


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    wire: Wire
    # Fixed. `{resource}` is the only substitution, and only after validation.
    base_url: str
    # The Settings attribute holding this provider's key, for status display
    # and for building the client. Never the key itself.
    key_setting: str
    structured: StructuredStyle
    effort: EffortStyle
    # Parallel calls to this endpoint, on top of the run-wide limit. Low-TPM
    # providers 429 under a six-lens fan-out long before CR's own cap matters.
    max_concurrency: int = 4
    # SDK-level retries. Both SDKs back off on 429/5xx and honour retry-after.
    max_retries: int = 2
    docs_url: str = ""
    # Request fields every call to this provider carries.
    extra_body: dict[str, object] = field(default_factory=dict)
    # Claude on Foundry and Anthropic serve the same model ids; this marks the
    # providers a bare `claude-*` name can resolve to.
    serves_claude: bool = False

    @property
    def needs_resource(self) -> bool:
        return "{resource}" in self.base_url

    def url(self, resource: str | None = None) -> str:
        if not self.needs_resource:
            return self.base_url
        if not resource:
            raise ValueError(f"{self.label} needs CR_AZURE_RESOURCE (the resource name, not a URL)")
        return self.base_url.format(resource=validate_resource(resource))

    @property
    def key_env(self) -> str:
        return f"CR_{self.key_setting.upper()}"


PROVIDERS: dict[str, Provider] = {
    p.id: p
    for p in (
        Provider(
            id="anthropic",
            label="Anthropic",
            wire=Wire.ANTHROPIC,
            base_url="https://api.anthropic.com",
            key_setting="anthropic_api_key",
            structured=StructuredStyle.ANTHROPIC,
            effort=EffortStyle.ANTHROPIC,
            max_concurrency=8,
            docs_url="https://platform.claude.com/docs",
            serves_claude=True,
        ),
        Provider(
            id="foundry",
            label="Claude on Microsoft Foundry",
            wire=Wire.ANTHROPIC,
            base_url="https://{resource}.services.ai.azure.com/anthropic",
            key_setting="azure_api_key",
            structured=StructuredStyle.ANTHROPIC,
            effort=EffortStyle.ANTHROPIC,
            max_concurrency=8,
            docs_url="https://learn.microsoft.com/azure/ai-foundry/",
            serves_claude=True,
        ),
        Provider(
            id="openai",
            label="OpenAI",
            wire=Wire.OPENAI_RESPONSES,
            base_url="https://api.openai.com/v1",
            key_setting="openai_api_key",
            structured=StructuredStyle.JSON_SCHEMA,
            effort=EffortStyle.RESPONSES,
            max_concurrency=8,
            docs_url="https://platform.openai.com/docs",
        ),
        Provider(
            id="azure_openai",
            label="Azure OpenAI (Foundry)",
            wire=Wire.OPENAI_RESPONSES,
            base_url="https://{resource}.services.ai.azure.com/openai/v1",
            key_setting="azure_openai_api_key",
            structured=StructuredStyle.JSON_SCHEMA,
            effort=EffortStyle.RESPONSES,
            max_concurrency=8,
            docs_url="https://learn.microsoft.com/azure/ai-foundry/openai/",
        ),
        Provider(
            id="openrouter",
            label="OpenRouter",
            wire=Wire.OPENAI_CHAT,
            base_url="https://openrouter.ai/api/v1",
            key_setting="openrouter_api_key",
            structured=StructuredStyle.JSON_SCHEMA,
            effort=EffortStyle.OPENROUTER,
            max_concurrency=6,
            max_retries=3,
            docs_url="https://openrouter.ai/docs",
            # Route only to upstream endpoints that honour every parameter we
            # send — otherwise `response_format` can be silently dropped by a
            # provider that does not support it, and the JSON is unenforced.
            extra_body={"provider": {"require_parameters": True}},
        ),
        Provider(
            id="groq",
            label="Groq",
            wire=Wire.OPENAI_CHAT,
            base_url="https://api.groq.com/openai/v1",
            key_setting="groq_api_key",
            structured=StructuredStyle.JSON_SCHEMA,
            effort=EffortStyle.REASONING_EFFORT,
            # Groq's per-minute token limits are tight; a full fan-out 429s.
            max_concurrency=2,
            max_retries=4,
            docs_url="https://console.groq.com/docs",
        ),
        Provider(
            id="nvidia",
            label="NVIDIA NIM",
            wire=Wire.OPENAI_CHAT,
            base_url="https://integrate.api.nvidia.com/v1",
            key_setting="nvidia_api_key",
            structured=StructuredStyle.NVEXT_GUIDED_JSON,
            effort=EffortStyle.NONE,
            max_concurrency=2,
            max_retries=4,
            docs_url="https://docs.nvidia.com/nim/large-language-models/",
        ),
    )
}


@dataclass(frozen=True)
class Pricing:
    """USD per million tokens, plus cache multipliers on the input rate."""

    input: float
    output: float
    cache_write: float = 1.0
    cache_read: float = 1.0
    source: str = ""


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    # The name sent on the wire. On Foundry this is the deployment name.
    model: str
    label: str
    vendor: str
    # None means no published per-token price (e.g. NVIDIA's trial credits).
    # Such a model is tracked in tokens and shown as unpriced, never as $0.
    pricing: Pricing | None
    cache: CacheMode = CacheMode.NONE
    min_cacheable: int = 0
    context_window: int | None = None
    max_output: int | None = None
    effort_levels: tuple[str, ...] = ()
    # False for a name the registry does not list: priced with conservative
    # defaults and shown as unverified, so an operator notices.
    known: bool = True
    # Set when the entry's structured-output support has not been confirmed
    # against the live endpoint. `cr doctor` is how it gets confirmed.
    verified: bool = True
    note: str = ""
    # Which client serves this model. Empty means the provider's own client;
    # a customer connection sets it, so two Azure resources stay separate.
    endpoint: str = ""

    @property
    def ref(self) -> str:
        """Unambiguous identifier: `provider:model`."""
        return f"{self.provider}:{self.model}"

    @property
    def wire(self) -> Wire:
        return PROVIDERS[self.provider].wire

    @property
    def priced(self) -> bool:
        return self.pricing is not None

    @property
    def supports_effort(self) -> bool:
        return bool(self.effort_levels) and PROVIDERS[self.provider].effort is not EffortStyle.NONE

    def clamp_effort(self, requested: str | None) -> str | None:
        """The effort to actually send, or None to send nothing.

        A tier is written once and may land on any model, so asking for
        `xhigh` on a model whose ceiling is `high` must degrade, not 400.
        """
        if not requested or not self.supports_effort:
            return None
        if requested in self.effort_levels:
            return requested
        want = (
            EFFORT_ORDER.index(requested)
            if requested in EFFORT_ORDER
            else EFFORT_ORDER.index("high")
        )
        ranked = sorted(
            (lvl for lvl in self.effort_levels if lvl in EFFORT_ORDER), key=EFFORT_ORDER.index
        )
        below = [lvl for lvl in ranked if EFFORT_ORDER.index(lvl) <= want]
        if below:
            return below[-1]
        return ranked[0] if ranked else None

    def clamp_max_tokens(self, requested: int) -> int:
        return min(requested, self.max_output) if self.max_output else requested

    def cost_usd(self, usage: object) -> float:
        """Price a `Usage`. Unpriced models cost nothing *that we can see*;
        callers that need to tell the difference check `priced`."""
        if self.pricing is None:
            return 0.0
        p = self.pricing
        return usage.cost_usd(  # type: ignore[attr-defined]
            p.input,
            p.output,
            cache_write_multiplier=p.cache_write,
            cache_read_multiplier=p.cache_read,
        )


# --- the catalog -------------------------------------------------------------

_CLAUDE_SOURCE = "Anthropic list price, 2026-09-25 (Foundry bills at the same rate)"
_CLAUDE_EFFORT = ("low", "medium", "high", "xhigh", "max")


def _claude(
    model: str,
    label: str,
    price_in: float,
    price_out: float,
    cache_read: float,
    *,
    min_cacheable: int,
    context: int = 1_000_000,
    max_output: int = 128_000,
    effort: tuple[str, ...] = _CLAUDE_EFFORT,
    note: str = "",
) -> list[ModelSpec]:
    """One Claude model, offered by both Claude endpoints at the same price."""
    pricing = Pricing(
        price_in, price_out, cache_write=1.25, cache_read=cache_read, source=_CLAUDE_SOURCE
    )
    return [
        ModelSpec(
            provider=provider,
            model=model,
            label=label,
            vendor="Anthropic",
            pricing=pricing,
            cache=CacheMode.EXPLICIT,
            min_cacheable=min_cacheable,
            context_window=context,
            max_output=max_output,
            effort_levels=effort,
            note=note,
        )
        for provider in ("anthropic", "foundry")
    ]


_OPENAI_SOURCE = "OpenAI list price via OpenRouter catalog, 2026-10-06"
_REASONING_3 = ("low", "medium", "high")


def _openai(
    provider: str,
    model: str,
    label: str,
    price_in: float,
    price_out: float,
    *,
    cache_read: float = 0.10,
    source: str = _OPENAI_SOURCE,
    note: str = "",
) -> ModelSpec:
    return ModelSpec(
        provider=provider,
        model=model,
        label=label,
        vendor="OpenAI",
        pricing=Pricing(price_in, price_out, cache_read=cache_read, source=source),
        cache=CacheMode.AUTOMATIC,
        context_window=1_050_000,
        max_output=128_000,
        effort_levels=_REASONING_3,
        note=note,
    )


_OPENROUTER_SOURCE = "OpenRouter /api/v1/models, 2026-10-06"


def _openrouter(
    model: str,
    label: str,
    vendor: str,
    price_in: float,
    price_out: float,
    cache_read_per_m: float,
    *,
    context: int,
    max_output: int,
) -> ModelSpec:
    return ModelSpec(
        provider="openrouter",
        model=model,
        label=label,
        vendor=vendor,
        pricing=Pricing(
            price_in,
            price_out,
            cache_read=round(cache_read_per_m / price_in, 4) if price_in else 1.0,
            source=_OPENROUTER_SOURCE,
        ),
        # OpenRouter bills cached reads where the upstream caches on its own;
        # nothing here is explicit, so nothing here is worth staggering for.
        cache=CacheMode.AUTOMATIC,
        context_window=context,
        max_output=max_output,
        effort_levels=_REASONING_3,
    )


_GROQ_SOURCE = "Groq model docs, 2026-10-06"


def _groq(
    model: str, label: str, vendor: str, price_in: float, price_out: float, *, max_output: int
) -> ModelSpec:
    return ModelSpec(
        provider="groq",
        model=model,
        label=label,
        vendor=vendor,
        pricing=Pricing(price_in, price_out, source=_GROQ_SOURCE),
        context_window=131_072,
        max_output=max_output,
        effort_levels=_REASONING_3,
        note="Strict JSON schema mode (constrained decoding).",
    )


def _nvidia(model: str, label: str, vendor: str) -> ModelSpec:
    return ModelSpec(
        provider="nvidia",
        model=model,
        label=label,
        vendor=vendor,
        pricing=None,
        verified=False,
        note="Hosted trial endpoint; no per-token list price. Run `cr doctor` before use.",
    )


CATALOG: tuple[ModelSpec, ...] = (
    # Anthropic — model ids, prices, cache minimums and effort levels from the
    # Claude API reference. Claude Opus 5.5 and Fable 5.1 read cache cheaper
    # than the usual 0.1x, which is why cache_read is per model.
    *_claude("claude-fable-5-1", "Claude Fable 5.1", 10.0, 50.0, 0.025, min_cacheable=512),
    *_claude("claude-opus-5-5", "Claude Opus 5.5", 4.0, 20.0, 0.05, min_cacheable=512),
    *_claude("claude-opus-5", "Claude Opus 5", 5.0, 25.0, 0.10, min_cacheable=512),
    *_claude("claude-sonnet-5-5", "Claude Sonnet 5.5", 2.0, 10.0, 0.10, min_cacheable=512),
    *_claude("claude-sonnet-5", "Claude Sonnet 5", 2.0, 10.0, 0.10, min_cacheable=1024),
    *_claude(
        "claude-haiku-4-5",
        "Claude Haiku 4.5",
        1.0,
        5.0,
        0.10,
        min_cacheable=4096,
        context=200_000,
        max_output=64_000,
        # Haiku 4.5 rejects the effort parameter outright.
        effort=(),
        note="No effort control; caches only prefixes of 4,096+ tokens.",
    ),
    # Azure OpenAI — the deployments T4 has always used. Cache pricing on this
    # deployment is unconfirmed for GPT-6 Luna, so it is priced with no
    # discount until an invoice says otherwise.
    _openai(
        "azure_openai",
        "gpt-6-luna",
        "GPT-6 Luna",
        0.10,
        0.50,
        cache_read=1.0,
        source="Azure Foundry list price, 2026-09",
        note="Cache discount unconfirmed on Azure; priced without one.",
    ),
    _openai(
        "azure_openai",
        "gpt-5.6-luna",
        "GPT-5.6 Luna",
        0.20,
        1.20,
        source="Azure Foundry list price, 2026-09",
    ),
    # OpenAI, first party.
    _openai("openai", "gpt-6-astra", "GPT-6 Astra", 10.0, 50.0),
    _openai("openai", "gpt-6-sol", "GPT-6 Sol", 2.0, 10.0, cache_read=0.10),
    _openai("openai", "gpt-6-luna", "GPT-6 Luna", 0.10, 0.50),
    _openai("openai", "gpt-5.6-terra", "GPT-5.6 Terra", 2.0, 12.0),
    _openai("openai", "gpt-5.6-luna", "GPT-5.6 Luna", 0.20, 1.20),
    # OpenRouter — only ids whose `supported_parameters` include
    # `structured_outputs`. Prices are OpenRouter's, which pass through the
    # upstream list price.
    _openrouter(
        "anthropic/claude-sonnet-5.5",
        "Claude Sonnet 5.5",
        "Anthropic",
        2.0,
        10.0,
        0.20,
        context=1_000_000,
        max_output=128_000,
    ),
    _openrouter(
        "anthropic/claude-opus-5.5",
        "Claude Opus 5.5",
        "Anthropic",
        4.0,
        20.0,
        0.20,
        context=1_000_000,
        max_output=128_000,
    ),
    _openrouter(
        "openai/gpt-6-sol",
        "GPT-6 Sol",
        "OpenAI",
        2.0,
        10.0,
        0.20,
        context=1_050_000,
        max_output=128_000,
    ),
    _openrouter(
        "openai/gpt-6-luna",
        "GPT-6 Luna",
        "OpenAI",
        0.10,
        0.50,
        0.01,
        context=1_050_000,
        max_output=128_000,
    ),
    _openrouter(
        "google/gemini-3.8-flash",
        "Gemini 3.8 Flash",
        "Google",
        0.75,
        3.75,
        0.075,
        context=1_048_576,
        max_output=65_536,
    ),
    _openrouter(
        "qwen/qwen3.8-27b",
        "Qwen 3.8 27B",
        "Qwen",
        0.425,
        2.55,
        0.085,
        context=1_000_000,
        max_output=131_072,
    ),
    _openrouter(
        "deepseek/deepseek-v4.1-flash",
        "DeepSeek V4.1 Flash",
        "DeepSeek",
        0.05,
        1.32,
        0.016,
        context=1_048_576,
        max_output=131_072,
    ),
    _openrouter(
        "x-ai/grok-4.7",
        "Grok 4.7",
        "xAI",
        2.0,
        6.0,
        0.50,
        context=500_000,
        max_output=131_072,
    ),
    # Groq — the only models Groq runs in strict (constrained) schema mode.
    _groq("openai/gpt-oss-120b", "GPT-OSS 120B", "OpenAI", 0.15, 0.60, max_output=65_536),
    _groq("openai/gpt-oss-20b", "GPT-OSS 20B", "OpenAI", 0.075, 0.30, max_output=65_536),
    _groq("qwen/qwen3.8-27b", "Qwen 3.8 27B", "Qwen", 0.80, 4.00, max_output=16_384),
    # NVIDIA — guided decoding via `nvext.guided_json`. Not yet confirmed
    # against the hosted endpoint, so these are listed as unverified.
    _nvidia("openai/gpt-oss-20b", "GPT-OSS 20B", "OpenAI"),
    _nvidia("nvidia/nemotron-3-super-120b-a12b", "Nemotron 3 Super 120B", "NVIDIA"),
    _nvidia("deepseek-ai/deepseek-v4.1-flash", "DeepSeek V4.1 Flash", "DeepSeek"),
)

_BY_REF: dict[str, ModelSpec] = {m.ref: m for m in CATALOG}
assert len(_BY_REF) == len(CATALOG), "duplicate provider:model in the catalog"
assert all(m.provider in PROVIDERS for m in CATALOG), "catalog names an unknown provider"

# A bare name (no `provider:` prefix) is how every existing setting and tier
# names a model. It resolves to the first provider in this order that offers
# it — Claude's endpoint first, then Azure, which is where T4's bare
# `gpt-6-luna` has always lived.
_BARE_PRECEDENCE: tuple[str, ...] = ("azure_openai", "openai", "groq", "openrouter", "nvidia")

# Conservative defaults for a name the catalog does not list, matching what
# the old flat rate table charged for an unknown model.
_FALLBACK_PRICING = Pricing(
    3.0, 15.0, cache_write=1.25, cache_read=0.10, source="fallback (unlisted model)"
)

_warned: set[str] = set()


def split_ref(name: str) -> tuple[str | None, str]:
    """`provider:model` -> (provider, model). A bare name -> (None, name).

    Only a known provider id counts as a prefix, so a model name that happens
    to contain a colon is not misread.
    """
    head, sep, tail = name.partition(":")
    if sep and head in PROVIDERS:
        return head, tail
    return None, name


def resolve(name: str, *, claude_provider: str = "anthropic") -> ModelSpec:
    """The spec for a model as named in settings or a tier.

    `claude_provider` is the Claude endpoint this deployment uses
    (`Settings.provider`): a bare `claude-sonnet-5` means "Sonnet 5, on
    whichever Claude endpoint we are configured for".
    """
    provider, model = split_ref(name)
    if provider is not None:
        spec = _BY_REF.get(f"{provider}:{model}")
        return spec or _unlisted(provider, model)

    order = (claude_provider, *(p for p in _BARE_PRECEDENCE if p != claude_provider))
    for candidate in order:
        spec = _BY_REF.get(f"{candidate}:{model}")
        if spec is not None:
            return spec
    # An unlisted bare name is most likely a Foundry deployment someone named
    # themselves, so it goes to the Claude endpoint — the pre-registry
    # behaviour — rather than failing a review over a naming choice.
    return _unlisted(claude_provider, model)


def _unlisted(provider: str, model: str) -> ModelSpec:
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r} in model {provider}:{model}")
    ref = f"{provider}:{model}"
    if ref not in _warned:
        _warned.add(ref)
        log.warning(
            "model %s is not in the registry: pricing it at the fallback rate and "
            "assuming structured-output support — add it to llm/registry.py",
            ref,
        )
    wire = PROVIDERS[provider].wire
    claude_like = wire is Wire.ANTHROPIC
    return ModelSpec(
        provider=provider,
        model=model,
        label=model,
        vendor="Unknown",
        pricing=_FALLBACK_PRICING,
        cache=CacheMode.EXPLICIT if claude_like else CacheMode.NONE,
        min_cacheable=1024 if claude_like else 0,
        effort_levels=_CLAUDE_EFFORT if claude_like else _REASONING_3,
        known=False,
        verified=False,
    )


def catalog() -> tuple[ModelSpec, ...]:
    return CATALOG


def custom_spec(
    provider: str,
    model: str,
    *,
    endpoint: str = "",
    effort: bool | None = None,
    price: tuple[float, float] | None = None,
) -> ModelSpec:
    """Capabilities for a model a customer named on their own connection.

    A name the catalog lists for that provider keeps its known profile —
    pricing, cache behaviour, limits. Any other name (an Azure deployment,
    a fine-tune, a model released last week) gets the provider's defaults and
    no price: the customer's provider bills it, and CR tracks tokens only.

    `effort` is what a connection test learned: False when the model rejected
    the reasoning-effort parameter, so it is never sent again.

    `price` is the customer's own (input, output) USD per million tokens. It
    wins over the catalog, since a customer may have negotiated rates, and is
    the only price an unlisted model has.
    """
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}")
    listed = _BY_REF.get(f"{provider}:{model}")
    if listed is not None:
        spec = replace(listed, endpoint=endpoint)
    else:
        claude_like = PROVIDERS[provider].wire is Wire.ANTHROPIC
        spec = ModelSpec(
            provider=provider,
            model=model,
            label=model,
            vendor="Custom",
            pricing=None,
            cache=CacheMode.EXPLICIT
            if claude_like
            else CacheMode.AUTOMATIC
            if PROVIDERS[provider].wire is Wire.OPENAI_RESPONSES
            else CacheMode.NONE,
            min_cacheable=1024 if claude_like else 0,
            effort_levels=_CLAUDE_EFFORT if claude_like else _REASONING_3,
            known=False,
            verified=False,
            endpoint=endpoint,
        )
    if effort is False:
        spec = replace(spec, effort_levels=())
    if price is not None:
        listed_cache = spec.pricing.cache_read if spec.pricing else 1.0
        spec = replace(
            spec,
            pricing=Pricing(price[0], price[1], cache_read=listed_cache, source="Your price"),
        )
    return spec
