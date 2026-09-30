"""Talking back: replies in our own review threads, and `@pullagent` commands.

A review comment is a claim, and a claim someone can argue with is worth more
than one they cannot. This module is the other half of the loop the store was
always built for — `Suppression` already recorded "a human rejected this", and
now a human can say *why*, get an answer, and have the system actually change
its mind in a way that persists.

Three constraints shape everything here:

**Never talk to yourself.** Every path checks the author and the marker before
generating anything. A bot that replies to its own replies bills you forever.

**Only in threads we started.** We answer a reply under one of our comments,
or an explicit `@pullagent`. We do not join conversations between humans.

**Conceding is a write, not a sentence.** When the model withdraws a finding,
that becomes a `Suppression` row for the repo — the same mechanism `cr learn`
uses for resolved threads. Agreeing in prose while quietly planning to repost
next week would be worse than not replying at all.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from cr.app.api import InstallationClient
from cr.app.auth import AppAuth
from cr.app.events import help_text
from cr.config import Settings
from cr.config import settings as default_settings
from cr.diff import parse
from cr.github import MARKER, PRRef
from cr.llm.client import LLMClient, build_pool
from cr.llm.prefix import PRContext, PrefixBuilder, RepoContext
from cr.models import ReplyDraft
from cr.review import prompts
from cr.store import db as store

log = logging.getLogger(__name__)

# A reply is a paragraph, not a report. This is a generous ceiling that still
# stops a runaway essay from landing in someone's inbox.
REPLY_MAX_TOKENS = 4000
REPLY_EFFORT = "medium"

# Marks our conversational replies, distinct from the `cr:<fingerprint>` marker
# on findings — so re-reading a thread never mistakes an answer for a claim.
REPLY_MARKER = "<!-- cr:reply -->"

# How much of the PR diff to show when answering a question. Whole reviews get
# the chunking machinery; a question does not deserve a $2 context window.
ASK_DIFF_CHARS = 40_000


def _as_patch(path: str, hunk: str) -> str:
    """Wrap a GitHub `diff_hunk` into something the line numberer can read.

    `diff_hunk` is a bare `@@ ... @@` fragment with no file headers, which parses
    to zero files — so without this the reply path shows an unnumbered hunk and
    the model is back to counting lines, which is what put the wrong number in
    the comment it is being asked about.
    """
    body = (hunk or "").strip("\n")
    if not body or body.startswith(("---", "diff --git")):
        return body
    name = path or "file"
    return f"--- a/{name}\n+++ b/{name}\n{body}\n"


def _budgeted_diff(unified: str, max_chars: int) -> str:
    """Trim to whole files. A mid-hunk slice does not parse, and an unparseable
    diff silently loses its line numbers exactly when the PR is big enough to
    need them."""
    if len(unified) <= max_chars:
        return unified
    kept: list[str] = []
    used = 0
    for f in parse(unified):
        block = f.patch.rstrip("\n") + "\n"
        if used + len(block) > max_chars:
            break
        kept.append(block)
        used += len(block)
    return "".join(kept) or unified[:max_chars]


@dataclass
class ReplyOutcome:
    posted: bool = False
    verdict: str = ""
    suppressed: str = ""
    skipped: str = ""
    cost_usd: float = 0.0


def _client(s: Settings) -> LLMClient:
    return LLMClient(pool=build_pool(s), max_concurrency=1)


def _model(s: Settings) -> str:
    return s.app_reply_model or s.model_standard


async def handle_reply(
    payload: dict[str, Any], auth: AppAuth, *, settings: Settings | None = None
) -> ReplyOutcome:
    """Answer a human who replied inside one of our review threads."""
    s = settings or default_settings
    repo: str = payload["repo"]
    number = int(payload["pr_number"])
    comment_id = int(payload["comment_id"])
    owner, name = repo.split("/", 1)
    ref = PRRef(owner, name, number)

    async def token() -> str:
        return await auth.installation_token(int(payload["installation_id"]), repositories=[name])

    async with InstallationClient(ref, token) as gh:
        comments = await gh.review_comments()
        by_id = {c["id"]: c for c in comments}
        trigger = by_id.get(comment_id)
        if trigger is None:
            return ReplyOutcome(skipped="comment not found")

        root_id = trigger.get("in_reply_to_id") or comment_id
        thread = _thread(comments, root_id)
        root = by_id.get(root_id) or thread[0]

        fingerprints = MARKER.findall(root.get("body") or "")
        if not fingerprints:
            return ReplyOutcome(skipped="thread was not started by CR")

        # Someone else already answered after them; do not pile on.
        if thread[-1]["id"] != comment_id:
            return ReplyOutcome(skipped="not the latest message in the thread")

        meta = await gh.metadata()
        draft, cost = await _draft_reply(
            repo=repo,
            meta=meta,
            instruction=prompts.reply_instruction(_thread_json(thread, root)),
            diff=_as_patch(root.get("path") or "", root.get("diff_hunk") or ""),
            path="",
            s=s,
        )
        if draft is None:
            return ReplyOutcome(skipped="model produced no reply", cost_usd=cost)

        body = _render(draft)
        await gh.reply_to_comment(root_id, body)

        outcome = ReplyOutcome(posted=True, verdict=draft.verdict, cost_usd=cost)
        if draft.verdict == "withdrawn":
            fp = fingerprints[0]
            if store.suppress(
                repo,
                fp,
                reason="conceded",
                claim=(root.get("body") or "")[:400],
                file=root.get("path") or "",
                note=draft.reason[:400],
                pr_number=number,
            ):
                outcome.suppressed = fp
                log.info("withdrew finding %s on %s after review-thread discussion", fp, repo)
        return outcome


async def handle_command(
    payload: dict[str, Any], auth: AppAuth, *, settings: Settings | None = None
) -> ReplyOutcome:
    """Run an `@pullagent ...` command. Reviews are queued by the caller, not here."""
    s = settings or default_settings
    repo: str = payload["repo"]
    number = int(payload["pr_number"])
    command = payload.get("command") or "help"
    args = (payload.get("command_args") or "").strip()
    comment_id = payload.get("comment_id")
    # A review-comment command must be answered in its thread; an issue-comment
    # command has no thread to answer in.
    in_thread = bool(payload.get("in_thread"))
    owner, name = repo.split("/", 1)
    ref = PRRef(owner, name, number)

    async def token() -> str:
        return await auth.installation_token(int(payload["installation_id"]), repositories=[name])

    async with InstallationClient(ref, token) as gh:
        if comment_id:
            await gh.react(int(comment_id), "eyes", review=in_thread)

        if command == "help":
            await _say(gh, help_text(s.app_command_prefix), comment_id, in_thread)
            return ReplyOutcome(posted=True, verdict="answered")

        if command == "ignore":
            return await _ignore(gh, repo, number, args, comment_id, in_thread)

        if command == "ask":
            if not args:
                await _say(
                    gh,
                    f"Ask me something about this PR: `{s.app_command_prefix} ask "
                    "why does this change the retry path?`",
                    comment_id,
                    in_thread,
                )
                return ReplyOutcome(posted=True, verdict="answered")
            return await _ask(
                gh, repo, args, payload.get("sender") or "there", s, comment_id, in_thread
            )

        # review / incremental are handled by queueing a review job; the caller
        # does that. Acknowledge so the human is not left wondering.
        scope = "the whole PR" if command == "review" else "the changes since the last review"
        await _say(gh, f"Queued a review of {scope}.", comment_id, in_thread)
        return ReplyOutcome(posted=True, verdict="answered")


# --- the model call ----------------------------------------------------------


async def _draft_reply(
    *,
    repo: str,
    meta: dict[str, Any],
    instruction: str,
    diff: str,
    path: str,
    s: Settings,
    graph_slice: str = "",
) -> tuple[ReplyDraft | None, float]:
    """One structured call. Shares `PrefixBuilder`, so the repo-level prefix
    written by a review in the last hour is read back at cache rates here."""
    llm = _client(s)
    model = _model(s)
    pr = PRContext(
        title=meta.get("title") or "",
        description=(meta.get("body") or "")[:2000],
        diff=f"# {path}\n{diff}" if path else diff,
        graph_slice=graph_slice,
    )
    builder = PrefixBuilder(preamble=prompts.REPLY_PREAMBLE, repo=RepoContext(slug=repo), pr=pr)
    try:
        call = await llm.parse(
            model=model,
            schema=ReplyDraft,
            system=builder.system(),
            messages=builder.messages(instruction),
            effort=REPLY_EFFORT,
            max_tokens=REPLY_MAX_TOKENS,
            label="reply",
        )
    except Exception as e:  # noqa: BLE001 - a failed reply is silence, not a crash
        log.warning("reply generation failed for %s: %s", repo, e)
        return None, llm.total_cost_usd()

    draft = call.parsed if isinstance(call.parsed, ReplyDraft) else None
    return draft, llm.total_cost_usd()


# --- commands ----------------------------------------------------------------


async def _ask(
    gh: InstallationClient,
    repo: str,
    question: str,
    asker: str,
    s: Settings,
    comment_id: int | None,
    in_thread: bool,
) -> ReplyOutcome:
    meta = await gh.metadata()
    try:
        diff = _budgeted_diff(await gh.diff(), ASK_DIFF_CHARS)
    except Exception as e:  # noqa: BLE001 - answer from the description if the diff is unreadable
        log.warning("could not read the diff to answer a question on %s: %s", repo, e)
        diff = ""

    # `@pullagent ask` typed inside one of our review threads is almost always *about*
    # that thread — "is this the right line?" means nothing without it. A plain
    # reply already carries this context; the command path used to drop it and
    # then ask the human which comment they meant.
    thread_json = ""
    if in_thread and comment_id:
        try:
            comments = await gh.review_comments()
            by_id = {c["id"]: c for c in comments}
            trigger = by_id.get(int(comment_id)) or {}
            root_id = trigger.get("in_reply_to_id") or int(comment_id)
            thread = _thread(comments, root_id)
            root = by_id.get(root_id) or (thread[0] if thread else None)
            if root is not None:
                thread_json = _thread_json(thread, root)
        except Exception as e:  # noqa: BLE001 - answering without it beats not answering
            log.warning("could not read the thread behind an ask on %s: %s", repo, e)

    draft, cost = await _draft_reply(
        repo=repo,
        meta=meta,
        instruction=prompts.ask_instruction(question, asker, thread_json),
        diff=diff,
        path="",
        s=s,
    )
    if draft is None:
        await _say(
            gh,
            "I could not answer that one — the model call failed. Try again, or rephrase.",
            comment_id,
            in_thread,
        )
        return ReplyOutcome(posted=True, verdict="needs_human", cost_usd=cost)

    await _say(gh, _render(draft), comment_id, in_thread)
    return ReplyOutcome(posted=True, verdict=draft.verdict, cost_usd=cost)


async def _ignore(
    gh: InstallationClient,
    repo: str,
    number: int,
    args: str,
    comment_id: int | None,
    in_thread: bool,
) -> ReplyOutcome:
    """Suppress findings by fingerprint, permanently, on this repo.

    With no argument inside a thread, the thread's own finding is the target —
    which is the only form anyone actually types.
    """
    wanted = {w.strip().strip("`") for w in args.replace(",", " ").split() if w.strip()}

    if not wanted and comment_id and in_thread:
        try:
            comment = await gh.review_comment(int(comment_id))
            root_id = comment.get("in_reply_to_id") or comment_id
            root = await gh.review_comment(int(root_id))
            wanted = set(MARKER.findall(root.get("body") or ""))
        except Exception as e:  # noqa: BLE001
            log.warning("could not resolve the thread's fingerprint: %s", e)

    if not wanted:
        await _say(
            gh,
            "Reply `@pullagent ignore` under the comment you want suppressed, or pass its "
            "fingerprint: `@pullagent ignore a1b2c3d4e5f6`.",
            comment_id,
            in_thread,
        )
        return ReplyOutcome(posted=True, verdict="needs_human")

    done = [
        fp for fp in sorted(wanted) if store.suppress(repo, fp, reason="manual", pr_number=number)
    ]
    already = sorted(wanted - set(done))
    parts = []
    if done:
        which = "them" if len(done) > 1 else "it"
        parts.append(
            f"Suppressed `{'`, `'.join(done)}` — I will not raise {which} on `{repo}` again."
        )
    if already:
        parts.append(f"Already suppressed: `{'`, `'.join(already)}`.")
    await _say(gh, " ".join(parts), comment_id, in_thread)
    return ReplyOutcome(posted=True, verdict="answered", suppressed=",".join(done))


# --- output ------------------------------------------------------------------


def _render(draft: ReplyDraft) -> str:
    body = draft.reply.strip()
    if draft.verdict == "withdrawn":
        body += "\n\n<sub>Withdrawn — this finding will not be raised on this repo again.</sub>"
    elif draft.verdict == "needs_human":
        body += "\n\n<sub>Answered from the diff alone; I could not see enough to be sure.</sub>"
    return f"{body}\n\n{REPLY_MARKER}"


async def _say(gh: InstallationClient, body: str, comment_id: int | None, in_thread: bool) -> None:
    """Answer where we were asked: in the thread, or on the PR."""
    text = body if REPLY_MARKER in body else f"{body}\n\n{REPLY_MARKER}"
    if in_thread and comment_id:
        try:
            comment = await gh.review_comment(int(comment_id))
            root = comment.get("in_reply_to_id") or comment_id
            await gh.reply_to_comment(int(root), text)
            return
        except Exception as e:  # noqa: BLE001 - fall back rather than say nothing
            log.warning("could not reply in thread, commenting on the PR instead: %s", e)
    await gh.comment_on_issue(text)


def _thread(comments: list[dict], root_id: int) -> list[dict]:
    """Every comment in one review thread, oldest first."""
    thread = [c for c in comments if c["id"] == root_id or c.get("in_reply_to_id") == root_id]
    return sorted(thread, key=lambda c: (c.get("created_at") or "", c["id"]))


def _thread_json(thread: list[dict], root: dict) -> str:
    """The conversation as data.

    Bodies are passed through as written — including any `@pullagent` line and any
    instruction-shaped text. The preamble is what makes that safe: this block
    is labelled as untrusted evidence, and the model is told not to obey it.
    """
    return json.dumps(
        {
            "file": root.get("path"),
            "line": root.get("line") or root.get("original_line"),
            "messages": [
                {
                    "author": (c.get("user") or {}).get("login") or "",
                    "is_you": bool(MARKER.search(c.get("body") or ""))
                    or REPLY_MARKER in (c.get("body") or ""),
                    "body": (c.get("body") or "")[:4000],
                }
                for c in thread
            ],
        },
        indent=2,
        ensure_ascii=False,
    )
