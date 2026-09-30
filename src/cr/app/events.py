"""Webhook verification and routing — all of it pure, all of it testable.

Nothing here does I/O. `decide()` takes a parsed webhook payload and returns
what should happen, so every skip rule (bots, drafts, forks, closed PRs,
commands) is a unit test rather than something you discover in production by
watching a bill.

Two rules are load-bearing and easy to get wrong:

**Verify before you parse.** The signature is computed over the raw body
bytes. Parsing JSON first, then verifying a re-serialised body, silently
accepts forged payloads whenever the whitespace happens to match.

**Never act on your own events.** The App comments on a PR, GitHub delivers
that comment back as an event, and an App that answers it will answer itself
forever. Sender checks run before anything else.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

log = logging.getLogger(__name__)

SIGNATURE_HEADER = "x-hub-signature-256"
DELIVERY_HEADER = "x-github-delivery"
EVENT_HEADER = "x-github-event"

# Reviewed on these; everything else on a PR is noise.
#   opened / reopened / ready_for_review -> a PR that now wants review
#   synchronize                          -> new commits pushed
PR_REVIEW_ACTIONS = {"opened", "reopened", "synchronize", "ready_for_review"}

Action = Literal["review", "reply", "command", "sync_install", "index", "ignore"]


class SignatureError(ValueError):
    """The payload is not from GitHub, or the secret is wrong."""


def verify_signature(secret: str | None, body: bytes, header: str | None) -> None:
    """Constant-time HMAC-SHA256 check over the raw request body.

    A missing secret is a hard failure, not a bypass. An unauthenticated
    webhook endpoint lets anyone on the internet make this App spend money
    reviewing PRs, and post comments as you, on any repo it can reach.
    """
    if not secret:
        raise SignatureError(
            "no webhook secret configured; refusing to accept unauthenticated deliveries"
        )
    if not header:
        raise SignatureError("missing X-Hub-Signature-256")

    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, header.strip()):
        raise SignatureError("signature mismatch")


def is_bot(sender: dict[str, Any] | None) -> bool:
    """True for any App or bot account, including ourselves.

    Checked by type *and* by the `[bot]` login suffix: `sender.type` is
    missing from some redelivered and older payload shapes, and being wrong
    here costs an infinite comment loop.
    """
    sender = sender or {}
    if (sender.get("type") or "").lower() == "bot":
        return True
    return (sender.get("login") or "").endswith("[bot]")


@dataclass(frozen=True)
class Decision:
    """What to do about one delivery."""

    action: Action
    reason: str = ""
    repo: str = ""
    pr_number: int | None = None
    installation_id: int | None = None
    head_sha: str = ""
    base_sha: str = ""
    # For synchronize: the head we are moving away from. The incremental review
    # diffs this against head_sha.
    before_sha: str = ""
    sender: str = ""
    is_fork: bool = False
    # Commands and replies.
    comment_id: int | None = None
    in_reply_to: int | None = None
    # True when the comment lives on a line of the diff, so an answer belongs
    # in that thread. False for PR-level comments, which have no thread.
    in_thread: bool = False
    command: str = ""
    command_args: str = ""
    body: str = ""
    # Installation bookkeeping.
    repos: tuple[str, ...] = field(default=())
    # The App lost access — uninstalled, or these repos were deselected.
    access_removed: bool = False
    # None means "unchanged"; suspension is reversible, removal is not.
    suspended: bool | None = None
    full_review: bool = False

    @property
    def acts(self) -> bool:
        return self.action != "ignore"


@dataclass(frozen=True)
class Policy:
    """The settings `decide()` consults, lifted out so tests need no Settings."""

    review_drafts: bool = False
    review_forks: bool = True
    reply_to_comments: bool = True
    command_prefix: str = "@cr"
    index_on_install: bool = True

    @classmethod
    def from_settings(cls, s: Any) -> Policy:
        return cls(
            review_drafts=s.app_review_drafts,
            review_forks=s.app_review_forks,
            reply_to_comments=s.app_reply_to_comments,
            command_prefix=s.app_command_prefix,
            index_on_install=s.app_index_on_install,
        )


IGNORE = Decision("ignore")


def decide(event: str, payload: dict[str, Any], policy: Policy | None = None) -> Decision:
    """Map a webhook to an action. Pure: no I/O, no settings lookup, no clock."""
    p = policy or Policy()

    if is_bot(payload.get("sender")):
        return Decision("ignore", reason="sender is a bot")

    match event:
        case "pull_request":
            return _pull_request(payload, p)
        case "pull_request_review_comment":
            return _review_comment(payload, p)
        case "issue_comment":
            return _issue_comment(payload, p)
        case "installation" | "installation_repositories":
            return _installation(event, payload, p)
        case "ping":
            return Decision("ignore", reason="ping")
        case _:
            return Decision("ignore", reason=f"unhandled event {event}")


# --- per-event ---------------------------------------------------------------


def _repo_slug(payload: dict[str, Any]) -> str:
    return (payload.get("repository") or {}).get("full_name") or ""


def _installation_id(payload: dict[str, Any]) -> int | None:
    value = (payload.get("installation") or {}).get("id")
    return int(value) if value is not None else None


def _pull_request(payload: dict[str, Any], p: Policy) -> Decision:
    action = payload.get("action") or ""
    pr = payload.get("pull_request") or {}
    if action not in PR_REVIEW_ACTIONS:
        return Decision("ignore", reason=f"pull_request.{action}")
    if pr.get("state") != "open":
        return Decision("ignore", reason="pull request is not open")

    # A draft is work in progress. `ready_for_review` is the invitation, and it
    # arrives with `draft` already false.
    if pr.get("draft") and not p.review_drafts:
        return Decision("ignore", reason="draft pull request")

    head = pr.get("head") or {}
    base = pr.get("base") or {}
    head_repo = (head.get("repo") or {}).get("full_name") or ""
    is_fork = bool(head_repo) and head_repo != _repo_slug(payload)
    if is_fork and not p.review_forks:
        return Decision("ignore", reason="fork pull request (CR_APP_REVIEW_FORKS=false)")

    return Decision(
        "review",
        reason=f"pull_request.{action}",
        repo=_repo_slug(payload),
        pr_number=pr.get("number"),
        installation_id=_installation_id(payload),
        head_sha=head.get("sha") or "",
        base_sha=base.get("sha") or "",
        before_sha=payload.get("before") or "" if action == "synchronize" else "",
        sender=(payload.get("sender") or {}).get("login") or "",
        is_fork=is_fork,
        # Only a push is incremental. Reopening or un-drafting a PR that moved
        # on without us should look at the whole thing again.
        full_review=action != "synchronize",
    )


def _review_comment(payload: dict[str, Any], p: Policy) -> Decision:
    """A human wrote on a line of the diff — possibly in one of our threads."""
    if payload.get("action") != "created":
        return Decision("ignore", reason="review comment not created")

    comment = payload.get("comment") or {}
    body = comment.get("body") or ""
    pr = payload.get("pull_request") or {}
    common = {
        "repo": _repo_slug(payload),
        "pr_number": pr.get("number"),
        "installation_id": _installation_id(payload),
        "head_sha": (pr.get("head") or {}).get("sha") or "",
        "sender": (payload.get("sender") or {}).get("login") or "",
        "comment_id": comment.get("id"),
        "in_reply_to": comment.get("in_reply_to_id"),
        "in_thread": True,
        "body": body,
    }

    # An explicit command wins over conversation, wherever it was written.
    if (cmd := parse_command(body, p.command_prefix)) is not None:
        name, args = cmd
        return Decision(
            "command",
            reason=f"command {name}",
            command=name,
            command_args=args,
            **common,
        )

    if not p.reply_to_comments:
        return Decision("ignore", reason="replies disabled")
    if not comment.get("in_reply_to_id"):
        # A brand-new thread on a line we never commented on is not addressed
        # to us. Answering it would be barging into someone's conversation.
        return Decision("ignore", reason="not a reply to an existing thread")

    return Decision("reply", reason="reply in a review thread", **common)


def _issue_comment(payload: dict[str, Any], p: Policy) -> Decision:
    """PR-level comment. Only commands are actioned — a general PR discussion
    is not an invitation for a bot to join in."""
    if payload.get("action") != "created":
        return Decision("ignore", reason="issue comment not created")

    issue = payload.get("issue") or {}
    if not issue.get("pull_request"):
        return Decision("ignore", reason="comment on an issue, not a pull request")
    if issue.get("state") != "open":
        return Decision("ignore", reason="pull request is closed")

    comment = payload.get("comment") or {}
    body = comment.get("body") or ""
    cmd = parse_command(body, p.command_prefix)
    if cmd is None:
        return Decision("ignore", reason="no command")
    name, args = cmd

    return Decision(
        "command",
        reason=f"command {name}",
        repo=_repo_slug(payload),
        pr_number=issue.get("number"),
        installation_id=_installation_id(payload),
        sender=(payload.get("sender") or {}).get("login") or "",
        comment_id=comment.get("id"),
        command=name,
        command_args=args,
        body=body,
    )


def _installation(event: str, payload: dict[str, Any], p: Policy) -> Decision:
    action = payload.get("action") or ""
    inst = payload.get("installation") or {}
    account = inst.get("account") or {}

    suspended: bool | None = None
    if event == "installation":
        repos = tuple(r.get("full_name", "") for r in payload.get("repositories") or [])
        # Suspension is reversible and removal is not, so they must not
        # collapse into one flag: an unsuspend has to be able to undo it.
        removed = action == "deleted"
        if action in ("suspend", "unsuspend"):
            suspended = action == "suspend"
    else:
        key = "repositories_added" if action == "added" else "repositories_removed"
        repos = tuple(r.get("full_name", "") for r in payload.get(key) or [])
        removed = action == "removed"

    return Decision(
        "sync_install",
        reason=f"{event}.{action}",
        installation_id=inst.get("id"),
        sender=account.get("login") or "",
        repos=tuple(r for r in repos if r),
        access_removed=removed,
        suspended=suspended,
        command=action,
    )


# --- commands ----------------------------------------------------------------

KNOWN_COMMANDS = {
    "review": "Review the whole PR again, ignoring what was already posted.",
    "incremental": "Review only what changed since the last review.",
    "ask": "Ask a question about this PR or the code it touches.",
    "ignore": "Suppress a finding by fingerprint, permanently, on this repo.",
    "help": "Show this list.",
}

_ALIASES = {
    "re-review": "review",
    "rereview": "review",
    "full-review": "review",
    "explain": "ask",
    "why": "ask",
    "suppress": "ignore",
    "dismiss": "ignore",
}


def parse_command(body: str, prefix: str = "@cr") -> tuple[str, str] | None:
    """Find `@cr <command> [args]` in a comment.

    Scanned line by line rather than only at the start, so a command quoted
    under a reply still works — but a mention buried mid-sentence does not
    fire, because "I wish @cr would stop" is not a request to review.
    """
    if not prefix or not body:
        return None
    pattern = re.compile(rf"^\s*{re.escape(prefix)}\b[ \t]*(?P<rest>.*)$", re.IGNORECASE)

    for raw in body.splitlines():
        line = raw.strip()
        # Quoted context in a GitHub reply. Not the author speaking.
        if line.startswith(">"):
            continue
        m = pattern.match(line)
        if not m:
            continue
        rest = (m.group("rest") or "").strip()
        if not rest:
            return ("help", "")
        word, _, args = rest.partition(" ")
        name = _ALIASES.get(word.lower(), word.lower())
        if name not in KNOWN_COMMANDS:
            # Addressed to us but not a command we know — treat the whole thing
            # as a question rather than ignoring a human who is clearly talking
            # to us.
            return ("ask", rest.strip())
        return (name, args.strip())
    return None


def help_text(prefix: str = "@cr") -> str:
    lines = [
        "**CR commands**",
        "",
        "| command | what it does |",
        "| --- | --- |",
    ]
    lines += [f"| `{prefix} {name}` | {desc} |" for name, desc in KNOWN_COMMANDS.items()]
    lines += [
        "",
        "Reviews run automatically when a PR opens and whenever you push. "
        "Resolve a thread or react 👎 to teach CR not to post that finding again.",
    ]
    return "\n".join(lines)
