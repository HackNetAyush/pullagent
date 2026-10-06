"""Customization on the customer's own keys: prices, custom lenses, review
guidelines, the sample review, the PR footer, and spend tracking.

Shares the store and fake-probe fixtures with test_workspace.
"""

from __future__ import annotations

import asyncio

import pytest

from cr.app import workspace as ws
from cr.models import (
    Category,
    Evidence,
    Finding,
    ModelCost,
    ReviewResult,
    Severity,
    Usage,
    Verdict,
    VerifiedFinding,
)
from cr.store import db as store
from tests.test_workspace import _connect, _draft, _spec, fake_probe, settings  # noqa: F401

# --- prices -----------------------------------------------------------------------------


def test_customer_prices_price_their_own_models(settings) -> None:
    conn = _connect(
        settings,
        models=["acme-gpt4o-prod"],
        prices={"acme-gpt4o-prod": {"input": 2.5, "output": 10.0}},
    )
    model = conn["models"][0]
    assert model["pricing"] == {"input": 2.5, "output": 10.0}
    assert model["price_source"] == "custom"

    client = ws.byok_client(settings, "acme")
    spec = client.spec(model["ref"])
    assert spec.cost_usd(Usage(input_tokens=1_000_000, output_tokens=100_000)) == pytest.approx(3.5)


def test_unlisted_models_without_a_price_are_untracked_not_free(settings) -> None:
    conn = _connect(settings, models=["acme-gpt4o-prod"])
    model = conn["models"][0]
    assert model["pricing"] is None and model["price_source"] is None


def test_a_customer_price_overrides_the_catalog(settings) -> None:
    conn = _connect(
        settings, models=["gpt-6-luna"], prices={"gpt-6-luna": {"input": 0.05, "output": 0.25}}
    )
    model = conn["models"][0]
    assert model["pricing"]["input"] == 0.05
    assert model["catalog_pricing"]["input"] == 0.10


def test_repricing_renaming_or_dropping_models_needs_no_retest(settings) -> None:
    conn = _connect(settings)
    draft = _draft(
        api_key="",
        models=["gpt-6-luna"],
        label="Renamed",
        prices={"gpt-6-luna": {"input": 0.2, "output": 0.6}},
    )
    saved = ws.save_connection(settings, "acme", draft, "", conn_id=conn["id"])
    assert saved["label"] == "Renamed" and saved["models"][0]["pricing"]["input"] == 0.2


def test_adding_an_untested_model_still_needs_a_test(settings) -> None:
    conn = _connect(settings)
    draft = _draft(api_key="", models=["gpt-6-luna", "brand-new-deploy"])
    with pytest.raises(ws.WorkspaceError, match="test the connection"):
        ws.save_connection(settings, "acme", draft, "", conn_id=conn["id"])


def test_prices_must_belong_to_listed_models(settings) -> None:
    tested = asyncio.run(ws.test_connection(settings, "acme", _draft()))
    draft = _draft(prices={"not-on-it": {"input": 1, "output": 1}})
    with pytest.raises(ws.WorkspaceError, match="not listed"):
        ws.save_connection(settings, "acme", draft, tested["receipt"])


# --- custom lenses ------------------------------------------------------------------------


def _lens(**kw) -> dict:
    base = {
        "id": "custom_n_plus_one",
        "name": "N+1 queries",
        "instruction": "Look for ORM queries issued inside loops over querysets.",
    }
    base.update(kw)
    return base


def test_custom_finder_lenses_run_with_their_own_prompt(settings) -> None:
    conn = _connect(settings)
    spec = _spec(conn, custom_finders=[_lens()])
    cfg = ws.to_tier_config(spec)
    assert "custom_n_plus_one" in cfg.finders
    prompt = cfg.finder_prompts["custom_n_plus_one"]
    assert "N+1 queries" in prompt and "ORM queries issued inside loops" in prompt
    # The coverage rule is restated whatever the owner wrote.
    assert "never how strictly to report" in prompt


def test_custom_verifier_lenses_replace_the_lens_question(settings) -> None:
    from cr.review import prompts

    conn = _connect(settings)
    spec = _spec(
        conn,
        custom_verifiers=[
            _lens(id="custom_tenancy", name="Tenancy", instruction="Is tenant id always checked?")
        ],
    )
    cfg = ws.to_tier_config(spec)
    text = cfg.verifier_prompts["custom_tenancy"]
    rendered = prompts.batch_verifier_instruction("custom_tenancy", "[]", text)
    assert "Tenancy: Is tenant id always checked?" in rendered


def test_a_tier_may_run_only_custom_lenses(settings) -> None:
    conn = _connect(settings)
    spec = _spec(conn, finders=[], custom_finders=[_lens()])
    assert ws.to_tier_config(spec).finders == ["custom_n_plus_one"]
    with pytest.raises(ValueError, match="at least one finder"):
        _spec(conn, finders=[], custom_finders=[])


def test_filtering_wording_is_flagged_not_blocked(settings) -> None:
    conn = _connect(settings)
    spec = _spec(
        conn,
        custom_finders=[_lens(instruction="Only report critical security issues in this code.")],
    )
    assert "custom_n_plus_one" in spec.warnings()
    assert ws.save_tier("acme", spec)["warnings"]


def test_engine_sends_the_custom_prompt_to_the_model(settings) -> None:
    from cr.config import Settings
    from cr.llm.prefix import PRContext, PrefixBuilder, RepoContext
    from cr.review import engine, prompts

    sent: dict[str, str] = {}

    class Recorder:
        async def fanout(self, **kw):
            sent.update(dict(kw["roles"]))
            return []

    conn = _connect(settings)
    cfg = ws.to_tier_config(_spec(conn, custom_finders=[_lens()]))
    builder = PrefixBuilder(
        preamble=prompts.PREAMBLE,
        repo=RepoContext(slug="acme/api"),
        pr=PRContext(title="t", description="d", diff="@@ -1 +1 @@\n-a\n+b\n"),
    )
    asyncio.run(engine.find(Recorder(), builder, cfg, Settings()))
    assert sent["custom_n_plus_one"] == cfg.finder_prompts["custom_n_plus_one"]
    assert sent["correctness"] == prompts.finder_instruction("correctness")


# --- footer ----------------------------------------------------------------------------------


def test_reviews_on_own_keys_say_which_models_wrote_them(settings) -> None:
    from cr.github import build_review

    conn = _connect(settings, label="Prod Azure")
    cfg = ws.to_tier_config(_spec(conn))
    line = ws.attribution("acme", cfg)
    assert "Fast and cheap" in line and "`gpt-6-luna` via Prod Azure" in line

    body, _ = build_review(
        [],
        commentable={},
        already=set(),
        tier="custom",
        cost=0.01,
        elapsed=3,
        killed=0,
        attribution=line,
    )
    assert line in body


# --- guidelines --------------------------------------------------------------------------------


def test_guidelines_are_saved_per_repository_and_read_case_insensitively(settings) -> None:
    ws.save_guidelines("acme", "acme/API", "Prefer explicit transactions.\nNo raw SQL.")
    assert store.repo_guidelines("ACME/api") == "Prefer explicit transactions.\nNo raw SQL."
    assert store.account_guidelines("acme") == {
        "acme/api": "Prefer explicit transactions.\nNo raw SQL."
    }
    ws.save_guidelines("acme", "acme/api", "")
    assert store.repo_guidelines("acme/api") == ""


def test_guidelines_cannot_target_another_accounts_repository(settings) -> None:
    with pytest.raises(ws.WorkspaceError, match="does not belong"):
        ws.save_guidelines("acme", "rival/api", "Be strict.")


def test_guidelines_that_would_break_caching_are_refused(settings) -> None:
    with pytest.raises(ws.WorkspaceError, match="disables caching"):
        ws.save_guidelines("acme", "acme/api", "Updated 2026-10-07 14:00:00 by ops.")


def test_guidelines_reach_the_review_prompt() -> None:
    from cr.llm.prefix import PRContext, PrefixBuilder, RepoContext
    from cr.review import prompts

    b = PrefixBuilder(
        preamble=prompts.PREAMBLE,
        repo=RepoContext(slug="acme/api", guidelines="No raw SQL."),
        pr=PRContext(title="t", description="d", diff="@@ -1 +1 @@\n-a\n+b\n"),
    )
    assert "No raw SQL." in "".join(block["text"] for block in b.system())


# --- sample review ---------------------------------------------------------------


def _bug(line: int = 6, claim: str = "end is one short") -> Finding:
    return Finding(
        claim=claim,
        failure_scenario="paginate([1,2,3], 0, 3) returns [1, 2] and drops the last item",
        evidence=[
            Evidence(file="app/pagination.py", start_line=line, end_line=line, quote="", why="w")
        ],
        category=Category.CORRECTNESS,
        severity=Severity.HIGH,
        confidence=0.9,
    )


def test_sample_review_reports_whether_the_planted_bug_was_caught(settings, monkeypatch) -> None:
    import cr.review.engine

    conn = _connect(settings)
    caught = _bug()

    async def fake_review(**kw):
        assert kw["record"] is False and kw["use_cache"] is False
        assert kw["tier"].model == conn["models"][0]["ref"]
        return ReviewResult(
            tier="sample",
            raw_findings=[caught],
            posted=[
                VerifiedFinding(
                    finding=caught,
                    verdicts=[Verdict(refuted=False, reasoning="app/pagination.py:6")],
                )
            ],
            cost_usd=0.002,
            elapsed_s=4.2,
        )

    monkeypatch.setattr(cr.review.engine, "review", fake_review)
    out = asyncio.run(ws.sample_review(settings, "acme", conn["id"], "gpt-6-luna"))
    assert out["found"] and out["verified"] and out["cost_usd"] == 0.002


def test_sample_review_notices_a_miss(settings, monkeypatch) -> None:
    import cr.review.engine

    conn = _connect(settings)
    unrelated = _bug(line=3, claim="Docstring should mention negative pages")
    unrelated.failure_scenario = "a reader may be confused"

    async def fake_review(**kw):
        return ReviewResult(tier="sample", raw_findings=[unrelated], posted=[])

    monkeypatch.setattr(cr.review.engine, "review", fake_review)
    out = asyncio.run(ws.sample_review(settings, "acme", conn["id"], "gpt-6-luna"))
    assert out["found"] is False and out["verified"] is False


def test_sample_review_only_runs_models_on_the_connection(settings) -> None:
    conn = _connect(settings)
    with pytest.raises(ws.WorkspaceError, match="not on this connection"):
        asyncio.run(ws.sample_review(settings, "acme", conn["id"], "someone-elses-model"))


# --- spend tracking --------------------------------------------------------------


def test_each_run_records_what_each_model_cost(settings) -> None:
    from cr.models import CallTrace
    from cr.review.engine import model_costs

    conn = _connect(
        settings,
        models=["acme-gpt4o-prod"],
        prices={"acme-gpt4o-prod": {"input": 1.0, "output": 2.0}},
    )
    client = ws.byok_client(settings, "acme")
    ref = conn["models"][0]["ref"]
    client.calls = [
        CallTrace(label="correctness", model=ref, usage=Usage(input_tokens=1000), cost_usd=0.001),
        CallTrace(label="verify", model=ref, usage=Usage(output_tokens=500), cost_usd=0.001),
    ]
    [mc] = model_costs(client)
    assert mc.model == ref and mc.label == "acme-gpt4o-prod" and mc.provider == "azure_openai"
    assert mc.calls == 2 and mc.cost_usd == pytest.approx(0.002) and mc.priced


def test_dashboard_separates_own_key_spend_by_model(settings, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from cr import server

    managed = store.start_run("acme/api", tier="T2", model="claude-sonnet-5")
    store.finish_run(managed, ReviewResult(tier="T2"), cost=0.30)
    byok = store.start_run(
        "acme/api", tier="custom", model="conn:1:acme-gpt4o-prod", billing="byok"
    )
    store.finish_run(
        byok,
        ReviewResult(
            tier="custom",
            model_costs=[
                ModelCost(
                    model="conn:1:acme-gpt4o-prod",
                    label="acme-gpt4o-prod",
                    provider="azure_openai",
                    calls=4,
                    cost_usd=0.05,
                )
            ],
        ),
        cost=0.05,
    )
    monkeypatch.setattr(server.settings, "github_client_id", "")
    with TestClient(server.app) as c:
        data = c.get("/api/overview?days=30").json()
        runs = c.get("/api/runs").json()["items"]

    assert data["cost"] == pytest.approx(0.35)
    assert data["byok"]["cost"] == pytest.approx(0.05)
    assert data["byok"]["by_model"][0]["label"] == "acme-gpt4o-prod · Azure OpenAI (Foundry)"
    day = data["series"][-1]
    assert day["managed"] == pytest.approx(0.30) and day["byok"] == pytest.approx(0.05)
    # CR-credit spend by model leaves the customer's models out.
    assert "conn:1:acme-gpt4o-prod" not in data["cost_by_model"]
    # A connection reference reads as the customer's model name.
    assert {r["model"] for r in runs} == {"claude-sonnet-5", "acme-gpt4o-prod"}

    spend = store.byok_spend("acme", __import__("datetime").datetime(2000, 1, 1))
    assert spend == {"conn:1:acme-gpt4o-prod": pytest.approx(0.05)}


# --- what a failed review may say in public -----------------------------------


def test_a_failed_review_never_shows_the_customers_key(settings):  # noqa: F811
    """A provider rejecting a key often quotes it back; the check run that says
    so is readable by anyone who can see the pull request."""
    from cr.app.runner import _public
    from tests.test_workspace import AZURE_KEY

    _connect(settings, "acme", resource="a" * 60)
    message = _public(
        f"finder: AuthenticationError: Incorrect API key provided: {AZURE_KEY}. "
        f"(starts {AZURE_KEY[:8]}, ends {AZURE_KEY[-8:]})",
        settings,
        "acme",
    )
    assert AZURE_KEY not in message
    assert AZURE_KEY[:8] not in message and AZURE_KEY[-8:] not in message
    assert "Incorrect API key provided" in message


def test_a_long_resource_name_still_saves(settings):  # noqa: F811
    """The default label names the resource; it must fit the column."""
    saved = _connect(settings, "acme", resource="r" * 64)
    assert len(saved["label"]) <= 64


def test_old_runs_are_assigned_to_their_owner_in_one_statement(tmp_path):
    """An existing database: runs from before workspaces get their account."""
    from sqlalchemy import create_engine, text

    url = f"sqlite:///{(tmp_path / 'old.db').as_posix()}"
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(text("CREATE TABLE runs (id INTEGER PRIMARY KEY, repo VARCHAR(255))"))
        c.execute(text("INSERT INTO runs (repo) VALUES ('acme/api'), ('bob/site'), ('odd')"))
    store._add_missing_columns(engine)
    with engine.begin() as c:
        rows = c.execute(text("SELECT repo, account FROM runs ORDER BY id")).all()
    assert rows == [("acme/api", "acme"), ("bob/site", "bob"), ("odd", "odd")]
