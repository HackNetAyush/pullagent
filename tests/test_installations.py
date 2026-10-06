"""Keeping our record of installations true when webhooks do not arrive.

GitHub is replaced by a dictionary of installations; everything else - the
store, the leases, the routes - is real.
"""

from __future__ import annotations

import asyncio
from collections import Counter

import httpx
import pytest

from cr.app import service
from cr.app.auth import AppAuth, InstallationGone, Secret
from cr.app.diagnose import event_gaps, mask_url, permission_gaps
from cr.store import db as store
from tests.test_app_service import FakeAuth, _noop, client, settings  # noqa: F401


def _inst(iid: int, login: str, kind: str = "User", suspended: bool = False) -> dict:
    return {
        "id": iid,
        "account": {"login": login, "type": kind},
        "suspended_at": "2026-01-01T00:00:00Z" if suspended else None,
    }


class GitHubApp(FakeAuth):
    """The App as GitHub sees it: its installations."""

    def __init__(self, installs: list[dict]) -> None:
        super().__init__()
        self.installs = installs
        self.calls: Counter = Counter()
        self.complete = True
        self.delay = 0.0

    async def all_installations(self) -> tuple[list[dict], bool]:
        self.calls["list"] += 1
        return list(self.installs), self.complete

    async def list_installation_repos(self, installation_id: int) -> list[str]:
        self.calls["repos"] += 1
        return [f"r{installation_id}/repo"]

    async def account_installation(self, login: str, account_type: str = "") -> dict | None:
        self.calls["account"] += 1
        await asyncio.sleep(self.delay)
        return next(
            (i for i in self.installs if i["account"]["login"].lower() == login.lower()), None
        )

    async def installation(self, installation_id: int) -> dict | None:
        self.calls["one"] += 1
        return next((i for i in self.installs if i["id"] == installation_id), None)


def _svc(settings, github: GitHubApp) -> service.AppService:  # noqa: F811
    svc = service.AppService(settings)
    svc._auth = github
    return svc


def _accounts() -> set[str]:
    return {i.account for i in store.installations()}


# --- the per-account check ----------------------------------------------------


def test_check_records_an_install_the_webhook_never_reported(settings):  # noqa: F811
    gh = GitHubApp([_inst(5, "alice")])
    assert asyncio.run(_svc(settings, gh).check_account("alice")) is True
    assert _accounts() == {"alice"}
    assert gh.calls["repos"] == 1  # new to us, so its repos are read


def test_check_drops_an_uninstall_the_webhook_never_reported(settings):  # noqa: F811
    store.upsert_installation(5, account="alice", repos=["alice/repo"])
    gh = GitHubApp([])
    assert asyncio.run(_svc(settings, gh).check_account("alice")) is False
    assert _accounts() == set()


def test_repeats_within_the_throttle_reuse_our_record(settings):  # noqa: F811
    gh = GitHubApp([_inst(5, "alice")])
    svc = _svc(settings, gh)
    asyncio.run(svc.check_account("alice"))
    asyncio.run(svc.check_account("Alice"))
    assert gh.calls["account"] == 1


def test_after_the_throttle_github_is_asked_again(settings, monkeypatch):  # noqa: F811
    monkeypatch.setattr(service, "INSTALL_CHECK_S", 0.0)
    gh = GitHubApp([])
    svc = _svc(settings, gh)
    assert asyncio.run(svc.check_account("bob")) is False
    gh.installs.append(_inst(9, "bob"))
    assert asyncio.run(svc.check_account("bob")) is True
    asyncio.run(svc.check_account("bob"))
    # A known installation is not re-read for its repos.
    assert gh.calls["repos"] == 1


def test_github_failing_leaves_the_record_alone(settings):  # noqa: F811
    class Down(GitHubApp):
        async def account_installation(self, login, account_type=""):
            raise httpx.ConnectError("down")

    store.upsert_installation(5, account="alice", repos=["alice/repo"])
    assert asyncio.run(_svc(settings, Down([])).check_account("alice")) is True
    assert _accounts() == {"alice"}


def test_tabs_asking_at_once_share_one_github_call(settings):  # noqa: F811
    gh = GitHubApp([_inst(5, "alice")])
    gh.delay = 0.05
    svc = _svc(settings, gh)

    async def many():
        return await asyncio.gather(*(svc.check_account("alice") for _ in range(5)))

    assert asyncio.run(many()) == [True] * 5
    assert gh.calls["account"] == 1


def test_account_lookup_refuses_what_is_not_a_login():
    auth = AppAuth(app_id="1", private_key=Secret("unused"))
    with pytest.raises(ValueError):
        asyncio.run(auth.account_installation("../app/hook/config"))


# --- the full reconcile -------------------------------------------------------


def test_full_sync_adds_and_removes(settings):  # noqa: F811
    store.upsert_installation(1, account="stays", repos=["stays/repo"])
    store.upsert_installation(2, account="gone", repos=["gone/repo"])
    gh = GitHubApp([_inst(1, "stays"), _inst(3, "new")])
    asyncio.run(_svc(settings, gh)._sync_installations())
    assert _accounts() == {"stays", "new"}
    assert gh.calls["repos"] == 1  # only the new one


def test_an_incomplete_listing_removes_nothing(settings):  # noqa: F811
    store.upsert_installation(2, account="past-the-cap", repos=["x/y"])
    gh = GitHubApp([_inst(1, "first")])
    gh.complete = False
    asyncio.run(_svc(settings, gh)._sync_installations())
    assert _accounts() == {"first", "past-the-cap"}


def test_a_failed_listing_removes_nothing(settings):  # noqa: F811
    class Down(GitHubApp):
        async def all_installations(self):
            raise httpx.ConnectError("down")

    store.upsert_installation(1, account="kept", repos=["kept/repo"])
    asyncio.run(_svc(settings, Down([]))._sync_installations())
    assert _accounts() == {"kept"}


def test_a_suspension_is_picked_up(settings):  # noqa: F811
    store.upsert_installation(1, account="acme", repos=["acme/repo"])
    asyncio.run(_svc(settings, GitHubApp([_inst(1, "acme", suspended=True)]))._sync_installations())
    assert [i.suspended for i in store.installations()] == [True]


# --- a refused token ----------------------------------------------------------


def test_a_404_token_exchange_marks_the_installation_removed(settings):  # noqa: F811
    store.upsert_installation(7, account="left", repos=["left/repo"])

    class Real(AppAuth):
        def app_jwt(self) -> str:
            return "jwt"

    auth = Real(app_id="1", private_key=Secret("unused"))
    auth._client = httpx.AsyncClient(
        base_url="https://api.github.com",
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={})),
    )
    svc = service.AppService(settings)
    svc._auth = auth
    with pytest.raises(InstallationGone):
        asyncio.run(svc.auth().installation_token(7))
    assert _accounts() == set()


# --- routes -------------------------------------------------------------------


def test_check_route_answers_for_one_workspace(client):  # noqa: F811
    client.service._auth = GitHubApp([_inst(5, "acme", "Organization")])
    r = client.post("/api/workspaces/acme/installation/check")
    assert r.status_code == 200
    assert r.json() == {"account": "acme", "installed": True}
    assert client.post("/api/workspaces/*/installation/check").status_code == 400


def test_setup_url_records_the_installation_github_confirms(client):  # noqa: F811
    gh = GitHubApp([_inst(7, "fresh")])
    client.service._auth = gh
    r = client.get("/app/installed?installation_id=7&setup_action=install", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/"
    assert "fresh" in _accounts()
    # An id GitHub does not know records nothing.
    client.get("/app/installed?installation_id=999", follow_redirects=False)
    assert gh.calls["one"] == 2 and "fresh" in _accounts()


# --- the doctor ---------------------------------------------------------------


def test_doctor_names_what_the_app_lacks():
    assert permission_gaps({"contents": "read"})[0].startswith("pull_requests: write")
    assert "members: read (has none)" in permission_gaps({"contents": "write"})
    assert event_gaps(["pull_request"]) == ["pull_request_review_comment", "issue_comment"]
    assert mask_url("https://smee.io/AbCdEfGhIjKlMnOp") == "https://smee.io/AbCdE…MnOp"
