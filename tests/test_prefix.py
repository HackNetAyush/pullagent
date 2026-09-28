"""Tests for the cache-prefix contract.

These guard the 82% input saving. If any of them fail, caching is broken and the
only symptom in production would be the bill.
"""

from __future__ import annotations

import pytest

from cr.llm.prefix import (
    CacheInvalidatorError,
    PRContext,
    PrefixBuilder,
    RepoContext,
    stable_json,
)
from cr.review import prompts


def _builder(**repo_kw) -> PrefixBuilder:
    return PrefixBuilder(
        preamble=prompts.PREAMBLE,
        repo=RepoContext(slug="acme/api", **repo_kw),
        pr=PRContext(title="t", description="d", diff="@@ -1 +1 @@\n-a\n+b\n"),
    )


def test_two_breakpoints_in_the_right_places() -> None:
    b = _builder()
    system = b.system()
    messages = b.messages("find bugs")

    # Breakpoint 1: last system block only.
    assert "cache_control" not in system[0]
    assert system[-1]["cache_control"]["ttl"] == "1h"

    # Breakpoint 2: the PR block, NOT the role instruction.
    content = messages[0]["content"]
    assert content[0]["cache_control"]["ttl"] == "5m"
    assert "cache_control" not in content[1]


def test_role_instruction_is_last_and_outside_the_cache() -> None:
    """The whole design rests on this: role goes last, so finder and verifier
    passes share both cache entries."""
    b = _builder()
    finder = b.messages("FINDER ROLE")
    verifier = b.messages("VERIFIER ROLE")

    # Everything up to the breakpoint is byte-identical between the two.
    assert finder[0]["content"][0] == verifier[0]["content"][0]
    # Only the trailing, uncached block differs.
    assert finder[0]["content"][1]["text"] != verifier[0]["content"][1]["text"]


def test_prefix_is_byte_stable_across_builds() -> None:
    """Two builders with the same inputs must produce identical bytes, or every
    request writes a fresh cache entry and reads none."""
    a = _builder().system()
    b = _builder().system()
    assert stable_json(a) == stable_json(b)


@pytest.mark.parametrize(
    "poison",
    [
        "Generated at 2026-09-28T14:03:11",
        "run_id: 91f0c2",
        "trace-id = abc",
        "session 550e8400-e29b-41d4-a716-446655440000",
        "built 1759000000",
    ],
)
def test_invalidators_are_caught_at_build_time(poison: str) -> None:
    """Fail loud when someone puts volatile content in a cached block — the
    alternative is finding out from the invoice."""
    b = PrefixBuilder(
        preamble=prompts.PREAMBLE,
        repo=RepoContext(slug="acme/api", conventions=poison),
        pr=PRContext(title="t", description="d", diff="x"),
    )
    with pytest.raises(CacheInvalidatorError):
        b.system()


def test_diff_may_contain_timestamps() -> None:
    """The PR block is cached per head SHA, so a timestamp inside a diff is fine
    and must not trip the guard."""
    b = PrefixBuilder(
        preamble=prompts.PREAMBLE,
        repo=RepoContext(slug="acme/api"),
        pr=PRContext(title="t", description="d", diff="+created = 2026-09-28T14:03:11"),
    )
    b.system()  # must not raise
    assert "2026-09-28" in b.messages("x")[0]["content"][0]["text"]


def test_warm_payload_has_no_output_schema() -> None:
    """max_tokens: 0 is rejected alongside output_config.format."""
    payload = _builder().warm_payload()
    assert payload["max_tokens"] == 0
    assert "output_config" not in payload
    assert payload["system"][-1]["cache_control"]["ttl"] == "1h"


def test_suppressed_rules_reach_the_model() -> None:
    """D1: the deterministic layer subtracts. If this stops rendering, the model
    starts re-reporting every lint nit."""
    pr = PRContext(
        title="t",
        description="d",
        diff="x",
        suppressed_rules=("E501 line too long", "no-unused-vars"),
    )
    rendered = pr.render()
    assert "DO NOT comment on these" in rendered
    assert "E501 line too long" in rendered


def test_verifier_shares_the_finder_cache_prefix() -> None:
    """The verifier must reuse the finders' system block byte-for-byte.

    Regression guard: putting the verifier preamble in system[0] breaks the
    prefix match at byte 0, so every verifier call silently pays full price.
    Caught live on a real PR — the finder wrote 19,475 tokens and the verifier
    read zero.
    """
    from cr.config import TIERS
    from cr.models import Category, Evidence, Finding, Severity
    from cr.review import engine

    captured: list[dict] = []

    class FakeClient:
        usage = None

        async def parse(self, **kw):
            captured.append(kw)
            raise RuntimeError("stop after capture")

    finding = Finding(
        claim="x",
        failure_scenario="a concrete scenario long enough to pass prefilter",
        evidence=[Evidence(file="a.py", start_line=1, end_line=2, why="w")],
        category=Category.CORRECTNESS,
        severity=Severity.HIGH,
        confidence=0.9,
    )

    import asyncio

    b = _builder()
    asyncio.run(engine.verify(FakeClient(), b, TIERS["T1"], [finding]))

    assert captured, "verifier never issued a call"
    assert captured[0]["system"] == b.system(), (
        "verifier system block diverged from the finders' — cache prefix broken"
    )
    # The role text must appear after the cache breakpoint, not before it.
    trailing = captured[0]["messages"][0]["content"][-1]["text"]
    assert "REFUTE" in trailing
    assert "cache_control" not in trailing


def test_fanout_survives_a_failing_first_pass() -> None:
    """A truncated structured output on one lens must not lose the whole review."""
    import asyncio

    from cr.llm.client import Call, ClientPool, LLMClient
    from cr.models import FindingList, Usage

    class Boom:
        class messages:
            @staticmethod
            def stream(**_kw):
                raise RuntimeError("truncated JSON")

    client = LLMClient(pool=ClientPool(Boom()))

    async def fake_parse(**kw):
        return Call(parsed=FindingList(), usage=Usage(), model="m", label=kw.get("label", ""))

    client.parse = fake_parse  # type: ignore[method-assign]

    b = _builder()
    out = asyncio.run(
        client.fanout(
            model="m",
            schema=FindingList,
            system=b.system(),
            message_builder=b.messages,
            roles=[("a", "x"), ("b", "y")],
            warm_timeout_s=1.0,
        )
    )
    # The surviving lens still returns.
    assert [c.label for c in out] == ["b"]
