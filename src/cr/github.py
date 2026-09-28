"""GitHub PR integration.

Uses the REST API directly over httpx — four endpoints, no abstraction needed.
`githubkit` stays in the dependency list for the GitHub App (Phase 4), where
installation-token handling actually earns its keep.

Inline comments are posted with `path` + `line` + `side`, NOT the legacy
`position` field. That is the single biggest simplification available here: the
modern API takes real file line numbers, so we skip diff-position anchoring
entirely. GitHub still rejects any line not present in the diff, so every finding
is validated against the commentable-line map first and demoted to the summary
if it does not land.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from cr.models import VerifiedFinding

log = logging.getLogger(__name__)

API = "https://api.github.com"
MARKER = re.compile(r"<!-- cr:([0-9a-f]{12}) -->")
SEVERITY_EMOJI = {"critical": "🛑", "high": "🔴", "medium": "🟡", "low": "🔵"}


@dataclass
class PRRef:
    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


def pr_from_env() -> PRRef | None:
    """Resolve the PR from the GitHub Actions environment.

    Works for `pull_request`, `pull_request_target`, and `issue_comment` on a PR.
    """
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repo:
        return None
    owner, name = repo.split("/", 1)

    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and Path(event_path).is_file():
        try:
            event = json.loads(Path(event_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            event = {}
        number = (event.get("pull_request") or {}).get("number") or (
            event.get("issue") or {}
        ).get("number")
        if number:
            return PRRef(owner, name, int(number))

    # Fallback: refs/pull/123/merge
    ref = os.environ.get("GITHUB_REF", "")
    m = re.search(r"refs/pull/(\d+)/", ref)
    if m:
        return PRRef(owner, name, int(m.group(1)))
    return None


class GitHubPR:
    def __init__(self, ref: PRRef, token: str, *, timeout: float = 30.0) -> None:
        self.ref = ref
        self._c = httpx.Client(
            base_url=API,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "cr-review",
            },
        )

    @property
    def _base(self) -> str:
        return f"/repos/{self.ref.owner}/{self.ref.repo}/pulls/{self.ref.number}"

    def close(self) -> None:
        self._c.close()

    def __enter__(self) -> GitHubPR:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def metadata(self) -> dict[str, Any]:
        r = self._c.get(self._base, headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        return r.json()

    def diff(self) -> str:
        r = self._c.get(self._base, headers={"Accept": "application/vnd.github.v3.diff"})
        r.raise_for_status()
        return r.text

    def posted_fingerprints(self) -> set[str]:
        """Rebuild the set of already-posted findings from markers in comment bodies.

        No database required — the fingerprints live in the comments themselves,
        which is what makes re-runs idempotent. (Same trick as pr-agent's
        `inline_comment_dedup`, reduced to one fingerprint for now.)
        """
        seen: set[str] = set()
        page = 1
        while page <= 10:
            r = self._c.get(
                f"{self._base}/comments",
                params={"per_page": 100, "page": page},
                headers={"Accept": "application/vnd.github+json"},
            )
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            for c in batch:
                seen.update(MARKER.findall(c.get("body") or ""))
            if len(batch) < 100:
                break
            page += 1
        return seen


    def review_comments(self) -> list[dict]:
        """All inline review comments on the PR, paginated."""
        out: list[dict] = []
        page = 1
        while page <= 10:
            r = self._c.get(
                f"{self._base}/comments",
                params={"per_page": 100, "page": page},
                headers={"Accept": "application/vnd.github+json"},
            )
            r.raise_for_status()
            batch = r.json()
            out.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return out

    def resolved_threads(self) -> set[str]:
        """Comment node IDs in threads a human marked resolved.

        Resolution only exists in GraphQL — REST has no notion of it.
        """
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
            r = self._c.post(
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
            r.raise_for_status()
            data = r.json()["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
        except Exception as e:  # noqa: BLE001 - feedback is best-effort
            log.warning("could not read resolved threads: %s", e)
            return set()

        found: set[str] = set()
        for thread in data:
            if not thread.get("isResolved"):
                continue
            for c in thread.get("comments", {}).get("nodes", []):
                found.update(MARKER.findall(c.get("body") or ""))
        return found

    def thumbs_down(self) -> set[str]:
        """Fingerprints of comments a human reacted to with a thumbs-down."""
        found: set[str] = set()
        for c in self.review_comments():
            fps = MARKER.findall(c.get("body") or "")
            if not fps:
                continue
            reactions = c.get("reactions") or {}
            if reactions.get("-1", 0) > 0:
                found.update(fps)
        return found

    def submit_review(self, body: str, comments: list[dict[str, Any]], commit_sha: str) -> None:
        """Post one review containing all inline comments.

        A single review is deliberate: N separate comments generate N notifications
        and read as spam. One review with N threads reads as a review.
        """
        payload: dict[str, Any] = {
            "commit_id": commit_sha,
            "body": body,
            "event": "COMMENT",
        }
        if comments:
            payload["comments"] = comments

        r = self._c.post(
            f"{self._base}/reviews",
            json=payload,
            headers={"Accept": "application/vnd.github+json"},
        )
        if r.status_code == 422 and comments:
            # One bad anchor rejects the whole review. Retry with the summary
            # only rather than losing the entire run.
            log.warning("inline comments rejected (%s); posting summary only", r.text[:200])
            payload.pop("comments")
            payload["body"] = body + "\n\n> Inline anchors were rejected by GitHub; "
            payload["body"] += "findings are listed above."
            r = self._c.post(
                f"{self._base}/reviews",
                json=payload,
                headers={"Accept": "application/vnd.github+json"},
            )
        r.raise_for_status()


def render_comment(v: VerifiedFinding) -> str:
    """One inline comment body, carrying its dedup marker."""
    f = v.finding
    sev = v.final_severity
    emoji = SEVERITY_EMOJI.get(str(sev), "🔵")
    parts = [
        f"{emoji} **{sev.upper()}** · `{f.category}` · {f.confidence:.0%} confidence\n",
        f"\n**{f.claim}**\n",
        f"\n**How it breaks:** {f.failure_scenario}\n",
    ]
    if f.suggested_fix:
        parts.append(f"\n```suggestion\n{f.suggested_fix}\n```\n")
    parts.append(f"\n<!-- cr:{f.fingerprint()} -->")
    return "".join(parts)


def build_review(
    posted: list[VerifiedFinding],
    *,
    commentable: dict[str, set[int]],
    already: set[str],
    tier: str,
    cost: float,
    elapsed: float,
    killed: int,
) -> tuple[str, list[dict[str, Any]]]:
    """Split findings into inline comments and a summary body.

    Anything whose anchor is not a commentable line in this diff is demoted into
    the summary rather than dropped — losing a real finding to an anchoring
    technicality is the worst possible failure.
    """
    comments: list[dict[str, Any]] = []
    demoted: list[VerifiedFinding] = []
    skipped = 0

    for v in posted:
        fp = v.finding.fingerprint()
        if fp in already:
            skipped += 1
            continue
        path, line = v.finding.anchor_file, v.finding.anchor_line
        if line in commentable.get(path, set()):
            comments.append(
                {"path": path, "line": line, "side": "RIGHT", "body": render_comment(v)}
            )
        else:
            demoted.append(v)

    lines = ["## Code review\n"]
    if not posted:
        lines.append("\nNo findings survived verification. ✅\n")
    else:
        lines.append(
            f"\n{len(comments)} inline comment(s)"
            + (f", {len(demoted)} without a diff anchor" if demoted else "")
            + (f", {skipped} already posted" if skipped else "")
            + ".\n"
        )

    for v in demoted:
        f = v.finding
        emoji = SEVERITY_EMOJI.get(str(v.final_severity), "🔵")
        lines.append(
            f"\n### {emoji} `{f.anchor_file}:{f.anchor_line}` — {f.claim}\n"
            f"\n{f.failure_scenario}\n"
            f"\n<!-- cr:{f.fingerprint()} -->\n"
        )

    lines.append(
        f"\n<sub>tier `{tier}` · {killed} finding(s) killed by verification · "
        f"${cost:.4f} · {elapsed:.0f}s</sub>"
    )
    return "".join(lines), comments
