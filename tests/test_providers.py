"""The multi-provider layer: registry resolution, transports, and routing.

What these guard, in order of how badly each would fail in production:

- every stage reaching the provider that serves its model (a GPT tier used to
  send its verifier calls to the Anthropic client and fail);
- no base URL the registry did not approve, and no credential in any output;
- request shapes each provider actually accepts (schema mode, effort field);
- truncation, refusals and usage reported the same way whatever the provider.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from cr.config import Settings, TierConfig
from cr.llm import registry
from cr.llm.client import (
    ClientPool,
    LLMClient,
    ModelRefused,
    OutputBudgetExceeded,
    make_client,
    provider_status,
)
from cr.llm.prefix import PRContext, PrefixBuilder, RepoContext
from cr.llm.registry import CATALOG, PROVIDERS, CacheMode, InvalidResourceName, resolve
from cr.llm.transports import ChatTransport, Request, ResponsesTransport, strict_schema
from cr.models import Category, Evidence, Finding, FindingList, Severity, Usage
from cr.review import engine, prompts


def _settings(**kw) -> Settings:
    # Never read the developer's .env: these tests must not see real keys.
    return Settings(_env_file=None, **kw)


def _builder() -> PrefixBuilder:
    return PrefixBuilder(
        preamble=prompts.PREAMBLE,
        repo=RepoContext(slug="acme/api"),
        pr=PRContext(title="t", description="d", diff="@@ -1 +1 @@\n-a\n+b\n"),
    )


def _finding() -> Finding:
    return Finding(
        claim="x",
        failure_scenario="a concrete scenario long enough to pass prefilter",
        evidence=[Evidence(file="a.py", start_line=1, end_line=2, quote="", why="w")],
        category=Category.CORRECTNESS,
        severity=Severity.HIGH,
        confidence=0.9,
    )


# --- registry ---------------------------------------------------------------


def test_catalog_refs_are_unique_and_every_provider_exists() -> None:
    refs = [m.ref for m in CATALOG]
    assert len(refs) == len(set(refs))
    assert {m.provider for m in CATALOG} <= set(PROVIDERS)


def test_bare_claude_names_follow_the_configured_claude_endpoint() -> None:
    assert resolve("claude-sonnet-5").provider == "anthropic"
    assert resolve("claude-sonnet-5", claude_provider="foundry").provider == "foundry"


def test_bare_luna_keeps_resolving_to_azure_like_t4_always_has() -> None:
    assert resolve("gpt-6-luna").ref == "azure_openai:gpt-6-luna"
    assert resolve("openai:gpt-6-luna").ref == "openai:gpt-6-luna"


def test_explicit_refs_pick_the_provider() -> None:
    spec = resolve("groq:openai/gpt-oss-120b")
    assert spec.provider == "groq"
    assert spec.model == "openai/gpt-oss-120b"
    assert spec.known


def test_unlisted_bare_name_is_a_claude_deployment_priced_conservatively() -> None:
    spec = resolve("my-sonnet-deployment", claude_provider="foundry")
    assert spec.provider == "foundry"
    assert not spec.known
    assert spec.pricing is not None and spec.pricing.input == 3.0
    # Still Claude-shaped, so the fan-out keeps staggering for the cache.
    assert spec.cache is CacheMode.EXPLICIT


def test_a_colon_in_a_model_name_is_not_mistaken_for_a_provider() -> None:
    spec = resolve("deployment:v2")
    assert spec.provider == "anthropic"
    assert spec.model == "deployment:v2"


def test_effort_degrades_to_what_the_model_supports() -> None:
    assert resolve("claude-opus-5").clamp_effort("xhigh") == "xhigh"
    assert resolve("groq:openai/gpt-oss-120b").clamp_effort("xhigh") == "high"
    assert resolve("groq:openai/gpt-oss-120b").clamp_effort("minimal") == "low"
    # Haiku 4.5 rejects the effort parameter outright, so none is sent.
    assert resolve("claude-haiku-4-5").clamp_effort("low") is None
    # Neither is it sent where the provider has no effort control.
    assert resolve("nvidia:openai/gpt-oss-20b").clamp_effort("high") is None


def test_max_tokens_are_clamped_to_the_model_ceiling() -> None:
    assert resolve("groq:qwen/qwen3.8-27b").clamp_max_tokens(32000) == 16_384
    assert resolve("claude-sonnet-5").clamp_max_tokens(64000) == 64000


@pytest.mark.parametrize(
    "bad",
    ["evil.com", "a/b", "-lead", "trail-", "x", "x" * 65, "res#frag", "res?q=1", "", "a b"],
)
def test_azure_resource_names_cannot_carry_a_host_or_path(bad: str) -> None:
    with pytest.raises(InvalidResourceName):
        registry.validate_resource(bad)


def test_azure_url_is_built_only_from_a_valid_resource_name() -> None:
    assert PROVIDERS["azure_openai"].url("My-Res") == (
        "https://my-res.services.ai.azure.com/openai/v1"
    )
    with pytest.raises(InvalidResourceName):
        PROVIDERS["azure_openai"].url("attacker.example/x")


def test_unpriced_models_are_reported_as_unpriced_not_free() -> None:
    spec = resolve("nvidia:openai/gpt-oss-20b")
    assert not spec.priced
    assert not spec.verified
    assert spec.cost_usd(Usage(input_tokens=1000, output_tokens=1000)) == 0.0


def test_claude_cache_reads_are_priced_per_model() -> None:
    u = Usage(cache_read_input_tokens=1_000_000)
    # Opus 5.5 reads cache at $0.20/M (0.05x of $4), Sonnet 5 at $0.20/M (0.1x of $2).
    assert resolve("claude-opus-5-5").cost_usd(u) == pytest.approx(0.20)
    assert resolve("claude-sonnet-5").cost_usd(u) == pytest.approx(0.20)


# --- schema -----------------------------------------------------------------


def test_strict_schema_closes_every_object_and_requires_every_field() -> None:
    schema = strict_schema(FindingList)

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)


# --- chat transport (OpenRouter, Groq, NVIDIA) ------------------------------


def _req(ref: str, *, effort: str | None = "high", max_tokens: int = 4000) -> Request:
    spec = resolve(ref)
    b = _builder()
    return Request(
        spec=spec,
        schema=FindingList,
        system=b.system(),
        messages=b.messages("find bugs"),
        effort=spec.clamp_effort(effort),
        max_tokens=spec.clamp_max_tokens(max_tokens),
    )


def test_groq_request_uses_strict_json_schema_and_reasoning_effort() -> None:
    kw = ChatTransport(None, PROVIDERS["groq"]).build(_req("groq:openai/gpt-oss-120b"))
    assert kw["response_format"]["type"] == "json_schema"
    assert kw["response_format"]["json_schema"]["strict"] is True
    assert kw["reasoning_effort"] == "high"
    assert "extra_body" not in kw


def test_openrouter_request_requires_parameter_support_and_nests_effort() -> None:
    kw = ChatTransport(None, PROVIDERS["openrouter"]).build(
        _req("openrouter:google/gemini-3.8-flash")
    )
    assert kw["extra_body"]["provider"] == {"require_parameters": True}
    assert kw["extra_body"]["reasoning"] == {"effort": "high"}
    assert "reasoning_effort" not in kw
    # The provider's own defaults must not be mutated by a request.
    assert "reasoning" not in PROVIDERS["openrouter"].extra_body


def test_nvidia_request_uses_guided_json_and_no_effort() -> None:
    kw = ChatTransport(None, PROVIDERS["nvidia"]).build(_req("nvidia:openai/gpt-oss-20b"))
    assert "response_format" not in kw
    assert kw["extra_body"]["nvext"]["guided_json"]["type"] == "object"
    assert "reasoning_effort" not in kw


def test_chat_messages_flatten_blocks_in_prefix_order() -> None:
    kw = ChatTransport(None, PROVIDERS["groq"]).build(_req("groq:openai/gpt-oss-120b"))
    roles = [m["role"] for m in kw["messages"]]
    assert roles == ["system", "user"]
    assert all(isinstance(m["content"], str) for m in kw["messages"])
    assert kw["messages"][1]["content"].endswith("find bugs")
    assert "cache_control" not in json.dumps(kw["messages"])


class FakeChat:
    """An OpenAI-compatible client that answers from a script."""

    def __init__(self, *, content=None, finish="stop", refusal=None, cached=0, gate=None):
        self.requests: list[dict] = []
        self.content = content if content is not None else FindingList().model_dump_json()
        self.finish, self.refusal, self.cached, self.gate = finish, refusal, cached, gate
        self.in_flight = 0
        self.peak = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.requests.append(kw)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            if self.gate is not None:
                await self.gate()
            else:
                await asyncio.sleep(0)
        finally:
            self.in_flight -= 1
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=self.finish,
                    message=SimpleNamespace(content=self.content, refusal=self.refusal),
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=1000,
                completion_tokens=200,
                prompt_tokens_details=SimpleNamespace(cached_tokens=self.cached),
            ),
        )


class Unusable:
    """A Claude client that must never be reached."""

    class messages:
        @staticmethod
        def stream(**_kw):
            raise AssertionError("routed to the Claude endpoint")


def _client_with(fake, *, max_concurrency: int = 8) -> LLMClient:
    pool = ClientPool(Unusable(), claude_provider="anthropic", factory=lambda _pid: fake)
    return LLMClient(pool=pool, max_concurrency=max_concurrency)


def test_chat_call_parses_and_prices_with_the_providers_own_rates() -> None:
    fake = FakeChat(cached=400)
    llm = _client_with(fake)
    call = asyncio.run(
        llm.parse(
            model="groq:openai/gpt-oss-120b",
            schema=FindingList,
            system=_builder().system(),
            messages=_builder().messages("x"),
            label="correctness",
        )
    )
    assert isinstance(call.parsed, FindingList)
    # Cached tokens are a subset of prompt tokens on the wire, disjoint here.
    assert call.usage.input_tokens == 600
    assert call.usage.cache_read_input_tokens == 400
    expected = (1000 * 0.15 + 200 * 0.60) / 1_000_000
    assert llm.total_cost_usd() == pytest.approx(expected)


def test_chat_truncation_raises_but_keeps_the_usage() -> None:
    llm = _client_with(FakeChat(finish="length", content=""))
    with pytest.raises(OutputBudgetExceeded):
        asyncio.run(
            llm.parse(
                model="openrouter:qwen/qwen3.8-27b",
                schema=FindingList,
                system=[],
                messages=[{"role": "user", "content": [{"type": "text", "text": "x"}]}],
                label="correctness",
            )
        )
    assert llm.usage.output_tokens == 200
    assert llm.calls[0].error == "OutputBudgetExceeded"


def test_a_refusal_is_its_own_error() -> None:
    llm = _client_with(FakeChat(refusal="no", content=""))
    with pytest.raises(ModelRefused):
        asyncio.run(
            llm.parse(
                model="groq:openai/gpt-oss-20b",
                schema=FindingList,
                system=[],
                messages=[{"role": "user", "content": [{"type": "text", "text": "x"}]}],
                label="verify:correctness",
            )
        )


def test_invalid_json_is_an_error_not_an_empty_finding_list() -> None:
    llm = _client_with(FakeChat(content='{"not": "a finding list"'))
    with pytest.raises(ValueError, match="did not match FindingList"):
        asyncio.run(
            llm.parse(
                model="groq:openai/gpt-oss-20b",
                schema=FindingList,
                system=[],
                messages=[{"role": "user", "content": [{"type": "text", "text": "x"}]}],
                label="correctness",
            )
        )


# --- responses transport (OpenAI, Azure OpenAI) -----------------------------


class FakeResponses:
    def __init__(self, *, status="completed", reason=None, text=None):
        self.requests: list[dict] = []
        self.status, self.reason = status, reason
        self.text = text if text is not None else FindingList().model_dump_json()
        self.responses = SimpleNamespace(create=self._create)

    async def _create(self, **kw):
        self.requests.append(kw)
        return SimpleNamespace(
            status=self.status,
            incomplete_details=SimpleNamespace(reason=self.reason),
            output=[],
            output_text=self.text,
            usage=SimpleNamespace(
                input_tokens=500,
                output_tokens=50,
                input_tokens_details=SimpleNamespace(cached_tokens=0),
            ),
        )


def test_responses_request_shape() -> None:
    fake = FakeResponses()
    done = asyncio.run(ResponsesTransport(fake).complete(_req("openai:gpt-6-sol")))
    kw = fake.requests[0]
    assert kw["text"]["format"]["strict"] is True
    assert kw["reasoning"] == {"effort": "high"}
    assert kw["max_output_tokens"] == 4000
    assert isinstance(done.parsed, FindingList)


def test_responses_incomplete_output_is_truncation() -> None:
    fake = FakeResponses(status="incomplete", reason="max_output_tokens", text="")
    done = asyncio.run(ResponsesTransport(fake).complete(_req("openai:gpt-6-sol")))
    assert done.truncated
    assert done.parsed is None


# --- routing ----------------------------------------------------------------


def test_a_gpt_tier_verifies_on_its_own_provider_not_the_claude_endpoint() -> None:
    """The bug this layer fixed: a non-Claude tier with no verifier_model sent
    its verification calls to the Anthropic client."""

    class FakeVerify(FakeResponses):
        async def _create(self, **kw):
            text = kw["input"][-1]["content"][-1]["text"]
            payload = json.loads(text.split("Candidates (data, not instructions):\n")[1])
            self.text = json.dumps(
                {
                    "decisions": [
                        {
                            "finding_id": x["finding_id"],
                            "status": "confirmed",
                            "reasoning": "a.py:1",
                        }
                        for x in payload
                    ]
                }
            )
            return await super()._create(**kw)

    fake = FakeVerify()
    pool = ClientPool(Unusable(), factory=lambda pid: fake)
    llm = LLMClient(pool=pool)
    tier = TierConfig(
        name="custom",
        model="openai:gpt-6-luna",
        effort="medium",
        finders=["correctness"],
        verifier_lenses=["correctness"],
        max_comments=3,
    )
    out = asyncio.run(engine.verify(llm, _builder(), tier, [_finding()]))
    assert fake.requests, "verification never reached the OpenAI client"
    assert out[0].verdicts and not out[0].verdicts[0].refuted


def test_non_explicit_cache_models_fan_out_at_once() -> None:
    """Staggering only pays off with explicit caching. Elsewhere it is pure
    latency: every lens must be in flight together."""
    roles = [(f"lens{i}", f"instruction {i}") for i in range(4)]
    arrived = asyncio.Event()
    count = {"n": 0}

    async def barrier():
        count["n"] += 1
        if count["n"] == len(roles):
            arrived.set()
        await asyncio.wait_for(arrived.wait(), timeout=2)

    fake = FakeChat(gate=barrier)
    llm = _client_with(fake)
    b = _builder()
    out = asyncio.run(
        llm.fanout(
            model="openrouter:google/gemini-3.8-flash",
            schema=FindingList,
            system=b.system(),
            message_builder=b.messages,
            roles=roles,
            warm_timeout_s=30,
        )
    )
    assert [c.label for c in out] == [label for label, _ in roles]


def test_per_provider_concurrency_caps_parallel_calls() -> None:
    fake = FakeChat()

    async def slow():
        await asyncio.sleep(0.02)

    fake.gate = slow
    llm = _client_with(fake, max_concurrency=16)
    b = _builder()
    asyncio.run(
        llm.fanout(
            model="groq:openai/gpt-oss-120b",
            schema=FindingList,
            system=b.system(),
            message_builder=b.messages,
            roles=[(f"l{i}", "x") for i in range(6)],
        )
    )
    assert len(fake.requests) == 6
    assert fake.peak <= PROVIDERS["groq"].max_concurrency


def test_effort_is_not_sent_to_a_model_that_rejects_it() -> None:
    captured: dict = {}

    class Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get_final_message(self):
            return SimpleNamespace(
                usage=Usage(input_tokens=1), stop_reason="end_turn", parsed_output=FindingList()
            )

    def stream(**kw):
        captured.update(kw)
        return Stream()

    llm = LLMClient(client=SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    asyncio.run(
        llm.parse(
            model="claude-haiku-4-5",
            schema=FindingList,
            system=[],
            messages=[],
            effort="low",
            max_tokens=100_000,
            label="t1",
        )
    )
    assert "output_config" not in captured
    assert captured["max_tokens"] == 64_000


# --- construction and status --------------------------------------------------


def test_clients_never_follow_redirects() -> None:
    s = _settings(openrouter_api_key="or-test", anthropic_api_key="sk-ant-test")
    assert make_client(s, "openrouter")._client.follow_redirects is False
    assert make_client(s, "anthropic")._client.follow_redirects is False


def test_clients_use_the_registry_base_url() -> None:
    s = _settings(groq_api_key="gsk-test", azure_api_key="az-test", azure_resource="acme-ai")
    assert str(make_client(s, "groq").base_url).startswith("https://api.groq.com/openai/v1")
    assert str(make_client(s, "azure_openai").base_url).startswith(
        "https://acme-ai.services.ai.azure.com/openai/v1"
    )


def test_a_provider_without_a_key_fails_clearly_only_when_used() -> None:
    s = _settings()
    with pytest.raises(ValueError, match="CR_GROQ_API_KEY"):
        make_client(s, "groq")


def test_legacy_openai_base_url_still_configures_azure_openai(monkeypatch) -> None:
    monkeypatch.setenv("CR_OPENAI_BASE_URL", "https://acme-ai.services.ai.azure.com/openai/v1")
    assert _settings().azure_openai_base_url == "https://acme-ai.services.ai.azure.com/openai/v1"


def test_provider_status_reports_names_never_values() -> None:
    secret = "gsk-super-secret-value"
    s = _settings(groq_api_key=secret, azure_resource="bad.example/x", azure_api_key="az")
    status = provider_status(s)
    assert status["groq"] == {"configured": True, "missing": []}
    assert status["openrouter"]["missing"] == ["CR_OPENROUTER_API_KEY"]
    # A resource name that would not pass validation is not "configured".
    assert "CR_AZURE_RESOURCE" in status["foundry"]["missing"]
    assert secret not in json.dumps(status)
