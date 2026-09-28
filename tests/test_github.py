"""Tests for PR posting. A bug here spams someone's pull request."""

from __future__ import annotations

from cr.diff import parse
from cr.github import MARKER, PRRef, build_review, render_comment
from cr.models import Category, Evidence, Finding, Severity, Verdict, VerifiedFinding

SAMPLE_DIFF = """\
diff --git a/app/handler.py b/app/handler.py
index 1111111..2222222 100644
--- a/app/handler.py
+++ b/app/handler.py
@@ -10,5 +10,7 @@ def existing():
     context_one()
     context_two()
     context_three()
+    added_one()
+    added_two()
     context_four()
     context_five()
"""


def _vf(file: str = "app/handler.py", line: int = 13, claim: str = "off-by-one") -> VerifiedFinding:
    return VerifiedFinding(
        finding=Finding(
            claim=claim,
            failure_scenario="With an empty list the index wraps and returns the last element.",
            evidence=[Evidence(file=file, start_line=line, end_line=line, why="the bound")],
            category=Category.CORRECTNESS,
            severity=Severity.HIGH,
            confidence=0.9,
        ),
        verdicts=[Verdict(refuted=False, reasoning="confirmed")],
    )


def test_pr_ref_slug() -> None:
    assert PRRef("acme", "api", 7).slug == "acme/api"


def test_commentable_lines_come_from_the_diff() -> None:
    files = parse(SAMPLE_DIFF)
    assert len(files) == 1
    # Added lines plus context lines are commentable; nothing outside the hunk is.
    assert 13 in files[0].commentable
    assert 14 in files[0].commentable
    assert 999 not in files[0].commentable


def test_finding_on_a_diff_line_becomes_an_inline_comment() -> None:
    commentable = {f.path: f.commentable for f in parse(SAMPLE_DIFF)}
    body, comments = build_review(
        [_vf(line=13)],
        commentable=commentable,
        already=set(),
        tier="T2",
        cost=0.21,
        elapsed=42.0,
        killed=1,
    )
    assert len(comments) == 1
    assert comments[0]["path"] == "app/handler.py"
    assert comments[0]["line"] == 13
    assert comments[0]["side"] == "RIGHT"
    assert "1 finding(s) killed" in body


def test_finding_off_the_diff_is_demoted_not_dropped() -> None:
    """Losing a real finding to an anchoring technicality is the worst failure
    mode available, so it must land in the summary instead."""
    commentable = {f.path: f.commentable for f in parse(SAMPLE_DIFF)}
    body, comments = build_review(
        [_vf(line=999)],
        commentable=commentable,
        already=set(),
        tier="T2",
        cost=0.1,
        elapsed=1.0,
        killed=0,
    )
    assert comments == []
    assert "app/handler.py:999" in body
    assert "off-by-one" in body


def test_already_posted_findings_are_skipped() -> None:
    """Re-running on a new push must not repost the same comment."""
    commentable = {f.path: f.commentable for f in parse(SAMPLE_DIFF)}
    vf = _vf(line=13)
    fp = vf.finding.fingerprint()

    _, first = build_review(
        [vf], commentable=commentable, already=set(), tier="T2", cost=0.1, elapsed=1.0, killed=0
    )
    assert len(first) == 1

    body, second = build_review(
        [vf], commentable=commentable, already={fp}, tier="T2", cost=0.1, elapsed=1.0, killed=0
    )
    assert second == []
    assert "1 already posted" in body


def test_comment_carries_a_recoverable_fingerprint_marker() -> None:
    """The marker is the entire dedup mechanism — no database involved."""
    vf = _vf()
    rendered = render_comment(vf)
    found = MARKER.findall(rendered)
    assert found == [vf.finding.fingerprint()]


def test_suggested_fix_renders_as_a_github_suggestion_block() -> None:
    vf = _vf()
    vf.finding.suggested_fix = "    return items[len(items) - 1]"
    assert "```suggestion" in render_comment(vf)


def test_clean_review_still_posts_a_summary() -> None:
    body, comments = build_review(
        [], commentable={}, already=set(), tier="T1", cost=0.02, elapsed=8.0, killed=0
    )
    assert comments == []
    assert "No findings survived verification" in body
