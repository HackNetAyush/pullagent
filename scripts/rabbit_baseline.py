"""Deterministic reconstruction of CodeRabbit's published candidates for a fork PR.

CodeRabbit splits one review across two surfaces: inline review comments
("Actionable comments posted: N") and collapsed sections inside the single
review body (nitpicks, outside-diff-range, duplicates, refactor suggestions).
Both surfaces are published findings, so both enter the candidate universe.

This is a parser over CodeRabbit's own published text. It performs no model
calls and invents nothing: every candidate is a markdown-stripped excerpt of a
comment CodeRabbit actually posted at the pinned head commit.
"""

from __future__ import annotations

import re

RABBIT = "coderabbit"
# Collapsed body sections that hold real findings. "Review details"/"Commits"
# are metadata. "Additional comments" is CodeRabbit's LGTM/praise channel: it
# is published text but not a defect claim, so it is extracted with its section
# recorded and excluded from the candidate universe by default. Counting praise
# as candidates would inflate CodeRabbit's candidate count and feed the blind
# judge dozens of non-claims.
FINDING_SECTIONS = (
    "Nitpick comments",
    "Outside diff range comments",
    "Duplicate comments",
    "Refactor suggestion",
    "Potential issue",
    "Additional comments",
)
PRAISE_SECTIONS = ("Additional comments",)
# Collapsed blocks that carry no findings but do end the preceding section, so an
# entry underneath them must not inherit it. Real bodies keep these counter-less.
METADATA_SECTIONS = ("Review details", "Run configuration", "Commits", "Review info")
# Sections CodeRabbit could not post inline are wrapped in a blockquote, so every
# line carries a "> " prefix. Tolerate it or those findings are silently dropped.
ENTRY = re.compile(r"^(?:>\s?)*`(?P<lines>\d+(?:-\d+)?)`:\s*\*\*(?P<title>.+?)\*\*\s*$", re.M)
SUMMARY = re.compile(r"<summary>(?P<text>[^<>]*)</summary>")
FILE_HEADER = re.compile(r"^(?P<path>\S*[^<>\s]+?\.[A-Za-z0-9_]+)\s*\((?P<n>\d+)\)$")
COUNTED = re.compile(r"\(\d+\)\s*$")


def is_rabbit(node: dict) -> bool:
    return RABBIT in (node.get("user", {}).get("login", "") or "").lower()


def strip_markup(text: str) -> str:
    """Reduce a CodeRabbit comment to its prose claim."""
    text = re.sub(r"^(?:>\s?)+", "", text, flags=re.M)
    # Drop every <details> helper block (proposed fix, AI-agent prompt, autofix).
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r"<details>(?:(?!<details>).)*?</details>", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = text.replace("**", "").replace("`", "")
    text = re.sub(r"^\s*[_*]+|[_*]+\s*$", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" _*|-")


def _tag_and_body(body: str) -> tuple[str, str]:
    """Split the leading `_⚠️ Potential issue_ | _🟠 Major_` tag line off a comment."""
    lines = body.lstrip().splitlines()
    tag = ""
    if lines and "|" in lines[0] and "_" in lines[0]:
        tag = strip_markup(lines[0])
        lines = lines[1:]
    return tag, "\n".join(lines)


def candidate_text(path: str, line: object, claim: str) -> str:
    loc = f"{path}:{line}" if line not in (None, "") else path
    return f"{loc} {claim}".strip()


def from_inline(comments: list[dict], head: str) -> list[dict]:
    """One candidate per top-level CodeRabbit inline comment at `head`."""
    out = []
    for c in comments:
        if not is_rabbit(c) or c.get("in_reply_to_id"):
            continue
        if c.get("original_commit_id") and c["original_commit_id"] != head:
            continue
        tag, rest = _tag_and_body(c.get("body") or "")
        claim = strip_markup(rest)
        if not claim:
            continue
        line = c.get("original_line") or c.get("line") or c.get("original_start_line")
        out.append(
            dict(
                surface="inline",
                severity=tag,
                path=c.get("path", ""),
                line=line,
                claim=claim,
                text=candidate_text(c.get("path", ""), line, claim),
            )
        )
    return out


def from_review_body(body: str) -> list[dict]:
    """Candidates from the collapsed finding sections of the single review body."""
    out = []
    sections: list[tuple[int, str]] = []
    files: list[tuple[int, str]] = []
    for m in SUMMARY.finditer(body):
        text = m.group("text").strip()
        header = FILE_HEADER.match(text)
        if header:
            files.append((m.start(), header.group("path")))
            continue
        # Only counted headers and the known metadata blocks close a section.
        # Helper blocks ("Proposed fix", "Prompt for AI Agents") carry no count
        # and must not be mistaken for one, or they would orphan every entry
        # after the first in a multi-finding section.
        if COUNTED.search(text) or any(k in text for k in METADATA_SECTIONS):
            kind = next((k for k in FINDING_SECTIONS if k in text), "")
            sections.append((m.start(), kind))
    for m in ENTRY.finditer(body):
        start = m.start()
        # Nearest preceding section of ANY kind: a metadata section ends the
        # finding section above it, so entries below it are not findings.
        kind = next((k for pos, k in reversed(sections) if pos < start), "")
        if not kind:
            continue  # entry sits outside any finding section; not a published finding
        path = next((p for pos, p in reversed(files) if pos < start), "")
        nxt = ENTRY.search(body, m.end())
        chunk = body[m.end() : nxt.start() if nxt else len(body)]
        detail = strip_markup(chunk.split("\n---\n")[0])
        claim = strip_markup(m.group("title"))
        if detail:
            claim = f"{claim} {detail}"
        line = m.group("lines")
        out.append(
            dict(
                surface="review_body",
                severity=kind,
                path=path,
                line=line,
                claim=claim,
                text=candidate_text(path, line, claim),
            )
        )
    return out


def extract(
    comments: list[dict], reviews: list[dict], head: str, include_praise: bool = False
) -> dict:
    """Full published candidate set for one CodeRabbit-reviewed fork PR."""
    bodies = [r.get("body") or "" for r in reviews if is_rabbit(r) and (r.get("body") or "")]
    items = from_inline(comments, head)
    for b in bodies:
        items.extend(from_review_body(b))
    praise = [it for it in items if it["severity"] in PRAISE_SECTIONS]
    if not include_praise:
        items = [it for it in items if it["severity"] not in PRAISE_SECTIONS]
    seen: dict[str, dict] = {}
    for it in items:
        seen.setdefault(it["text"], it)
    unique = list(seen.values())
    posted = None
    for b in bodies:
        m = re.search(r"Actionable comments posted:\s*(\d+)", b)
        if m:
            posted = int(m.group(1))
    return dict(
        candidates=unique,
        texts=[it["text"] for it in unique],
        inline_count=sum(1 for it in unique if it["surface"] == "inline"),
        body_count=sum(1 for it in unique if it["surface"] == "review_body"),
        actionable_posted=posted,
        review_bodies=len(bodies),
        praise_excluded=len(praise),
    )
