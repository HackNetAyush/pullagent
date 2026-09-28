"""Benchmark mining, c-CRAB style.

Ground truth is what human reviewers actually flagged on real merged PRs — not
bugs we planted. For each mined PR we record the reviewer's comment and the exact
commit they were looking at, then ask whether our agent raises the same concern
when reviewing that same state.

Two things make this honest rather than flattering:

1. **We review the commit the human reviewed**, not the merged result. Reviewing
   the final state would mean grading ourselves on code where the issue is
   already fixed.
2. **An LLM judge decides whether two texts describe the same concern.** Keyword
   matching cannot tell "this can be null here" from "null check needed
   elsewhere"; it rewards vocabulary overlap rather than agreement.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

API = "https://api.github.com"

BOT_MARKERS = ("[bot]", "dependabot", "codecov", "coderabbit", "sonarcloud", "github-actions")

# Comments that carry no reviewable claim. Cheap prefilter before the LLM judge.
NOISE = re.compile(
    r"^\s*(lgtm|nit:?\s*$|thanks|thank you|done|fixed|\+1|ship it|nice|good catch|"
    r"same here|ditto|👍|:\+1:)\s*[.!]?\s*$",
    re.I,
)
MIN_COMMENT_CHARS = 60


@dataclass
class MinedComment:
    pr: int
    title: str
    base_sha: str
    head_sha: str
    file: str
    line: int | None
    body: str
    author: str
    url: str


def _clean(body: str) -> str:
    body = re.sub(r"```[\s\S]*?```", " [code] ", body)
    body = re.sub(r"<!--[\s\S]*?-->", " ", body)
    return re.sub(r"\s+", " ", body).strip()


def mine(
    slug: str,
    token: str,
    *,
    limit_prs: int = 30,
    max_comments_per_pr: int = 3,
    timeout: float = 30.0,
) -> list[MinedComment]:
    """Collect substantive human review comments from recently merged PRs."""
    owner, repo = slug.split("/")
    out: list[MinedComment] = []

    with httpx.Client(
        base_url=API,
        timeout=timeout,
        headers={
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Accept": "application/vnd.github+json",
            "User-Agent": "cr-bench",
        },
    ) as c:
        prs: list[dict[str, Any]] = []
        page = 1
        while len(prs) < limit_prs * 3 and page <= 5:
            r = c.get(
                f"/repos/{owner}/{repo}/pulls",
                params={"state": "closed", "per_page": 100, "page": page, "sort": "updated"},
            )
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            prs.extend(p for p in batch if p.get("merged_at"))
            page += 1

        for pr in prs:
            if len({m.pr for m in out}) >= limit_prs:
                break
            number = pr["number"]
            rc = c.get(f"/repos/{owner}/{repo}/pulls/{number}/comments", params={"per_page": 100})
            if rc.status_code != 200:
                continue

            kept = 0
            for com in rc.json():
                if kept >= max_comments_per_pr:
                    break
                author = (com.get("user") or {}).get("login", "")
                if any(b in author.lower() for b in BOT_MARKERS):
                    continue
                body = _clean(com.get("body") or "")
                if len(body) < MIN_COMMENT_CHARS or NOISE.match(body):
                    continue
                # The commit the reviewer was actually looking at.
                head = com.get("original_commit_id") or com.get("commit_id")
                if not head:
                    continue
                out.append(
                    MinedComment(
                        pr=number,
                        title=pr.get("title") or "",
                        base_sha=(pr.get("base") or {}).get("sha", ""),
                        head_sha=head,
                        file=com.get("path") or "",
                        line=com.get("original_line") or com.get("line"),
                        body=body[:1200],
                        author=author,
                        url=com.get("html_url") or "",
                    )
                )
                kept += 1
    return out


def to_fixture(slug: str, comments: list[MinedComment], name: str) -> dict[str, Any]:
    """Group mined comments into one fixture per PR."""
    by_pr: dict[int, list[MinedComment]] = {}
    for m in comments:
        by_pr.setdefault(m.pr, []).append(m)

    prs = []
    for number, items in sorted(by_pr.items()):
        first = items[0]
        prs.append(
            {
                "pr": f"{slug}#{number}",
                "title": first.title,
                "base_sha": first.base_sha,
                "head_sha": first.head_sha,
                "human_reviews": [
                    {
                        "id": f"{number}-{i}",
                        "file": m.file,
                        "line": m.line,
                        "comment": m.body,
                        "author": m.author,
                        "url": m.url,
                    }
                    for i, m in enumerate(items)
                ],
            }
        )

    return {
        "name": name,
        "kind": "human_review",
        "source": (
            "Mined from merged PRs. Ground truth is what human reviewers flagged, "
            "evaluated at the commit they reviewed (c-CRAB methodology)."
        ),
        "repo": slug,
        "prs": prs,
    }


def save_fixture(fixture: dict[str, Any], directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    slug = fixture["repo"].replace("/", "__")
    path = directory / f"bench-{slug}.json"
    path.write_text(json.dumps(fixture, indent=2), encoding="utf-8")
    return path


# --- the judge ---------------------------------------------------------------


class Match(BaseModel):
    """Did our finding raise the same concern the human raised?"""

    same_concern: bool = Field(
        description="True only if both describe the same underlying problem in the same code"
    )
    reasoning: str = Field(description="One sentence. Name the shared defect, or why they differ.")


JUDGE_PREAMBLE = """You compare a human reviewer's comment against findings from an \
automated reviewer, on the same pull request.

Decide whether ANY of the automated findings raises the SAME underlying concern as the \
human comment. Same concern means the same defect in the same code — not merely the same \
file, the same topic, or similar vocabulary.

Set same_concern=true only when acting on the automated finding would address what the \
human was asking for. Two comments about the same function that identify different problems \
are NOT the same concern. A vaguer version of the human's point IS the same concern, as long \
as it identifies the same defect.

Default to false when uncertain. Inflated agreement makes the benchmark useless."""


def judge_prompt(human: dict[str, Any], findings: list[dict[str, Any]]) -> str:
    lines = [
        "HUMAN REVIEWER COMMENT",
        f"file: {human.get('file')}  line: {human.get('line')}",
        f"comment: {human.get('comment')}",
        "",
        f"AUTOMATED FINDINGS ({len(findings)})",
    ]
    if not findings:
        lines.append("(none — the agent reported nothing)")
    for i, f in enumerate(findings, 1):
        lines.append(
            f"{i}. [{f.get('file')}:{f.get('line')}] {f.get('claim')} — {f.get('failure_scenario')}"
        )
    lines.append("")
    lines.append("Does any automated finding raise the same concern as the human comment?")
    return "\n".join(lines)
