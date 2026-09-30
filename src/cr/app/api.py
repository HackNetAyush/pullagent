"""Async GitHub client, scoped to one installation and one repository.

`cr.github.GitHubPR` stays as it is — it is sync, it works, and the Action path
depends on it. The App needs a different shape for three reasons that are not
style: it runs many reviews in one event loop and must not block it, its token
expires and has to be re-fetched per request, and it calls endpoints the CLI
never touches (check runs, threaded replies, commit comparison).

Comment *rendering* is not duplicated: `build_review` and `render_comment` come
from `cr.github`, so a change to how a finding reads on GitHub changes both
delivery paths at once.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from cr.github import MARKER, PRRef

log = logging.getLogger(__name__)

API = "https://api.github.com"

# GitHub refuses the diff media type on very large PRs. That is not an error
# worth failing a review over — we rebuild the diff from the files endpoint.
DIFF_TOO_LARGE = 406

RETRY_STATUS = {500, 502, 503, 504}
MAX_ATTEMPTS = 4
# Never sit on a rate-limit reset longer than this; a job that sleeps for an
# hour looks identical to a hung worker.
MAX_RATE_LIMIT_WAIT_S = 90.0

TokenProvider = Callable[[], Awaitable[str]]


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"GitHub {status}: {message}")
        self.status = status
        self.message = message


class InstallationClient:
    """One repository, one installation, one event loop.

    The token is fetched per request from `token_provider` rather than held,
    so an hour-long review cannot fail at the end holding a token that expired
    at the start.
    """

    def __init__(
        self,
        ref: PRRef,
        token_provider: TokenProvider,
        *,
        api_url: str = API,
        user_agent: str = "cr-review-app",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.ref = ref
        self._token = token_provider
        self._owns_client = client is None
        self._c = client or httpx.AsyncClient(
            base_url=api_url,
            # Reviews are slow; the API is not. A long read timeout only hides
            # a hung connection.
            timeout=httpx.Timeout(45.0, connect=10.0),
            follow_redirects=True,  # renamed repos 301 from the old slug
            headers={
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": user_agent,
            },
        )

    async def aclose(self) -> None:
        if self._owns_client and not self._c.is_closed:
            await self._c.aclose()

    async def __aenter__(self) -> InstallationClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    @property
    def repo_base(self) -> str:
        return f"/repos/{self.ref.owner}/{self.ref.repo}"

    @property
    def _pr(self) -> str:
        return f"{self.repo_base}/pulls/{self.ref.number}"

    # --- transport ----------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        accept: str = "application/vnd.github+json",
        raise_for: bool = True,
        **kw: Any,
    ) -> httpx.Response:
        """One API call, with retries for the failures that are not our fault.

        Retried: 5xx, connection errors, and rate limiting. Not retried:
        anything 4xx, because a 422 means the request was wrong and sending it
        three more times just makes it wrong three more times.
        """
        last: Exception | None = None
        # Popped once, outside the loop: popping inside would silently drop
        # the caller's headers on every retry after the first.
        extra = kw.pop("headers", {})
        for attempt in range(MAX_ATTEMPTS):
            # Re-fetched per attempt so a long retry cannot outlive the token.
            token = await self._token()
            headers = {"Authorization": f"Bearer {token}", "Accept": accept, **extra}
            try:
                r = await self._c.request(method, path, headers=headers, **kw)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last = exc
                if attempt == MAX_ATTEMPTS - 1:
                    break
                await asyncio.sleep(_backoff(attempt))
                continue

            if r.status_code in RETRY_STATUS and attempt < MAX_ATTEMPTS - 1:
                await asyncio.sleep(_backoff(attempt))
                continue
            if _is_rate_limited(r) and attempt < MAX_ATTEMPTS - 1:
                wait = _rate_limit_wait(r)
                log.warning("rate limited on %s %s; waiting %.0fs", method, path, wait)
                await asyncio.sleep(wait)
                continue

            if raise_for and r.status_code >= 400:
                raise GitHubError(r.status_code, r.text[:300])
            return r

        raise GitHubError(0, f"{method} {path} failed after {MAX_ATTEMPTS} attempts: {last}")

    async def _paginate(self, path: str, *, per_page: int = 100, cap: int = 20) -> list[dict]:
        out: list[dict] = []
        page = 1
        while page <= cap:
            r = await self.request("GET", path, params={"per_page": per_page, "page": page})
            batch = r.json()
            if not isinstance(batch, list):
                break
            out.extend(batch)
            if len(batch) < per_page:
                break
            page += 1
        return out

    # --- pull request -------------------------------------------------------

    async def metadata(self) -> dict[str, Any]:
        return (await self.request("GET", self._pr)).json()

    async def diff(self) -> str:
        """The whole PR as a unified diff, with a fallback for giant PRs."""
        r = await self.request(
            "GET", self._pr, accept="application/vnd.github.v3.diff", raise_for=False
        )
        if r.status_code == DIFF_TOO_LARGE:
            log.info("PR too large for the diff media type; rebuilding from the files endpoint")
            return await self.diff_from_files()
        if r.status_code >= 400:
            raise GitHubError(r.status_code, r.text[:300])
        return r.text

    async def diff_from_files(self) -> str:
        """Reconstruct a unified diff from `/pulls/{n}/files`.

        The files endpoint has no size ceiling and hands back per-file patches.
        Reassembling them with real `diff --git` headers is what lets the
        normal parser handle a PR that the diff media type refuses.
        """
        return _unified_from_files(await self._paginate(f"{self._pr}/files", cap=30))

    async def compare_diff(self, base: str, head: str) -> str:
        """Just what changed between two commits — the incremental review.

        Three-dot semantics (GitHub's default for compare) means the diff is
        taken from the merge base, so a rebase or a force-push widens the
        review rather than silently skipping the commits it rewrote.
        """
        path = f"{self.repo_base}/compare/{base}...{head}"
        r = await self.request(
            "GET", path, accept="application/vnd.github.v3.diff", raise_for=False
        )
        if r.status_code == DIFF_TOO_LARGE:
            data = (await self.request("GET", path)).json()
            return _unified_from_files(data.get("files", []))
        if r.status_code >= 400:
            raise GitHubError(r.status_code, r.text[:300])
        return r.text

    async def changed_files(self) -> list[str]:
        return [f["filename"] for f in await self._paginate(f"{self._pr}/files", cap=30)]

    # --- comments -----------------------------------------------------------

    async def review_comments(self) -> list[dict]:
        return await self._paginate(f"{self._pr}/comments", cap=10)

    async def issue_comments(self) -> list[dict]:
        return await self._paginate(f"{self.repo_base}/issues/{self.ref.number}/comments", cap=10)

    async def review_comment(self, comment_id: int) -> dict:
        return (await self.request("GET", f"{self.repo_base}/pulls/comments/{comment_id}")).json()

    async def posted_fingerprints(self) -> set[str]:
        """Findings already on this PR, read back out of our own markers.

        No extra state: the fingerprints live in the comment bodies, which is
        what makes a re-review on a new push idempotent even if our database
        were wiped.
        """
        seen: set[str] = set()
        for c in await self.review_comments():
            seen.update(MARKER.findall(c.get("body") or ""))
        for c in await self.issue_comments():
            seen.update(MARKER.findall(c.get("body") or ""))
        return seen

    async def submit_review(
        self, body: str, comments: list[dict[str, Any]], commit_sha: str
    ) -> dict:
        """One review with N inline threads — not N comments, which read as spam."""
        payload: dict[str, Any] = {"commit_id": commit_sha, "body": body, "event": "COMMENT"}
        if comments:
            payload["comments"] = comments

        r = await self.request("POST", f"{self._pr}/reviews", json=payload, raise_for=False)
        if r.status_code == 422 and comments:
            # One unanchorable comment rejects the entire review. Losing every
            # finding to one bad line number is the worst available outcome.
            log.warning("inline comments rejected (%s); posting summary only", r.text[:200])
            payload.pop("comments")
            payload["body"] = (
                body + "\n\n> GitHub rejected the inline anchors for this review; "
                "the findings are listed above."
            )
            r = await self.request("POST", f"{self._pr}/reviews", json=payload, raise_for=False)
        if r.status_code >= 400:
            raise GitHubError(r.status_code, r.text[:300])
        return r.json()

    async def reply_to_comment(self, comment_id: int, body: str) -> dict:
        """Reply inside an existing review thread, not as a new top-level thread."""
        r = await self.request(
            "POST", f"{self._pr}/comments/{comment_id}/replies", json={"body": body}
        )
        return r.json()

    async def comment_on_issue(self, body: str) -> dict:
        r = await self.request(
            "POST", f"{self.repo_base}/issues/{self.ref.number}/comments", json={"body": body}
        )
        return r.json()

    async def react(self, comment_id: int, content: str = "eyes", *, review: bool = True) -> None:
        """Acknowledge a command immediately. A review takes minutes; a 👀
        within a second is the difference between 'working' and 'broken'."""
        kind = "pulls/comments" if review else "issues/comments"
        await self.request(
            "POST",
            f"{self.repo_base}/{kind}/{comment_id}/reactions",
            json={"content": content},
            raise_for=False,
        )

    # --- feedback signals ---------------------------------------------------

    async def resolved_threads(self) -> set[str]:
        """Fingerprints in threads a human marked resolved. GraphQL only."""
        query = """
        query($owner:String!,$name:String!,$number:Int!) {
          repository(owner:$owner,name:$name) {
            pullRequest(number:$number) {
              reviewThreads(first:100) {
                nodes { isResolved comments(first:50) { nodes { body } } }
              }
            }
          }
        }
        """
        try:
            r = await self.request(
                "POST",
                "/graphql",
                json={
                    "query": query,
                    "variables": {
                        "owner": self.ref.owner,
                        "name": self.ref.repo,
                        "number": self.ref.number,
                    },
                },
            )
            nodes = r.json()["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
        except Exception as e:  # noqa: BLE001 - feedback is best-effort, never blocking
            log.warning("could not read resolved threads: %s", e)
            return set()

        found: set[str] = set()
        for thread in nodes:
            if thread.get("isResolved"):
                for c in thread.get("comments", {}).get("nodes", []):
                    found.update(MARKER.findall(c.get("body") or ""))
        return found

    async def thumbs_down(self) -> set[str]:
        found: set[str] = set()
        for c in await self.review_comments():
            fps = MARKER.findall(c.get("body") or "")
            if fps and (c.get("reactions") or {}).get("-1", 0) > 0:
                found.update(fps)
        return found

    # --- check runs ---------------------------------------------------------

    async def create_check_run(self, head_sha: str, *, name: str = "CR review") -> int | None:
        """A check run is how a reviewer sees that we are working, and where the
        result lands if the PR has no findings worth a comment. Best-effort:
        Apps installed without `checks: write` still review fine."""
        r = await self.request(
            "POST",
            f"{self.repo_base}/check-runs",
            json={"name": name, "head_sha": head_sha, "status": "in_progress"},
            raise_for=False,
        )
        if r.status_code >= 400:
            log.info("check run not created (%s); continuing without one", r.status_code)
            return None
        return r.json().get("id")

    async def complete_check_run(
        self,
        check_id: int | None,
        *,
        conclusion: str,
        title: str,
        summary: str,
    ) -> None:
        if check_id is None:
            return
        await self.request(
            "PATCH",
            f"{self.repo_base}/check-runs/{check_id}",
            json={
                "status": "completed",
                "conclusion": conclusion,
                "output": {"title": title[:255], "summary": summary[:65000]},
            },
            raise_for=False,
        )


# --- helpers ----------------------------------------------------------------


def _backoff(attempt: int) -> float:
    """Exponential with jitter. Without the jitter, four workers that hit the
    same 502 retry in lockstep forever."""
    return min(8.0, 0.5 * (2**attempt)) * (0.5 + random.random())  # noqa: S311 - not crypto


def _is_rate_limited(r: httpx.Response) -> bool:
    if r.status_code == 429:
        return True
    if r.status_code != 403:
        return False
    return r.headers.get("x-ratelimit-remaining") == "0" or "rate limit" in r.text.lower()


def _rate_limit_wait(r: httpx.Response) -> float:
    retry_after = r.headers.get("retry-after")
    if retry_after:
        try:
            return min(float(retry_after), MAX_RATE_LIMIT_WAIT_S)
        except ValueError:
            pass
    reset = r.headers.get("x-ratelimit-reset")
    if reset:
        try:
            return max(1.0, min(float(reset) - time.time(), MAX_RATE_LIMIT_WAIT_S))
        except ValueError:
            pass
    return 5.0


def _unified_from_files(files: list[dict]) -> str:
    """Rebuild a unified diff from GitHub's per-file patch objects.

    The patches themselves are already unified-diff hunks; what they lack are
    the `diff --git` / `---` / `+++` headers the parser needs to know which
    file each hunk belongs to. Files with no `patch` (binary, or truncated by
    GitHub) are skipped rather than emitted as empty patches, which would
    otherwise inflate the file count triage routes on.
    """
    out: list[str] = []
    for f in files:
        patch = f.get("patch")
        if not patch:
            continue
        new = f.get("filename", "")
        old = f.get("previous_filename") or new
        status = f.get("status", "")
        a = "/dev/null" if status == "added" else f"a/{old}"
        b = "/dev/null" if status == "removed" else f"b/{new}"
        out.append(f"diff --git a/{old} b/{new}\n--- {a}\n+++ {b}\n{patch.rstrip()}\n")
    return "".join(out)
