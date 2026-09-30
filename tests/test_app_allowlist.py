"""The allowlist gate.

This is the only thing standing between a public App and a stranger spending
the model budget, so the tests that matter most are the ones asserting that
nothing is queued — not that the right thing is queued.
"""

from __future__ import annotations

import pytest

from cr.app import accounts
from cr.store import db as store

from .test_app_service import _pr_event, drain, post_hook

# client / github / settings / review_result / no_local_context come from
# conftest, so they are requested by name rather than imported here.


@pytest.fixture
def gated(settings):  # noqa: F811
    """The same service, with the allowlist actually switched on."""
    settings.app_require_approval = True
    return settings


def test_unapproved_account_gets_no_review(gated, client, github, no_local_context, review_result):
    """The whole point: an event from an account nobody approved must not reach
    a model call."""
    r = post_hook(client, "pull_request", _pr_event())
    assert r.status_code == 200
    assert r.json().get("blocked")
    drain(client)
    assert github.submitted == []
    # The recorder captures the engine's kwargs; empty means it was never called.
    assert review_result == {}


def test_approved_account_is_reviewed(gated, client, github, no_local_context, review_result):
    store.record_account("me", status="approved", decided_by="test")
    assert post_hook(client, "pull_request", _pr_event()).status_code == 202
    drain(client)
    assert len(github.submitted) == 1


def test_denied_account_stays_denied(gated, client, github, no_local_context, review_result):
    store.record_account("me", status="denied", decided_by="test")
    assert post_hook(client, "pull_request", _pr_event()).json().get("blocked")
    drain(client)
    assert github.submitted == []


def test_pending_is_not_approval(gated, client, github, no_local_context, review_result):
    """Asking for access must not grant it."""
    store.record_account("me", status="pending", requested_by="dev")
    assert post_hook(client, "pull_request", _pr_event()).json().get("blocked")
    drain(client)
    assert github.submitted == []


def test_blocked_events_are_counted(gated, client, github, no_local_context, review_result):
    """An unapproved install must be visible to an admin, or the owner waits
    forever for a review that was silently dropped."""
    post_hook(client, "pull_request", _pr_event(), delivery="d1")
    post_hook(client, "pull_request", _pr_event(action="reopened"), delivery="d2")
    row = next(a for a in store.accounts() if a.login == "me")
    assert row.status == "pending"
    assert row.blocked_events >= 1
    assert row.last_blocked_at is not None


def test_store_failure_denies_rather_than_allows(gated, monkeypatch, client, github, review_result):
    """Failing open would turn a database blip into an open door."""

    def explode(*a, **k):
        raise RuntimeError("db down")

    # Patch the session, not account_status: the swallow-and-deny lives inside
    # account_status, so replacing it would test the mock instead of the code.
    monkeypatch.setattr(store, "session", explode)
    assert store.account_status("me") == "unknown"
    assert accounts.allowed("me") is False


def test_install_events_are_never_blocked(gated, client, github):
    """Install is how an account first appears; blocking it means nobody can
    ever arrive to be approved."""
    event = {
        "action": "created",
        "installation": {
            "id": 99,
            "account": {"login": "newcomer", "type": "Organization"},
        },
        "repositories": [{"full_name": "newcomer/api"}],
        "sender": {"login": "someone", "type": "User"},
    }
    r = post_hook(client, "installation", event)
    assert r.status_code == 202
    assert not r.json().get("blocked")
