"""The review job.

`cr review-pr` and this file do the same six things — fetch, triage, lint,
gather context, review, post — and both call `cr.review.engine.review` to do
the part that matters. What the App adds is everything that only makes sense
when nobody typed a command:

* **Incremental review.** On a push we diff the last reviewed head against the
  new one and review that, not the whole PR again. Ten commits on a PR should
  cost ten small reviews, not ten full ones. Findings are deduped by
  fingerprint either way, so this is a cost decision, never a correctness one.
* **Learning before reviewing.** Threads the author resolved and comments they
  thumbed down are recorded as suppressions *first*, so a re-review cannot
  repeat a finding a human already rejected thirty seconds ago.
* **Cancellation.** A push during a review supersedes it. The job must leave
  no half-posted review and no row stuck at `running`.
* **A check run**, so the PR shows that a review is happening, and so a clean
  review has somewhere to land that is not another comment.

Blocking work — git, linters, the symbol index — is pushed to threads. This
job shares an event loop with the webhook ingress, and a 60-second clone that
blocks it is a stack of GitHub delivery timeouts.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cr.app.api import InstallationClient
from cr.app.auth import AppAuth
from cr.config import TIERS, Settings
from cr.config import settings as default_settings
from cr.diff import DiffSet, parse
from cr.github import PRRef, build_review
from cr.lint import LintResult, analyse
from cr.llm.prefix import PRContext, RepoContext
from cr.models import ReviewResult
from cr.repo import RepoCache, default_cache_dir
from cr.review.engine import review as run_review
from cr.store import db as store
from cr.triage import triage
from cr.warm import context_for_pr

log = logging.getLogger(__name__)

# Trigger labels, recorded on the run so the ledger can tell an automatic
# review from one a human asked for.
TRIGGER_AUTO = "auto"
TRIGGER_COMMAND = "command"


@dataclass
class ReviewOutcome:
    repo: str
    pr_number: int
    head_sha: str = ""
    tier: str = ""
    posted: int = 0
    incremental: bool = False
    skipped: str = ""
    error: str = ""
    cost_usd: float = 0.0
    elapsed_s: float = 0.0
    result: ReviewResult | None = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass
class LocalContext:
    """What the on-disk repo mirror can tell us about this PR."""

    graph_slice: str = ""
    lint: LintResult = field(default_factory=LintResult)


async def run_review_job(
    payload: dict[str, Any],
    auth: AppAuth,
    *,
    settings: Settings | None = None,
) -> ReviewOutcome:
    """Review one PR and post the result. Raises only on programmer error."""
    s = settings or default_settings
    repo: str = payload["repo"]
    number = int(payload["pr_number"])
    installation_id = payload.get("installation_id")
    owner, name = repo.split("/", 1)
    ref = PRRef(owner, name, number)

    started = time.monotonic()
    outcome = ReviewOutcome(repo=repo, pr_number=number)
    if installation_id is None:
        # Nothing downstream can authenticate. Fail here with a message that
        # names the cause, rather than a TypeError from deep in the token call.
        outcome.error = f"no installation id for {repo}#{number}; is the App still installed?"
        return outcome

    async def token() -> str:
        return await auth.installation_token(int(installation_id), repositories=[name])

    run_id: int | None = None
    check_id: int | None = None

    async with InstallationClient(ref, token) as gh:
        try:
            meta = await gh.metadata()
        except Exception as exc:  # noqa: BLE001 - a deleted or private PR is not a crash
            outcome.error = f"could not read {repo}#{number}: {exc}"
            log.warning("%s", outcome.error)
            return outcome

        # The webhook payload is already old by the time a debounced job runs.
        # The API is the only trustworthy source for the current head.
        head_sha = (meta.get("head") or {}).get("sha") or ""
        base_sha = (meta.get("base") or {}).get("sha") or ""
        outcome.head_sha = head_sha

        if meta.get("state") != "open" or meta.get("merged"):
            outcome.skipped = "pull request is closed"
            return outcome
        if meta.get("draft") and not s.app_review_drafts:
            outcome.skipped = "draft pull request"
            return outcome

        try:
            # Teach first: a finding the author dismissed a minute ago must not
            # come back in the review we are about to run.
            await _absorb_feedback(gh, repo)

            full_diff_text = await gh.diff()
            full = DiffSet(files=parse(full_diff_text), base=base_sha, head=head_sha)
            if not full.files:
                outcome.skipped = "empty diff"
                return outcome

            review_set, incremental, since = await _diff_to_review(
                gh, full, payload, repo, number, head_sha, s
            )
            outcome.incremental = incremental
            if not review_set.files:
                outcome.skipped = (
                    f"no reviewable code changed since {since[:8]}"
                    if since
                    else "nothing to review"
                )
                store.record_pr_review(
                    repo,
                    number,
                    head_sha=head_sha,
                    base_sha=base_sha,
                    installation_id=installation_id,
                )
                return outcome

            decision = triage(review_set)
            forced = payload.get("tier")
            if decision.is_skip and not forced:
                outcome.tier = decision.tier
                outcome.skipped = f"triage: {decision.reason}"
                store.record_pr_review(
                    repo,
                    number,
                    head_sha=head_sha,
                    base_sha=base_sha,
                    installation_id=installation_id,
                )
                return outcome

            cfg = TIERS[str(forced).upper()] if forced else decision.config
            assert cfg is not None
            outcome.tier = cfg.name

            check_id = await gh.create_check_run(head_sha)

            reviewable = {f.path for f in decision.reviewable}
            local = await asyncio.to_thread(
                _gather_local_context,
                repo,
                number,
                head_sha,
                base_sha,
                reviewable,
                await token(),
            )

            pr_ctx = PRContext(
                title=meta.get("title") or f"PR #{number}",
                description=(meta.get("body") or "")[:4000],
                diff=DiffSet(files=[f for f in review_set.files if f.path in reviewable]).render(),
                graph_slice=local.graph_slice,
                lint_output=local.lint.output,
                suppressed_rules=local.lint.rules,
            )

            def capture(rid: int | None) -> None:
                nonlocal run_id
                run_id = rid

            result = await run_review(
                repo=RepoContext(slug=repo),
                pr=pr_ctx,
                tier=cfg,
                source="app",
                pr_number=number,
                head_sha=head_sha,
                actor=(meta.get("user") or {}).get("login") or "",
                on_start=capture,
            )
            outcome.result = result
            outcome.cost_usd = result.cost_usd

            if result.errors:
                # An incomplete review is worse than no review: the author
                # reads silence on a file as "checked, nothing found".
                outcome.error = "; ".join(result.errors)[:500]
                await _finish_check(
                    gh,
                    check_id,
                    conclusion="neutral",
                    title="Review incomplete",
                    summary=(
                        "CR could not finish this review, so nothing was posted.\n\n"
                        f"```\n{outcome.error}\n```"
                    ),
                )
                return outcome

            posted = await _post(
                gh, result, full, review_set, head_sha, incremental, since, payload.get("row_id")
            )
            outcome.posted = posted

            store.record_pr_review(
                repo,
                number,
                head_sha=head_sha,
                base_sha=base_sha,
                comments=posted,
                installation_id=installation_id,
            )
            await _finish_check(
                gh,
                check_id,
                conclusion="neutral" if posted else "success",
                title=f"{posted} finding(s)" if posted else "No findings",
                summary=_check_summary(result, posted, incremental, since),
            )
            return outcome

        except asyncio.CancelledError:
            # A newer commit arrived. Say so on the check run rather than
            # leaving a spinner that never resolves.
            store.close_run(run_id, status="cancelled", error="superseded by a newer commit")
            await _finish_check(
                gh,
                check_id,
                conclusion="cancelled",
                title="Superseded",
                summary="A newer commit arrived; this review was cancelled in favour of it.",
            )
            raise
        except Exception as exc:  # noqa: BLE001 - one bad PR must not kill the worker
            log.exception("review of %s#%s failed", repo, number)
            outcome.error = f"{type(exc).__name__}: {exc}"[:500]
            store.close_run(run_id, status="failed", error=outcome.error)
            await _finish_check(
                gh,
                check_id,
                conclusion="failure",
                title="Review failed",
                summary=f"```\n{outcome.error}\n```",
            )
            return outcome
        finally:
            outcome.elapsed_s = time.monotonic() - started


# --- pieces -----------------------------------------------------------------


async def _diff_to_review(
    gh: InstallationClient,
    full: DiffSet,
    payload: dict[str, Any],
    repo: str,
    number: int,
    head_sha: str,
    s: Settings,
) -> tuple[DiffSet, bool, str]:
    """Pick between the whole PR and just what landed since we last looked.

    Returns (diff to review, was it incremental, the sha we compared from).
    """
    if payload.get("full_review") or not s.app_incremental:
        return full, False, ""

    state = store.pr_state(repo, number)
    since = (state.last_reviewed_sha if state else "") or payload.get("before_sha") or ""
    if not since or since == head_sha:
        return full, False, ""

    try:
        text = await gh.compare_diff(since, head_sha)
    except Exception as e:  # noqa: BLE001 - a rewritten history loses the old sha
        log.info(
            "no incremental diff %s..%s (%s); reviewing the whole PR", since[:8], head_sha[:8], e
        )
        return full, False, ""

    files = parse(text)
    if not files:
        return DiffSet(files=[], base=since, head=head_sha), True, since

    # A merge from the base branch shows up in this comparison but was not
    # written by the PR author. Reviewing it means commenting on other
    # people's merged code, on someone else's PR.
    in_pr = {f.path for f in full.files}
    kept = [f for f in files if f.path in in_pr]
    if not kept:
        return DiffSet(files=[], base=since, head=head_sha), True, since

    log.info(
        "incremental review of %s#%s: %d of %d changed file(s) since %s",
        repo,
        number,
        len(kept),
        len(full.files),
        since[:8],
    )
    return DiffSet(files=kept, base=since, head=head_sha), True, since


async def _absorb_feedback(gh: InstallationClient, repo: str) -> int:
    """Record resolved threads and 👎 as suppressions. Best-effort, always."""
    try:
        resolved, down = await asyncio.gather(gh.resolved_threads(), gh.thumbs_down())
        if not resolved and not down:
            return 0
        bodies: dict[str, tuple[str, str]] = {}
        from cr.github import MARKER

        for c in await gh.review_comments():
            for fp in MARKER.findall(c.get("body") or ""):
                bodies[fp] = ((c.get("body") or "")[:400], c.get("path") or "")

        added = 0
        for fps, reason in ((resolved, "resolved"), (down, "thumbs_down")):
            for fp in fps:
                body, path = bodies.get(fp, ("", ""))
                if store.suppress(
                    repo, fp, reason=reason, claim=body, file=path, pr_number=gh.ref.number
                ):
                    added += 1
        if added:
            log.info("learned %d new suppression(s) on %s before reviewing", added, repo)
        return added
    except Exception as e:  # noqa: BLE001 - never block a review on feedback bookkeeping
        log.warning("could not absorb feedback for %s: %s", repo, e)
        return 0


def _gather_local_context(
    repo: str,
    number: int,
    head_sha: str,
    base_sha: str,
    changed: set[str],
    token: str,
) -> LocalContext:
    """Cross-file context and linter output, from the on-disk mirror.

    Runs in a worker thread: git and the linters are blocking, and the event
    loop underneath is also serving webhooks.

    Nothing here executes repository code. A fork PR is fetched and checked
    out — the same thing `git fetch` does on your laptop — and read by the
    symbol index and by linters that parse rather than run. Until the sandbox
    lands (backlog CR-08…CR-13), that boundary is the security story, so it
    must not quietly grow a build step.
    """
    out = LocalContext()
    try:
        out.graph_slice = context_for_pr(repo, number, head_sha, base_sha, changed, token)
    except Exception as e:  # noqa: BLE001 - context is an enhancement, never a dependency
        log.warning("graph context unavailable for %s: %s", repo, e)

    try:
        cache = RepoCache(default_cache_dir())
        cache.ensure(repo, token, fetch=False)
        if not cache.has_commit(repo, head_sha):
            cache.fetch_pr(repo, number, token)
        with cache.worktree(repo, head_sha) as tree:
            out.lint = analyse(str(tree), sorted(changed))
    except Exception as e:  # noqa: BLE001 - a missing linter is not a failed review
        log.info("lint skipped for %s: %s", repo, e)
    return out


async def _post(
    gh: InstallationClient,
    result: ReviewResult,
    full: DiffSet,
    reviewed: DiffSet,
    head_sha: str,
    incremental: bool,
    since: str,
    row_id: int | None = None,
) -> int:
    """Submit the review. Shielded: a supersede here would half-post.

    Anchors are validated against the *whole* PR diff, not the incremental
    slice. GitHub accepts an inline comment on any line in the PR's diff, and
    a finding about code from an earlier commit is still worth anchoring where
    the reader can see it.
    """
    # Last chance to notice the work is stale. In-process, a newer push cancels
    # this task outright; across replicas nothing can, so a review started
    # before the latest push would otherwise comment on a commit that has
    # already been replaced.
    if not store.job_is_current(row_id):
        log.info("not posting: a newer push superseded this review")
        return 0

    already = await gh.posted_fingerprints()
    fresh = [v for v in result.posted if v.finding.fingerprint() not in already]
    body, comments = build_review(
        result.posted,
        commentable=full.commentable_map(),
        new_text=full.new_text_map(),
        already=already,
        tier=result.tier,
        cost=result.cost_usd,
        elapsed=result.elapsed_s,
        killed=len(result.suppressed),
    )
    if incremental and since:
        body = body.replace(
            "## Code review\n",
            f"## Code review\n\n<sub>Incremental: reviewing "
            f"`{since[:8]}...{head_sha[:8]}` — {reviewed.total_files} file(s).</sub>\n",
            1,
        )

    if not fresh:
        # Either nothing was found, or everything found is already on the PR.
        # The check run carries that result; a second "no new issues" comment
        # on every push is exactly the noise this project exists to avoid.
        log.info("nothing new to post on %s#%s", gh.ref.slug, gh.ref.number)
        return 0

    # A supersede landing mid-POST would leave a review half-submitted, so the
    # request is allowed to finish even as this job is being cancelled.
    task = asyncio.create_task(gh.submit_review(body, comments, head_sha))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise
    log.info("posted %d inline comment(s) on %s#%s", len(comments), gh.ref.slug, gh.ref.number)
    return len(comments)


async def _finish_check(
    gh: InstallationClient, check_id: int | None, *, conclusion: str, title: str, summary: str
) -> None:
    if check_id is None:
        return
    task = asyncio.create_task(
        gh.complete_check_run(check_id, conclusion=conclusion, title=title, summary=summary)
    )
    try:
        # Shielded for the cancellation path: the whole point of the
        # "superseded" conclusion is that it still gets written.
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise
    except Exception as e:  # noqa: BLE001 - status reporting is not the product
        log.warning("could not complete check run: %s", e)


def _check_summary(result: ReviewResult, posted: int, incremental: bool, since: str) -> str:
    scope = f"Incremental — changes since `{since[:8]}`." if incremental and since else "Full PR."
    lines = [
        scope,
        "",
        f"- tier `{result.tier}`",
        f"- {posted} comment(s) posted",
        f"- {len(result.refuted)} finding(s) killed by verification",
        f"- ${result.cost_usd:.4f}, {result.elapsed_s:.0f}s"
        + (" (cache replay, $0)" if result.cache_hit else ""),
    ]
    if result.memory_suppressed:
        lines.append(f"- {len(result.memory_suppressed)} suppressed by earlier human feedback")
    return "\n".join(lines)


# --- indexing ----------------------------------------------------------------


async def run_index_job(payload: dict[str, Any], auth: AppAuth) -> str:
    """Warm a repo's symbol index so its first PR review is not its slowest.

    Queued when the App is installed. Without it the first review pays a full
    clone and index inline, which on a large repo is minutes of a reviewer
    watching a spinner.
    """
    from cr.warm import index_repo

    repo: str = payload["repo"]
    installation_id = payload.get("installation_id")
    name = repo.split("/", 1)[1]
    token = await auth.installation_token(int(installation_id), repositories=[name])

    result = await asyncio.to_thread(index_repo, repo, token)
    log.info(
        "indexed %s@%s: %d files, %d symbols in %.0fs%s",
        repo,
        result.commit[:8],
        result.files,
        result.symbols,
        result.seconds,
        " (reused)" if result.reused else "",
    )
    return f"{result.files} files, {result.symbols} symbols"


def cache_dir() -> Path:
    return default_cache_dir()
