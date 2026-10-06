"""Workspaces: provider connections, custom tiers, and repository routing.

The properties that matter most, in order:

- a saved key never comes back out — not in a response, a status, an error;
- nothing is saved that did not pass its connection test, and the test result
  cannot be reused for a different key, resource or model list;
- one account can never use, see, or change another account's connections;
- a custom tier runs only on its own account's connections, never on ours;
- a repository routed to a tier that cannot run fails loudly instead of
  silently spending CR's credits on a repo routed away from them.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from cryptography.fernet import Fernet

from cr import vault
from cr.app import workspace as ws
from cr.config import Settings
from cr.doctor import DoctorReport
from cr.store import db as store

AZURE_KEY = "az-live-workspace-secret-0123456789"
GROQ_KEY = "gsk_live_workspace_secret_0123456789"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("CR_CACHE_DIR", str(tmp_path / "cache"))
    store.reset_for_tests()
    store.init(f"sqlite:///{(tmp_path / 'ws.db').as_posix()}")
    yield Settings(_env_file=None, secrets_key=Fernet.generate_key().decode())
    store.reset_for_tests()


@pytest.fixture(autouse=True)
def fake_probe(monkeypatch):
    """Stands in for the provider. Behaviour is chosen by model name:

    - `missing-*`   : 404, as Azure answers for a deployment that does not exist
    - `no-effort-*` : 400 when sent reasoning effort, fine without it
    - anything else : a healthy structured answer
    """
    import cr.doctor

    seen: list[tuple[str, bool]] = []

    async def probe(client, model, r=None, *, check_cache=True):
        spec = client.spec(model)
        seen.append((model, spec.supports_effort))
        rep = DoctorReport(model=model)
        if model.startswith("missing-"):
            rep.errors.append(f"NotFoundError: Error code: 404 - DeploymentNotFound ({AZURE_KEY})")
        elif model.startswith("no-effort-") and spec.supports_effort:
            rep.errors.append(
                "BadRequestError: Error code: 400 - Unsupported parameter: 'reasoning.effort'"
            )
        else:
            rep.reachable = rep.structured_outputs = True
        return rep

    monkeypatch.setattr(cr.doctor, "probe", probe)
    return seen


def _draft(**kw) -> ws.ConnectionDraft:
    base = {
        "provider": "azure_openai",
        "api_key": AZURE_KEY,
        "resource": "acme-ai",
        "models": ["gpt-6-luna", "acme-gpt4o-prod"],
    }
    base.update(kw)
    return ws.ConnectionDraft.model_validate(base)


def _connect(settings, account="acme", **kw) -> dict:
    draft = _draft(**kw)
    tested = asyncio.run(ws.test_connection(settings, account, draft))
    assert tested["ok"], tested
    return ws.save_connection(settings, account, draft, tested["receipt"])


def _spec(conn: dict, **kw) -> ws.TierSpec:
    first = conn["models"][0]["ref"]
    base = {
        "name": "Fast and cheap",
        "finder": {"model": first, "effort": "medium"},
        "verifier": {"model": first, "effort": "low"},
        "finders": [{"lens": "correctness"}, {"lens": "security"}],
        "verifiers": [{"lens": "reachability"}],
        "max_comments": 5,
    }
    base.update(kw)
    return ws.TierSpec.model_validate(base)


# --- connections ------------------------------------------------------------------


def test_a_tested_connection_is_saved_encrypted_with_its_own_model_names(settings) -> None:
    conn = _connect(settings)
    row = store.connection("acme", conn["id"])
    assert row is not None and AZURE_KEY not in row.ciphertext
    assert vault.decrypt(settings, row.ciphertext) == AZURE_KEY
    assert conn["hint"] == "…6789"
    assert conn["host"] == "acme-ai.services.ai.azure.com"
    # A deployment name the catalog has never heard of is accepted as-is.
    names = [m["name"] for m in conn["models"]]
    assert names == ["gpt-6-luna", "acme-gpt4o-prod"]
    custom = conn["models"][1]
    assert custom["listed"] is False and custom["pricing"] is None
    assert AZURE_KEY not in json.dumps(ws.overview(settings, "acme"))


def test_every_model_is_tested_and_failures_are_explained(settings) -> None:
    draft = _draft(models=["gpt-6-luna", "missing-deploy"])
    tested = asyncio.run(ws.test_connection(settings, "acme", draft))
    assert tested["ok"] is False and tested["receipt"] is None
    by_model = {r["model"]: r for r in tested["results"]}
    assert by_model["gpt-6-luna"]["ok"] is True
    bad = by_model["missing-deploy"]
    assert bad["ok"] is False
    assert bad["detail"] == "There is no deployment named missing-deploy on this resource."
    assert AZURE_KEY not in json.dumps(tested)


def test_nothing_is_saved_without_a_passing_test(settings) -> None:
    with pytest.raises(ws.WorkspaceError, match="test the connection"):
        ws.save_connection(settings, "acme", _draft(), "")
    with pytest.raises(ws.WorkspaceError, match="test the connection"):
        ws.save_connection(settings, "acme", _draft(), "forged-receipt")
    assert store.connections("acme") == []


@pytest.mark.parametrize(
    "change",
    [
        {"models": ["gpt-6-luna", "acme-gpt4o-prod", "added-after-the-test"]},
        {"api_key": "az-a-different-key-000000000"},
        {"resource": "other-resource"},
    ],
)
def test_a_receipt_only_covers_exactly_what_was_tested(settings, change) -> None:
    tested = asyncio.run(ws.test_connection(settings, "acme", _draft()))
    with pytest.raises(ws.WorkspaceError, match="test it again"):
        ws.save_connection(settings, "acme", _draft(**change), tested["receipt"])


def test_a_receipt_cannot_be_replayed_on_another_account(settings) -> None:
    tested = asyncio.run(ws.test_connection(settings, "acme", _draft()))
    with pytest.raises(ws.WorkspaceError):
        ws.save_connection(settings, "rival", _draft(), tested["receipt"])


def test_a_model_that_rejects_effort_is_retried_and_remembered(settings, fake_probe) -> None:
    conn = _connect(settings, models=["no-effort-gpt4o"])
    assert fake_probe == [("no-effort-gpt4o", True), ("no-effort-gpt4o", False)]
    assert conn["models"][0]["effort_levels"] == []
    client = ws.byok_client(settings, "acme")
    assert client.spec(conn["models"][0]["ref"]).clamp_effort("high") is None


def test_editing_without_the_key_keeps_the_stored_one(settings) -> None:
    conn = _connect(settings)
    before = store.connection("acme", conn["id"]).ciphertext
    draft = _draft(api_key="", models=["gpt-6-luna"], label="Prod")
    tested = asyncio.run(ws.test_connection(settings, "acme", draft, conn_id=conn["id"]))
    saved = ws.save_connection(settings, "acme", draft, tested["receipt"], conn_id=conn["id"])
    assert saved["label"] == "Prod" and [m["name"] for m in saved["models"]] == ["gpt-6-luna"]
    assert store.connection("acme", conn["id"]).ciphertext == before


@pytest.mark.parametrize("bad", ["has space", "", "../etc", "x" * 200])
def test_model_names_are_validated(bad) -> None:
    with pytest.raises(ValueError):
        _draft(models=[bad])


def test_azure_needs_a_valid_resource_name(settings) -> None:
    with pytest.raises(ws.WorkspaceError):
        asyncio.run(ws.test_connection(settings, "acme", _draft(resource="evil.com/x")))


@pytest.mark.parametrize("bad", ["short", "has a space in it ok"])
def test_implausible_keys_are_rejected(settings, bad) -> None:
    with pytest.raises(ws.WorkspaceError):
        asyncio.run(ws.test_connection(settings, "acme", _draft(api_key=bad)))


def test_connection_tests_are_rate_limited(settings) -> None:
    limited = settings.model_copy(update={"key_test_limit_per_hour": 1})
    asyncio.run(ws.test_connection(limited, "acme", _draft()))
    with pytest.raises(ws.RateLimited):
        asyncio.run(ws.test_connection(limited, "acme", _draft()))


def test_scrub_removes_the_key_and_anything_shaped_like_one() -> None:
    msg = (
        f"401: Incorrect API key provided: {GROQ_KEY[:8]}***{GROQ_KEY[-4:]} (also sk-ant-abc123xyz)"
    )
    out = vault.scrub(msg, [GROQ_KEY])
    assert GROQ_KEY[:8] not in out and "sk-ant-abc123xyz" not in out and "401" in out


def test_a_deployment_without_a_master_key_refuses_to_store_keys() -> None:
    s = Settings(_env_file=None, db_url="postgresql+psycopg://x/y", secrets_key=None)
    with pytest.raises(vault.VaultUnavailable):
        vault.encrypt(s, "anything")


def test_rotating_the_master_key_keeps_old_keys_readable(settings) -> None:
    token = vault.encrypt(settings, GROQ_KEY)
    rotated = Settings(
        _env_file=None, secrets_key=f"{Fernet.generate_key().decode()},{settings.secrets_key}"
    )
    assert vault.decrypt(rotated, token) == GROQ_KEY


# --- who may manage what ----------------------------------------------------------


def test_members_view_and_only_owners_and_org_admins_manage() -> None:
    me = ws.Actor(login="dev", orgs=["acme", "tools"], admin_orgs=["tools"])
    assert ws.can_manage(me, "dev")  # their own account
    assert ws.can_view(me, "ACME") and not ws.can_manage(me, "acme")  # plain member
    assert ws.can_manage(me, "tools")  # org admin
    assert not ws.can_view(me, "rival")
    ops = ws.Actor(login="ops", is_admin=True)
    assert ws.can_view(ops, "rival") and ws.can_manage(ops, "rival")


def test_one_accounts_connection_is_useless_to_another(settings) -> None:
    conn = _connect(settings, "acme")
    with pytest.raises(ws.WorkspaceError, match="connection was removed"):
        ws.save_tier("rival", _spec(conn))
    with pytest.raises(ValueError, match="not on any of your connections"):
        ws.byok_client(settings, "rival").spec(conn["models"][0]["ref"])


# --- custom tiers -------------------------------------------------------------------


def test_tier_spec_rejects_unknown_and_repeated_lenses(settings) -> None:
    conn = _connect(settings)
    with pytest.raises(ValueError, match="unknown finder lens"):
        _spec(conn, finders=[{"lens": "vibes"}])
    with pytest.raises(ValueError, match="once"):
        _spec(conn, verifiers=[{"lens": "evidence"}, {"lens": "evidence"}])


def test_managed_models_are_not_available_to_custom_tiers(settings) -> None:
    """A bare name means 'whatever CR runs' — our keys, our bill."""
    conn = _connect(settings)
    spec = _spec(conn, verifier={"model": "claude-opus-5", "effort": "high"})
    assert ws.tier_problems(spec, "acme") == [
        "claude-opus-5: choose a model from one of your connections"
    ]


def test_tier_names_are_unique_per_account(settings) -> None:
    conn = _connect(settings)
    ws.save_tier("acme", _spec(conn))
    with pytest.raises(ws.WorkspaceError, match="already exists"):
        ws.save_tier("acme", _spec(conn, name="fast and  CHEAP"))


def test_per_agent_choices_become_lens_routes(settings) -> None:
    conn = _connect(settings)
    a, b = (m["ref"] for m in conn["models"])
    spec = _spec(
        conn,
        finders=[{"lens": "correctness"}, {"lens": "security", "model": b, "effort": "xhigh"}],
        verifiers=[{"lens": "evidence", "effort": "high"}],
    )
    cfg = ws.to_tier_config(spec)
    assert cfg.custom and cfg.name == "custom"
    assert cfg.finder_route("correctness") == (a, "medium")
    assert cfg.finder_route("security") == (b, "xhigh")
    assert cfg.verifier_route("evidence") == (a, "high")
    assert cfg.finder_max_tokens == 64_000


# --- repository routing ----------------------------------------------------------------


def test_repositories_default_to_managed_review(settings) -> None:
    assert ws.resolve_repo(settings, "acme", "acme/api") is None


def test_an_account_wide_tier_forces_every_repository_onto_own_keys(settings) -> None:
    conn = _connect(settings)
    tier = ws.save_tier("acme", _spec(conn))
    ws.save_routing("acme", tier["id"], {})

    resolved = ws.resolve_repo(settings, "acme", "acme/anything")
    assert resolved is not None
    cfg, client = resolved
    assert cfg.custom and cfg.name == "custom"
    spec = client.spec(cfg.model)
    assert spec.provider == "azure_openai" and spec.model == "gpt-6-luna"
    azure = client._pool.client(spec.endpoint)
    assert azure.api_key == AZURE_KEY
    assert str(azure.base_url).startswith("https://acme-ai.services.ai.azure.com/openai/v1")


def test_a_repository_rule_beats_the_account_wide_one(settings) -> None:
    conn = _connect(settings)
    broad = ws.save_tier("acme", _spec(conn, name="Broad"))
    narrow = ws.save_tier("acme", _spec(conn, name="Payments audit", max_comments=12))
    ws.save_routing("acme", broad["id"], {"acme/Payments": narrow["id"]})
    assert ws.tier_for_repo("acme", "acme/payments") == narrow["id"]
    assert ws.tier_for_repo("acme", "acme/web") == broad["id"]

    ws.save_routing("acme", None, {"acme/payments": narrow["id"]})
    assert ws.resolve_repo(settings, "acme", "acme/web") is None
    cfg, _ = ws.resolve_repo(settings, "acme", "acme/payments")
    assert cfg.max_comments == 12


def test_routing_only_accepts_the_accounts_own_repositories(settings) -> None:
    conn = _connect(settings)
    tier = ws.save_tier("acme", _spec(conn))
    with pytest.raises(ws.WorkspaceError, match="does not belong to acme"):
        ws.save_routing("acme", None, {"rival/api": tier["id"]})
    with pytest.raises(ws.WorkspaceError, match="owner/name"):
        ws.save_routing("acme", None, {"not a repo": tier["id"]})


def test_a_broken_routed_tier_fails_loudly(settings) -> None:
    conn = _connect(settings)
    tier = ws.save_tier("acme", _spec(conn))
    ws.save_routing("acme", None, {"acme/api": tier["id"]})
    store.delete_connection("acme", conn["id"])  # e.g. removed straight from the store
    with pytest.raises(ws.TierUnavailable, match="connection was removed"):
        ws.resolve_repo(settings, "acme", "acme/api")


def test_removing_a_connection_in_use_is_refused(settings) -> None:
    conn = _connect(settings)
    tier = ws.save_tier("acme", _spec(conn))
    ws.save_routing("acme", tier["id"], {})
    with pytest.raises(ws.WorkspaceError, match="reviewing repositories"):
        ws.delete_connection("acme", conn["id"])
    ws.save_routing("acme", None, {})
    ws.delete_connection("acme", conn["id"])
    assert store.connections("acme") == []


def test_deleting_a_tier_returns_its_repositories_to_managed_review(settings) -> None:
    conn = _connect(settings)
    tier = ws.save_tier("acme", _spec(conn))
    ws.save_routing("acme", tier["id"], {"acme/api": tier["id"]})
    assert store.delete_custom_tier("acme", tier["id"])
    assert store.routing_rules("acme") == {"all": None, "repos": {}}


def test_routing_rejects_other_accounts_tiers(settings) -> None:
    conn = _connect(settings, "rival")
    theirs = ws.save_tier("rival", _spec(conn))
    with pytest.raises(ws.WorkspaceError, match="does not exist"):
        ws.save_routing("acme", theirs["id"], {})


# --- HTTP ----------------------------------------------------------------------------------


@pytest.fixture
def http(settings, monkeypatch):
    from fastapi.testclient import TestClient

    from cr import server
    from cr.app import workspace_routes

    monkeypatch.setattr(workspace_routes, "settings", settings)
    with TestClient(server.app) as c:
        yield c


def test_routes_round_trip_without_ever_returning_the_key(http) -> None:
    draft = _draft().model_dump()
    r = http.post("/api/workspaces/acme/connections/test", json=draft)
    assert r.status_code == 200, r.text
    assert AZURE_KEY not in r.text
    receipt = r.json()["receipt"]

    assert http.post("/api/workspaces/acme/connections", json=draft).status_code == 400
    r = http.post("/api/workspaces/acme/connections", json={**draft, "receipt": receipt})
    assert r.status_code == 200, r.text
    conn = r.json()
    assert AZURE_KEY not in r.text and conn["hint"] == "…6789"

    r = http.post("/api/workspaces/acme/tiers", json=_spec(conn).model_dump(mode="json"))
    assert r.status_code == 200, r.text
    tier_id = r.json()["id"]
    r = http.put("/api/workspaces/acme/routing", json={"repos": {"acme/api": tier_id}})
    assert r.status_code == 200, r.text
    assert r.json()["routing"] == {"all": None, "repos": {"acme/api": tier_id}}

    body = http.get("/api/workspaces/acme")
    assert body.status_code == 200 and AZURE_KEY not in body.text
    data = body.json()
    assert data["connections"][0]["used_by"] == ["Fast and cheap"]
    assert data["routing"]["repos"] == {"acme/api": tier_id}
    # Presets are described by behaviour, never by provider or model.
    assert all("model" not in p and "provider" not in p for p in data["presets"])

    assert http.delete(f"/api/workspaces/acme/connections/{conn['id']}").status_code == 409


def test_routes_report_validation_errors_plainly(http) -> None:
    assert http.post("/api/workspaces/acme/tiers", json={"name": ""}).status_code == 400
    bad = {**_draft().model_dump(), "provider": "nope"}
    assert http.post("/api/workspaces/acme/connections/test", json=bad).status_code == 422


def test_signed_in_users_cannot_reach_other_accounts(http, monkeypatch) -> None:
    from types import SimpleNamespace

    from cr.app import accounts, authroutes

    monkeypatch.setattr(accounts, "configured", lambda _s: True)
    monkeypatch.setattr(
        authroutes,
        "current_user",
        lambda _r: SimpleNamespace(login="dev", is_admin=False, orgs=["acme"], admin_orgs=[]),
    )
    # A member sees the org's workspace, read-only.
    body = http.get("/api/workspaces/acme")
    assert body.status_code == 200 and body.json()["can_manage"] is False
    r = http.post("/api/workspaces/acme/connections/test", json=_draft().model_dump())
    assert r.status_code == 403
    assert http.put("/api/workspaces/acme/routing", json={"all": None}).status_code == 403
    # ...and nothing at all of another account.
    assert http.get("/api/workspaces/rival").status_code == 403
    r = http.post("/api/workspaces/rival/connections/test", json=_draft().model_dump())
    assert r.status_code == 403
    listed = {w["login"]: w for w in http.get("/api/workspaces").json()["workspaces"]}
    assert set(listed) == {"dev", "acme"}
    assert listed["dev"]["role"] == "owner" and listed["acme"]["role"] == "member"


# --- engine ---------------------------------------------------------------------------------


def test_finder_lenses_on_different_models_run_as_separate_groups() -> None:
    from cr.llm.prefix import PRContext, PrefixBuilder, RepoContext
    from cr.review import engine, prompts

    calls: list[tuple[str, str, list[str]]] = []

    class Recorder:
        async def fanout(self, **kw):
            calls.append((kw["model"], kw["effort"], [label for label, _ in kw["roles"]]))
            return []

    spec = ws.TierSpec.model_validate(
        {
            "name": "t",
            "finder": {"model": "conn:1:a", "effort": "medium"},
            "verifier": {"model": "conn:1:a", "effort": "low"},
            "finders": [
                {"lens": "correctness"},
                {"lens": "security", "model": "conn:2:b"},
                {"lens": "performance"},
            ],
            "verifiers": [{"lens": "reachability"}],
        }
    )
    builder = PrefixBuilder(
        preamble=prompts.PREAMBLE,
        repo=RepoContext(slug="acme/api"),
        pr=PRContext(title="t", description="d", diff="@@ -1 +1 @@\n-a\n+b\n"),
    )
    asyncio.run(engine.find(Recorder(), builder, ws.to_tier_config(spec), Settings()))
    assert sorted(calls) == [
        ("conn:1:a", "medium", ["correctness", "performance"]),
        ("conn:2:b", "medium", ["security"]),
    ]
