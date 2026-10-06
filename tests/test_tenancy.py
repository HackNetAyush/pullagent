"""Every dashboard read is private to its workspace.

A workspace is the GitHub account that owns the repository: a person, or an
organisation. You see a workspace's reviews, findings, spend and queue only
if it is your own account or an org you belong to. The server enforces it, so
a hand-edited `?account=` changes nothing. CR administrators may see all.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cr.models import ReviewResult
from cr.store import db as store

ALICE = SimpleNamespace(login="alice", is_admin=False, orgs=["acme"], admin_orgs=[])
BOB = SimpleNamespace(login="bob", is_admin=False, orgs=[], admin_orgs=[])
OPS = SimpleNamespace(login="ops", is_admin=True, orgs=[], admin_orgs=[])


@pytest.fixture
def http(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from cr import server
    from cr.app import accounts, authroutes

    monkeypatch.setenv("CR_CACHE_DIR", str(tmp_path / "cache"))
    store.reset_for_tests()
    store.init(f"sqlite:///{(tmp_path / 't.db').as_posix()}")

    for repo, cost in (("acme/api", 0.30), ("alice/side", 0.10), ("bob/secret", 0.50)):
        run = store.start_run(repo, tier="T2", model="claude-sonnet-5")
        store.finish_run(run, ReviewResult(tier="T2"), cost=cost)
    store.suppress("bob/secret", "fp-bob", reason="manual")

    who = {"user": ALICE}
    monkeypatch.setattr(accounts, "configured", lambda _s: True)
    monkeypatch.setattr(authroutes, "current_user", lambda _r: who["user"])
    monkeypatch.setattr(server, "guard", lambda _r: None)
    with TestClient(server.app) as c:
        c.who = who  # type: ignore[attr-defined]
        yield c
    store.reset_for_tests()


def _repos(c, account: str) -> set[str]:
    return {r["repo"] for r in c.get(f"/api/runs?account={account}").json()["items"]}


def test_each_workspace_sees_only_its_own_runs(http) -> None:
    assert _repos(http, "alice") == {"alice/side"}
    assert _repos(http, "acme") == {"acme/api"}  # an org alice belongs to


def test_another_accounts_workspace_is_refused(http) -> None:
    for path in (
        "/api/runs",
        "/api/overview",
        "/api/findings",
        "/api/suppressions",
        "/api/repos",
        "/api/runs/facets",
        "/api/runs/active",
    ):
        assert http.get(f"{path}?account=bob").status_code == 403, path


def test_a_workspace_must_be_chosen(http) -> None:
    assert http.get("/api/runs").status_code == 400
    assert http.get("/api/runs?account=*").status_code == 403


def test_overview_counts_only_the_workspace(http) -> None:
    data = http.get("/api/overview?account=alice").json()
    assert data["runs"] == 1 and data["cost"] == pytest.approx(0.10)
    assert data["suppressions"] == 0  # bob's suppression is not counted


def test_someone_elses_run_does_not_exist(http) -> None:
    http.who["user"] = OPS  # type: ignore[attr-defined]
    bobs = next(r["id"] for r in http.get("/api/runs?account=bob").json()["items"])
    http.who["user"] = ALICE  # type: ignore[attr-defined]
    assert http.get(f"/api/runs/{bobs}").status_code == 404


def test_cr_admins_can_see_every_workspace(http) -> None:
    http.who["user"] = OPS  # type: ignore[attr-defined]
    everyone = {r["repo"] for r in http.get("/api/runs?account=*").json()["items"]}
    assert everyone == {"acme/api", "alice/side", "bob/secret"}
    assert _repos(http, "bob") == {"bob/secret"}


def test_new_runs_record_their_workspace(http) -> None:
    from cr.store.models import Run

    with store.session() as s:
        accounts = {r.repo: r.account for r in s.query(Run)}
    assert accounts["acme/api"] == "acme" and accounts["bob/secret"] == "bob"


def test_old_runs_are_backfilled_into_their_workspace(tmp_path, monkeypatch) -> None:
    from sqlalchemy import text

    monkeypatch.setenv("CR_CACHE_DIR", str(tmp_path / "cache"))
    url = f"sqlite:///{(tmp_path / 'old.db').as_posix()}"
    store.reset_for_tests()
    store.init(url)
    run = store.start_run("acme/legacy", tier="T2", model="m")
    with store.session() as s:
        s.execute(text("UPDATE runs SET account = '' WHERE id = :i"), {"i": run})
    store.reset_for_tests()
    store.init(url)  # start-up migration
    with store.session() as s:
        assert s.execute(text("SELECT account FROM runs WHERE id = :i"), {"i": run}).scalar() == (
            "acme"
        )
    store.reset_for_tests()
