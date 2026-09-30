"""Webhook verification, routing and command parsing.

These are the rules that decide whether money gets spent, so they are tested
as pure functions rather than through the server.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from cr.app.events import (
    Decision,
    Policy,
    SignatureError,
    decide,
    help_text,
    is_bot,
    parse_command,
    verify_signature,
)

SECRET = "s3cret"


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# --- signatures --------------------------------------------------------------


def test_valid_signature_passes():
    body = b'{"action":"opened"}'
    verify_signature(SECRET, body, sign(body))


@pytest.mark.parametrize(
    ("secret", "body", "header"),
    [
        (None, b"{}", sign(b"{}")),  # no secret configured
        ("", b"{}", sign(b"{}")),  # empty secret
        (SECRET, b"{}", None),  # unsigned delivery
        (SECRET, b"{}", "sha256=" + "0" * 64),  # wrong digest
        (SECRET, b'{"a":1}', sign(b'{"a":2}')),  # body tampered after signing
        (SECRET, b"{}", "sha1=" + "0" * 40),  # legacy algorithm
    ],
)
def test_bad_signatures_rejected(secret, body, header):
    with pytest.raises(SignatureError):
        verify_signature(secret, body, header)


def test_missing_secret_is_never_a_bypass():
    """An unauthenticated ingress lets anyone spend your model budget."""
    with pytest.raises(SignatureError):
        verify_signature(None, b"{}", None)


# --- bots --------------------------------------------------------------------


@pytest.mark.parametrize(
    "sender",
    [
        {"type": "Bot", "login": "cr-review[bot]"},
        {"type": "bot", "login": "whatever"},
        {"login": "some-app[bot]"},  # type missing from the payload
    ],
)
def test_bot_senders_detected(sender):
    assert is_bot(sender)


def test_humans_are_not_bots():
    assert not is_bot({"type": "User", "login": "robotnik"})


def _pr_payload(**over):
    pr = {
        "number": 42,
        "state": "open",
        "draft": False,
        "head": {"sha": "head1", "repo": {"full_name": "me/repo"}},
        "base": {"sha": "base1"},
    }
    pr.update(over.pop("pull_request", {}))
    payload = {
        "action": "opened",
        "repository": {"full_name": "me/repo"},
        "installation": {"id": 99},
        "sender": {"login": "me", "type": "User"},
        "pull_request": pr,
    }
    payload.update(over)
    return payload


def test_bot_events_never_act():
    """Otherwise the App answers its own comments, forever."""
    payload = _pr_payload(sender={"login": "cr[bot]", "type": "Bot"})
    assert decide("pull_request", payload).action == "ignore"


# --- pull_request ------------------------------------------------------------


def test_opened_queues_a_full_review():
    d = decide("pull_request", _pr_payload())
    assert d.action == "review"
    assert d.full_review is True
    assert (d.repo, d.pr_number, d.installation_id) == ("me/repo", 42, 99)


def test_synchronize_queues_an_incremental_review():
    d = decide("pull_request", _pr_payload(action="synchronize", before="old1"))
    assert d.action == "review"
    assert d.full_review is False
    assert d.before_sha == "old1"


def test_before_sha_only_set_for_pushes():
    d = decide("pull_request", _pr_payload(action="reopened", before="old1"))
    assert d.before_sha == ""


@pytest.mark.parametrize("action", ["closed", "labeled", "assigned", "edited", "review_requested"])
def test_uninteresting_actions_ignored(action):
    assert decide("pull_request", _pr_payload(action=action)).action == "ignore"


def test_drafts_skipped_by_default_and_reviewed_on_request():
    payload = _pr_payload(pull_request={"draft": True})
    assert decide("pull_request", payload).action == "ignore"
    assert decide("pull_request", payload, Policy(review_drafts=True)).action == "review"


def test_ready_for_review_reviews_the_whole_pr():
    d = decide("pull_request", _pr_payload(action="ready_for_review"))
    assert d.action == "review"
    assert d.full_review is True


def test_closed_pull_requests_are_never_reviewed():
    payload = _pr_payload(pull_request={"state": "closed"})
    assert decide("pull_request", payload).action == "ignore"


def test_forks_flagged_and_skippable():
    payload = _pr_payload(
        pull_request={"head": {"sha": "h", "repo": {"full_name": "someone/fork"}}}
    )
    assert decide("pull_request", payload).is_fork is True
    assert decide("pull_request", payload, Policy(review_forks=False)).action == "ignore"


# --- review comments ---------------------------------------------------------


def _comment_payload(body: str, *, in_reply_to: int | None = 7, **over):
    payload = {
        "action": "created",
        "repository": {"full_name": "me/repo"},
        "installation": {"id": 99},
        "sender": {"login": "dev", "type": "User"},
        "pull_request": {"number": 42, "head": {"sha": "head1"}},
        "comment": {"id": 8, "body": body, "in_reply_to_id": in_reply_to},
    }
    payload.update(over)
    return payload


def test_reply_in_a_thread_is_answered():
    d = decide("pull_request_review_comment", _comment_payload("are you sure? x is validated"))
    assert d.action == "reply"
    assert d.comment_id == 8
    assert d.in_reply_to == 7
    assert d.in_thread is True


def test_new_thread_on_an_unrelated_line_is_left_alone():
    """Two humans talking to each other is not an invitation."""
    d = decide(
        "pull_request_review_comment", _comment_payload("nit: rename this", in_reply_to=None)
    )
    assert d.action == "ignore"


def test_replies_can_be_switched_off():
    d = decide(
        "pull_request_review_comment",
        _comment_payload("thoughts?"),
        Policy(reply_to_comments=False),
    )
    assert d.action == "ignore"


def test_command_in_a_new_thread_still_fires():
    d = decide("pull_request_review_comment", _comment_payload("@cr review", in_reply_to=None))
    assert d.action == "command"
    assert d.command == "review"


# --- issue comments ----------------------------------------------------------


def _issue_payload(body: str, **over):
    payload = {
        "action": "created",
        "repository": {"full_name": "me/repo"},
        "installation": {"id": 99},
        "sender": {"login": "dev", "type": "User"},
        "issue": {"number": 42, "state": "open", "pull_request": {"url": "..."}},
        "comment": {"id": 11, "body": body},
    }
    payload.update(over)
    return payload


def test_pr_comment_command_fires():
    d = decide("issue_comment", _issue_payload("@cr review please"))
    assert d.action == "command"
    assert d.command == "review"
    assert d.in_thread is False


def test_pr_chatter_without_a_command_is_ignored():
    assert decide("issue_comment", _issue_payload("looks good to me")).action == "ignore"


def test_comments_on_plain_issues_are_ignored():
    payload = _issue_payload("@cr review")
    payload["issue"].pop("pull_request")
    assert decide("issue_comment", payload).action == "ignore"


# --- installations -----------------------------------------------------------


def test_installation_created_records_repos():
    d = decide(
        "installation",
        {
            "action": "created",
            "installation": {"id": 5, "account": {"login": "me", "type": "User"}},
            "repositories": [{"full_name": "me/a"}, {"full_name": "me/b"}],
            "sender": {"login": "me", "type": "User"},
        },
    )
    assert d.action == "sync_install"
    assert d.repos == ("me/a", "me/b")
    assert d.access_removed is False


def test_installation_deleted_marks_removal():
    d = decide(
        "installation",
        {
            "action": "deleted",
            "installation": {"id": 5, "account": {"login": "me"}},
            "sender": {"login": "me", "type": "User"},
        },
    )
    assert d.access_removed is True
    assert d.suspended is None


@pytest.mark.parametrize(("action", "expected"), [("suspend", True), ("unsuspend", False)])
def test_suspension_is_reversible_and_not_removal(action, expected):
    """Collapsing suspend into removal would make unsuspend un-undoable."""
    d = decide(
        "installation",
        {
            "action": action,
            "installation": {"id": 5, "account": {"login": "me"}},
            "sender": {"login": "me", "type": "User"},
        },
    )
    assert d.suspended is expected
    assert d.access_removed is False


def test_repositories_added_carries_only_the_delta():
    d = decide(
        "installation_repositories",
        {
            "action": "added",
            "installation": {"id": 5, "account": {"login": "me"}},
            "repositories_added": [{"full_name": "me/c"}],
            "sender": {"login": "me", "type": "User"},
        },
    )
    assert d.repos == ("me/c",)


# --- commands ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("@cr review", ("review", "")),
        ("@CR REVIEW", ("review", "")),
        ("@cr re-review", ("review", "")),
        ("@cr incremental", ("incremental", "")),
        ("@cr ask why does this retry twice?", ("ask", "why does this retry twice?")),
        ("@cr explain the change", ("ask", "the change")),
        ("@cr ignore a1b2c3d4e5f6", ("ignore", "a1b2c3d4e5f6")),
        ("@cr", ("help", "")),
        ("@cr help", ("help", "")),
        ("thanks!\n@cr review", ("review", "")),
        ("@cr what about the null case", ("ask", "what about the null case")),
    ],
)
def test_commands_parse(body, expected):
    assert parse_command(body) == expected


@pytest.mark.parametrize(
    "body",
    [
        "no mention here",
        "I wish @cr would stop commenting",  # mid-sentence, not addressed
        "> @cr review",  # quoted context from a reply
        "",
    ],
)
def test_non_commands_do_not_fire(body):
    assert parse_command(body) is None


def test_command_prefix_is_configurable():
    assert parse_command("@mybot review", prefix="@mybot") == ("review", "")
    assert parse_command("@cr review", prefix="@mybot") is None


def test_help_lists_every_command():
    text = help_text("@cr")
    for name in ("review", "incremental", "ask", "ignore", "help"):
        assert f"@cr {name}" in text


# --- unknown events ----------------------------------------------------------


@pytest.mark.parametrize("event", ["push", "star", "workflow_run", "ping"])
def test_unhandled_events_are_ignored(event):
    assert decide(event, {"sender": {"login": "me"}}) == Decision(
        "ignore", reason=decide(event, {"sender": {"login": "me"}}).reason
    )
