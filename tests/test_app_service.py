"""The GitHub App, end to end, with GitHub replaced by a dictionary.

A real webhook body, signed with a real HMAC, through the real ingress, the
real queue and the real runner — with only the network faked. That boundary is
deliberate: the parts that break in production are the ones between those
pieces (a delivery deduped twice, a debounce that never fires, a review posted
against a stale sha), and a test that mocks the queue cannot see any of them.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from cr.app import runner, service
from cr.app.api import InstallationClient
from cr.app.auth import AppAuth, Secret
from cr.app.jobs import JobQueue
from cr.config import Settings
from cr.models import (
    Category,
    Evidence,
    Finding,
    ReviewResult,
    Severity,
    Usage,
    Verdict,
    VerifiedFinding,
)
from cr.store import db as store

SECRET = "webhook-secret"

DIFF = """diff --git a/pkg/calc.py b/pkg/calc.py
--- a/pkg/calc.py
+++ b/pkg/calc.py
@@ -1,4 +1,5 @@
 def total(items):
-    return sum(items)
+    # off by one
+    return sum(items[1:])

 def other():
"""

INCREMENTAL_DIFF = """diff --git a/pkg/calc.py b/pkg/calc.py
--- a/pkg/calc.py
+++ b/pkg/calc.py
@@ -1,2 +1,2 @@
 def total(items):
-    return sum(items[1:])
+    return sum(items[2:])
"""


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# --- fakes -------------------------------------------------------------------


class FakeGitHub:
    """Just enough of the REST API to drive a review, and a log of every write."""

    def __init__(self) -> None:
        self.head_sha = "head1"
        self.state = "open"
        self.draft = False
        self.review_comment_list: list[dict] = []
        self.issue_comment_list: list[dict] = []
        self.submitted: list[dict] = []
        self.replies: list[dict] = []
        self.check_runs: list[dict] = []
        self.reactions: list[dict] = []
        self.compare: dict[tuple[str, str], str] = {}
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        self.calls.append(f"{method} {path}")
        body = json.loads(request.content) if request.content else {}

        if method == "GET" and path.endswith("/pulls/42"):
            if "diff" in request.headers.get("accept", ""):
                return httpx.Response(200, text=DIFF)
            return httpx.Response(
                200,
                json={
                    "number": 42,
                    "state": self.state,
                    "draft": self.draft,
                    "merged": False,
                    "title": "Fix the total",
                    "body": "Adjusts summation.",
                    "user": {"login": "dev"},
                    "head": {"sha": self.head_sha, "repo": {"full_name": "me/repo"}},
                    "base": {"sha": "base1"},
                },
            )
        if method == "GET" and "/compare/" in path:
            spec = path.rsplit("/compare/", 1)[1]
            base, _, head = spec.partition("...")
            return httpx.Response(200, text=self.compare.get((base, head), ""))
        if method == "GET" and path.endswith("/pulls/42/comments"):
            return httpx.Response(200, json=self.review_comment_list)
        if method == "GET" and path.endswith("/issues/42/comments"):
            return httpx.Response(200, json=self.issue_comment_list)
        if method == "POST" and path.endswith("/pulls/42/reviews"):
            self.submitted.append(body)
            return httpx.Response(200, json={"id": 1})
        if method == "POST" and "/comments/" in path and path.endswith("/replies"):
            self.replies.append({"root": int(path.split("/comments/")[1].split("/")[0]), **body})
            return httpx.Response(201, json={"id": 999})
        if method == "POST" and path.endswith("/issues/42/comments"):
            self.issue_comment_list.append({"id": 500, "body": body.get("body", "")})
            return httpx.Response(201, json={"id": 500})
        if method == "POST" and path.endswith("/reactions"):
            self.reactions.append(body)
            return httpx.Response(201, json={"id": 1})
        if method == "POST" and path.endswith("/check-runs"):
            self.check_runs.append({"id": 77, **body})
            return httpx.Response(201, json={"id": 77})
        if method == "PATCH" and "/check-runs/" in path:
            self.check_runs.append(body)
            return httpx.Response(200, json={"id": 77})
        if method == "GET" and "/pulls/comments/" in path:
            cid = int(path.rsplit("/", 1)[1])
            for c in self.review_comment_list:
                if c["id"] == cid:
                    return httpx.Response(200, json=c)
            return httpx.Response(404, json={})
        if path == "/graphql":
            return httpx.Response(
                200,
                json={"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}},
            )
        return httpx.Response(404, json={"message": f"unmocked {method} {path}"})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="https://api.github.com", transport=httpx.MockTransport(self.handler)
        )


class FakeAuth(AppAuth):
    """An AppAuth that mints tokens without a private key or a network."""

    def __init__(self) -> None:
        super().__init__(app_id="1", private_key=Secret("unused"))
        self.minted: list[tuple[int, tuple[str, ...]]] = []

    async def installation_token(self, installation_id, *, repositories=None) -> str:
        self.minted.append((installation_id, tuple(repositories or ())))
        return "ghs_faketoken"

    async def aclose(self) -> None:
        return None


def _finding(claim="Off-by-one drops the first item", line=3) -> VerifiedFinding:
    return VerifiedFinding(
        finding=Finding(
            claim=claim,
            failure_scenario="total([1,2,3]) returns 5 instead of 6 for every caller.",
            evidence=[
                Evidence(file="pkg/calc.py", start_line=line, end_line=line, quote="", why="slice")
            ],
            category=Category.CORRECTNESS,
            severity=Severity.HIGH,
            confidence=0.9,
        ),
        verdicts=[Verdict(refuted=False, reasoning="confirmed")],
    )


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("CR_CACHE_DIR", str(tmp_path / "cache"))
    store.reset_for_tests()
    store.init(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    s = Settings(
        github_app_id="1",
        github_app_private_key="-----BEGIN RSA PRIVATE KEY-----\nx\n",
        github_webhook_secret=SECRET,
        app_debounce_s=0.0,
        app_allow_setup=True,
        # The allowlist has its own tests below; every other test here predates
        # it and is about routing, not authorisation.
        app_require_approval=False,
        app_credentials_path=tmp_path / "creds.json",
    )
    yield s
    store.reset_for_tests()


@pytest.fixture
def github(monkeypatch):
    fake = FakeGitHub()
    original = InstallationClient.__init__

    def patched(self, ref, token_provider, **kw):
        original(self, ref, token_provider, client=fake.client(), **kw)

    monkeypatch.setattr(InstallationClient, "__init__", patched)
    return fake


@pytest.fixture
def no_local_context(monkeypatch):
    """No git, no linters, no clone — those are tested elsewhere."""
    monkeypatch.setattr(runner, "_gather_local_context", lambda *a, **k: runner.LocalContext())


@pytest.fixture
def review_result(monkeypatch):
    """Replace the engine with a recorder. Model calls are not under test here."""
    captured: dict[str, Any] = {}

    async def fake_review(**kw):
        captured.update(kw)
        return ReviewResult(
            tier=kw["tier"].name,
            posted=[_finding()],
            usage=Usage(),
            cost_usd=0.12,
            elapsed_s=3.0,
        )

    monkeypatch.setattr(runner, "run_review", fake_review)
    return captured


@pytest.fixture
def client(settings, monkeypatch):
    """The real app, with a FakeAuth and installations already recorded."""
    monkeypatch.setattr(service, "default_settings", settings)
    svc = service.AppService(settings)
    svc._auth = FakeAuth()
    # The only thing stubbed on the service itself: start-up reconciliation
    # calls the real GitHub App endpoints, which have no installation to list.
    monkeypatch.setattr(svc, "_sync_installations", _noop)

    store.upsert_installation(99, account="me", repos=["me/repo"])

    api = service.create_app(settings, with_dashboard=False, service=svc)
    with TestClient(api) as c:
        c.service = svc  # type: ignore[attr-defined]
        yield c


async def _noop() -> None:
    return None


def post_hook(
    client: TestClient, event: str, payload: dict, *, delivery: str = "d1", secret=SECRET
):
    body = json.dumps(payload).encode()
    return client.post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": sign(body, secret),
            "Content-Type": "application/json",
        },
    )


def _pr_event(action="opened", **over):
    payload = {
        "action": action,
        "repository": {"full_name": "me/repo"},
        "installation": {"id": 99},
        "sender": {"login": "dev", "type": "User"},
        "pull_request": {
            "number": 42,
            "state": "open",
            "draft": False,
            "head": {"sha": "head1", "repo": {"full_name": "me/repo"}},
            "base": {"sha": "base1"},
        },
    }
    payload.update(over)
    return payload


def drain(client: TestClient, timeout: float = 10.0) -> None:
    svc: service.AppService = client.service  # type: ignore[attr-defined]
    portal = client.portal  # anyio portal backing the TestClient
    portal.call(lambda: svc.queue.drain(timeout))


# --- ingress -----------------------------------------------------------------


def test_unsigned_delivery_is_rejected(client):
    body = json.dumps(_pr_event()).encode()
    r = client.post(
        "/webhook",
        content=body,
        headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d0"},
    )
    assert r.status_code == 401


def test_forged_signature_is_rejected(client):
    r = post_hook(client, "pull_request", _pr_event(), secret="wrong-secret")
    assert r.status_code == 401


def test_ignored_events_answer_200_without_queueing(client):
    r = post_hook(client, "pull_request", _pr_event(action="labeled"))
    assert r.status_code == 200
    assert r.json()["ignored"]
    assert client.service.queue.depth == 0  # type: ignore[attr-defined]


def test_duplicate_delivery_is_processed_once(client, github, no_local_context, review_result):
    first = post_hook(client, "pull_request", _pr_event(), delivery="same-id")
    second = post_hook(client, "pull_request", _pr_event(), delivery="same-id")
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    drain(client)
    assert len(github.submitted) == 1


# --- review ------------------------------------------------------------------


def test_opened_pr_is_reviewed_and_posted(client, github, no_local_context, review_result):
    assert post_hook(client, "pull_request", _pr_event()).status_code == 202
    drain(client)

    assert len(github.submitted) == 1
    review = github.submitted[0]
    assert review["commit_id"] == "head1"
    assert review["event"] == "COMMENT"
    assert review["comments"][0]["path"] == "pkg/calc.py"
    assert "Off-by-one" in review["comments"][0]["body"]
    # The fingerprint marker is what makes the next push idempotent.
    assert "<!-- cr:" in review["comments"][0]["body"]


def test_review_reports_a_check_run(client, github, no_local_context, review_result):
    post_hook(client, "pull_request", _pr_event())
    drain(client)
    assert github.check_runs[0]["status"] == "in_progress"
    assert github.check_runs[-1]["conclusion"] in {"neutral", "success"}


def test_state_records_the_reviewed_head(client, github, no_local_context, review_result):
    post_hook(client, "pull_request", _pr_event())
    drain(client)
    state = store.pr_state("me/repo", 42)
    assert state is not None
    assert state.last_reviewed_sha == "head1"
    assert state.reviews == 1


def test_push_reviews_only_the_new_commits(client, github, no_local_context, review_result):
    post_hook(client, "pull_request", _pr_event(), delivery="d-open")
    drain(client)

    review_result.clear()  # so a skipped second review cannot pass on stale data
    github.head_sha = "head2"
    github.compare[("head1", "head2")] = INCREMENTAL_DIFF
    post_hook(
        client,
        "pull_request",
        _pr_event(action="synchronize", before="head1"),
        delivery="d-push",
    )
    drain(client)

    # The engine was handed the incremental diff, not the whole PR.
    diff = review_result["pr"].diff
    assert "sum(items[2:])" in diff
    assert "# off by one" not in diff
    assert store.pr_state("me/repo", 42).last_reviewed_sha == "head2"


def test_push_with_no_reviewable_change_skips_the_model(
    client, github, no_local_context, review_result
):
    post_hook(client, "pull_request", _pr_event(), delivery="d-open")
    drain(client)
    review_result.clear()

    github.head_sha = "head2"
    github.compare[("head1", "head2")] = ""  # e.g. only a commit message changed
    post_hook(
        client,
        "pull_request",
        _pr_event(action="synchronize", before="head1"),
        delivery="d-push",
    )
    drain(client)

    assert review_result == {}  # never reached the engine
    assert len(github.submitted) == 1  # nothing new posted


def test_already_posted_findings_are_not_reposted(client, github, no_local_context, review_result):
    post_hook(client, "pull_request", _pr_event(), delivery="d1")
    drain(client)
    posted_body = github.submitted[0]["comments"][0]["body"]
    github.review_comment_list.append({"id": 1, "body": posted_body, "path": "pkg/calc.py"})

    post_hook(client, "pull_request", _pr_event(action="reopened"), delivery="d2")
    drain(client)

    assert len(github.submitted) == 1  # the second review had nothing new to say


def test_token_is_scoped_to_the_one_repository(client, github, no_local_context, review_result):
    post_hook(client, "pull_request", _pr_event())
    drain(client)
    auth: FakeAuth = client.service._auth  # type: ignore[attr-defined]
    assert auth.minted
    assert all(scope == ("repo",) for _, scope in auth.minted)


def test_incomplete_review_is_not_posted(client, github, no_local_context, monkeypatch):
    async def failing(**kw):
        return ReviewResult(
            tier="T1", posted=[_finding()], usage=Usage(), errors=["finder: APIError"]
        )

    monkeypatch.setattr(runner, "run_review", failing)
    post_hook(client, "pull_request", _pr_event())
    drain(client)

    assert github.submitted == []
    assert github.check_runs[-1]["conclusion"] == "neutral"
    assert "incomplete" in github.check_runs[-1]["output"]["title"].lower()


def test_closed_pr_is_not_reviewed(client, github, no_local_context, review_result):
    github.state = "closed"
    post_hook(client, "pull_request", _pr_event())
    drain(client)
    assert github.submitted == []


# --- conversation ------------------------------------------------------------


def _reply_event(body="are you sure? items[0] is a header row", comment_id=8, root=7):
    return {
        "action": "created",
        "repository": {"full_name": "me/repo"},
        "installation": {"id": 99},
        "sender": {"login": "dev", "type": "User"},
        "pull_request": {"number": 42, "head": {"sha": "head1"}},
        "comment": {"id": comment_id, "body": body, "in_reply_to_id": root},
    }


@pytest.fixture
def our_thread(github):
    """A CR finding comment with a human reply underneath it."""
    fp = _finding().finding.fingerprint()
    github.review_comment_list.extend(
        [
            {
                "id": 7,
                "body": f"Off-by-one drops the first item\n<!-- cr:{fp} -->",
                "path": "pkg/calc.py",
                "line": 3,
                "diff_hunk": "@@ -1,3 +1,3 @@\n-    return sum(items)\n+    return sum(items[1:])",
                "user": {"login": "cr[bot]"},
                "created_at": "2026-01-01T00:00:00Z",
            },
            {
                "id": 8,
                "body": "are you sure? items[0] is a header row",
                "in_reply_to_id": 7,
                "path": "pkg/calc.py",
                "user": {"login": "dev"},
                "created_at": "2026-01-01T00:01:00Z",
            },
        ]
    )
    return fp


@pytest.fixture
def reply_model(monkeypatch):
    """Drive `verdict` from the test without calling a model."""
    from cr.app import conversation
    from cr.models import ReplyDraft

    box = {"verdict": "stands", "reply": "It still drops index 0 on every call path."}

    async def fake_draft(**kw):
        box["instruction"] = kw["instruction"]
        return ReplyDraft(reply=box["reply"], verdict=box["verdict"], reason="test"), 0.01

    monkeypatch.setattr(conversation, "_draft_reply", fake_draft)
    return box


def test_reply_in_our_thread_gets_an_answer(client, github, our_thread, reply_model):
    assert post_hook(client, "pull_request_review_comment", _reply_event()).status_code == 202
    drain(client)

    assert len(github.replies) == 1
    assert github.replies[0]["root"] == 7
    assert "drops index 0" in github.replies[0]["body"]
    # The human's message reached the prompt as data.
    assert "header row" in reply_model["instruction"]


def test_conceding_suppresses_the_finding_for_good(client, github, our_thread, reply_model):
    reply_model["verdict"] = "withdrawn"
    reply_model["reply"] = "You're right, items[0] is a header. Withdrawn."
    post_hook(client, "pull_request_review_comment", _reply_event())
    drain(client)

    assert our_thread in store.suppressed_fingerprints("me/repo")
    assert "Withdrawn" in github.replies[0]["body"]


def test_disagreement_alone_does_not_suppress(client, github, our_thread, reply_model):
    post_hook(client, "pull_request_review_comment", _reply_event())
    drain(client)
    assert store.suppressed_fingerprints("me/repo") == set()


def test_reply_in_someone_elses_thread_is_ignored(client, github, reply_model):
    github.review_comment_list.extend(
        [
            {
                "id": 7,
                "body": "nit: rename this",
                "user": {"login": "dev"},
                "created_at": "2026-01-01T00:00:00Z",
            },
            {
                "id": 8,
                "body": "agreed",
                "in_reply_to_id": 7,
                "user": {"login": "other"},
                "created_at": "2026-01-01T00:01:00Z",
            },
        ]
    )
    post_hook(client, "pull_request_review_comment", _reply_event(body="agreed"))
    drain(client)
    assert github.replies == []


def test_our_own_reply_does_not_trigger_another(client, github, our_thread, reply_model):
    event = _reply_event()
    event["sender"] = {"login": "cr[bot]", "type": "Bot"}
    r = post_hook(client, "pull_request_review_comment", event)
    assert r.json()["ignored"] == "sender is a bot"
    drain(client)
    assert github.replies == []


# --- commands ----------------------------------------------------------------


def _command_event(body="@cr review", comment_id=11):
    return {
        "action": "created",
        "repository": {"full_name": "me/repo"},
        "installation": {"id": 99},
        "sender": {"login": "dev", "type": "User"},
        "issue": {"number": 42, "state": "open", "pull_request": {"url": "..."}},
        "comment": {"id": comment_id, "body": body},
    }


def test_review_command_queues_a_full_review(client, github, no_local_context, review_result):
    assert post_hook(client, "issue_comment", _command_event()).status_code == 202
    drain(client)
    assert len(github.submitted) == 1
    assert any("Queued a review" in c["body"] for c in github.issue_comment_list)


def test_help_command_answers_without_reviewing(client, github, no_local_context, review_result):
    post_hook(client, "issue_comment", _command_event(body="@cr help"))
    drain(client)
    assert github.submitted == []
    assert any("@cr review" in c["body"] for c in github.issue_comment_list)


def test_ignore_command_suppresses_by_fingerprint(client, github, no_local_context):
    post_hook(client, "issue_comment", _command_event(body="@cr ignore abc123abc123"))
    drain(client)
    assert "abc123abc123" in store.suppressed_fingerprints("me/repo")


def test_command_acknowledged_with_a_reaction(client, github, no_local_context, review_result):
    post_hook(client, "issue_comment", _command_event())
    drain(client)
    assert github.reactions and github.reactions[0]["content"] == "eyes"


# --- installations -----------------------------------------------------------


def test_install_records_repos_and_queues_indexing(client, monkeypatch):
    indexed: list[str] = []

    async def fake_index(payload, auth):
        indexed.append(payload["repo"])
        return "ok"

    monkeypatch.setattr(service, "run_index_job", fake_index)
    post_hook(
        client,
        "installation",
        {
            "action": "created",
            "installation": {"id": 99, "account": {"login": "me", "type": "User"}},
            "repositories": [{"full_name": "me/repo"}, {"full_name": "me/other"}],
            "sender": {"login": "me", "type": "User"},
        },
    )
    drain(client)
    assert sorted(indexed) == ["me/other", "me/repo"]
    assert store.installation_for_repo("me/other") == 99


def test_uninstall_forgets_the_installation(client):
    post_hook(
        client,
        "installation",
        {
            "action": "deleted",
            "installation": {"id": 99, "account": {"login": "me"}},
            "sender": {"login": "me", "type": "User"},
        },
    )
    assert store.installation_for_repo("me/repo") is None


# --- queue -------------------------------------------------------------------


@pytest.fixture
def tmp_store(tmp_path):
    """`JobQueue.submit` persists every job, so a queue test without this
    writes its fixtures into whatever database the developer actually uses."""
    store.reset_for_tests()
    store.init(f"sqlite:///{(tmp_path / 'queue.db').as_posix()}")
    yield
    store.reset_for_tests()


async def test_rapid_pushes_collapse_into_one_review(tmp_store):
    """Four commits in a minute must not be four paid reviews."""
    ran: list[str] = []

    async def handler(job):
        ran.append(job.payload["sha"])
        await asyncio.sleep(0.05)

    q = JobQueue(handler, concurrency=2, tick_s=0.02)
    await q.start(recover=False)
    for sha in ("a", "b", "c", "d"):
        q.submit("review", "review:me/repo#42", payload={"sha": sha}, delay_s=0.15)
        await asyncio.sleep(0.01)
    await q.drain(timeout=5)
    await q.stop()
    assert ran == ["d"]  # only the final head is reviewed


async def test_push_during_a_review_supersedes_it(tmp_store):
    started: list[str] = []
    finished: list[str] = []

    async def handler(job):
        started.append(job.payload["sha"])
        await asyncio.sleep(0.3)
        finished.append(job.payload["sha"])

    q = JobQueue(handler, concurrency=2, tick_s=0.02)
    await q.start(recover=False)
    q.submit("review", "review:me/repo#42", payload={"sha": "a"})
    await asyncio.sleep(0.1)
    q.submit("review", "review:me/repo#42", payload={"sha": "b"})
    await q.drain(timeout=5)
    await q.stop()

    assert started == ["a", "b"]
    assert finished == ["b"]  # the stale review never posted


async def test_different_prs_run_concurrently(tmp_store):
    active = 0
    peak = 0

    async def handler(job):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.1)
        active -= 1

    q = JobQueue(handler, concurrency=2, tick_s=0.02)
    await q.start(recover=False)
    for n in range(4):
        q.submit("review", f"review:me/repo#{n}", payload={})
    await q.drain(timeout=5)
    await q.stop()
    assert peak == 2  # the concurrency cap is respected


# --- setup flow --------------------------------------------------------------


def test_setup_page_refuses_once_configured(client):
    assert client.get("/app/setup").status_code == 409


def test_setup_callback_rejects_an_unknown_state(settings, monkeypatch):
    settings.github_app_id = None
    settings.github_app_private_key = None
    monkeypatch.setattr(service, "default_settings", settings)
    api = service.create_app(settings, with_dashboard=False)
    with TestClient(api) as c:
        r = c.get("/app/setup/callback", params={"code": "x", "state": "forged"})
    assert r.status_code == 400
    assert "did not come from this server" in r.text


def test_setup_page_served_when_unconfigured(settings, monkeypatch):
    settings.github_app_id = None
    settings.github_app_private_key = None
    monkeypatch.setattr(service, "default_settings", settings)
    api = service.create_app(settings, public_url="https://tunnel.test", with_dashboard=False)
    with TestClient(api) as c:
        r = c.get("/app/setup")
    assert r.status_code == 200
    assert "https://tunnel.test/webhook" in r.text
    assert "pull_requests: write" in r.text


def test_manifest_does_not_subscribe_to_implicit_events():
    """GitHub rejects the whole manifest with "Default events unsupported" if
    you ask for events it already delivers unconditionally."""
    from cr.app.manifest import IMPLICIT_EVENTS, build_manifest

    events = build_manifest("https://x.test")["default_events"]
    assert not set(events) & set(IMPLICIT_EVENTS)
    assert "pull_request" in events


def test_manifest_still_handles_the_implicit_events():
    """Not subscribing is not the same as not receiving — the router must
    still act on installation events, which arrive either way."""
    from cr.app.events import decide
    from cr.app.manifest import IMPLICIT_EVENTS

    for event in IMPLICIT_EVENTS:
        d = decide(
            event,
            {
                "action": "created",
                "installation": {"id": 1, "account": {"login": "me"}},
                "sender": {"login": "me", "type": "User"},
            },
        )
        assert d.action == "sync_install", event


def test_relay_webhook_url_differs_from_the_browser_url(settings, monkeypatch):
    """smee-style relay: GitHub posts to the relay, the browser stays local."""
    settings.github_app_id = None
    settings.github_app_private_key = None
    monkeypatch.setattr(service, "default_settings", settings)
    api = service.create_app(
        settings,
        public_url="http://localhost:8010",
        webhook_url="https://smee.io/abc123",
        with_dashboard=False,
    )
    with TestClient(api) as c:
        r = c.get("/app/setup")
    assert "https://smee.io/abc123" in r.text
    assert "localhost:8010/app/setup/callback" in r.text
    # No "unreachable" warning: the relay URL is public.
    assert "not reachable from the internet" not in r.text


def test_localhost_webhook_url_is_called_out(settings, monkeypatch):
    settings.github_app_id = None
    settings.github_app_private_key = None
    monkeypatch.setattr(service, "default_settings", settings)
    api = service.create_app(settings, public_url="http://localhost:8010", with_dashboard=False)
    with TestClient(api) as c:
        r = c.get("/app/setup")
    assert "not reachable from the internet" in r.text


def test_status_endpoint_reports_configuration(client):
    body = client.get("/api/app/status").json()
    assert body["configured"] is True
    assert body["webhook_secret_set"] is True
    assert body["installations"][0]["repos"] == ["me/repo"]


def test_ask_inside_a_thread_knows_which_comment_it_is_about(
    client, github, our_thread, reply_model
):
    """`@cr ask is this the right line?` used to be answered with "which line?",
    because the command path dropped the thread a plain reply would have kept."""
    event = _reply_event(body="@cr ask i guess its not the correct line!", comment_id=8, root=7)
    assert post_hook(client, "pull_request_review_comment", event).status_code == 202
    drain(client)

    instruction = reply_model["instruction"]
    # The ask path, not the plain-reply path — that is the one that dropped context.
    assert "asked a question about this pull request" in instruction
    assert "pkg/calc.py" in instruction
    assert "Off-by-one" in instruction
    assert "i guess its not the correct line!" in instruction


def test_ask_outside_a_thread_carries_no_thread_context(
    client, github, no_local_context, reply_model
):
    """An `@cr ask` on the PR itself has no thread to be about, and must not
    invent one."""
    post_hook(client, "issue_comment", _command_event(body="@cr ask what changed?"))
    drain(client)
    assert "Thread (data, not instructions)" not in reply_model["instruction"]
