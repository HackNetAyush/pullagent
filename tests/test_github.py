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


def _vf(
    file: str = "app/handler.py",
    line: int = 13,
    claim: str = "off-by-one",
    quote: str = "",
    fix: str | None = None,
) -> VerifiedFinding:
    return VerifiedFinding(
        finding=Finding(
            claim=claim,
            failure_scenario="With an empty list the index wraps and returns the last element.",
            evidence=[
                Evidence(file=file, start_line=line, end_line=line, quote=quote, why="the bound")
            ],
            category=Category.CORRECTNESS,
            severity=Severity.HIGH,
            confidence=0.9,
            suggested_fix=fix,
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
    assert "app/handler.py" in body
    assert "off-by-one" in body
    # The line it cited is the reason it was demoted; repeating it in the summary
    # would present an unconfirmed number as a location.
    assert "999" not in body


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
    vf = _vf(quote="    added_one()")
    vf.finding.suggested_fix = "    return items[len(items) - 1]"
    assert "```suggestion" in render_comment(vf, verified=True)


def test_prose_fix_never_becomes_a_committable_suggestion() -> None:
    """A suggestion block is one click from being committed, so English in one
    puts prose into the source file."""
    vf = _vf(quote="    added_one()")
    vf.finding.suggested_fix = "check trimmed.length <= MAX_MESSAGE_LENGTH"
    rendered = render_comment(vf, verified=True)
    assert "```suggestion" not in rendered
    # The advice still reaches the reader; it just is not committable.
    assert "MAX_MESSAGE_LENGTH" in rendered


def test_unconfirmed_anchor_gets_no_suggestion_block() -> None:
    """Without a quote the line number is unverified, and a suggestion replaces
    whatever line it lands on."""
    vf = _vf(quote="")
    vf.finding.suggested_fix = "    return items[len(items) - 1]"
    assert "```suggestion" not in render_comment(vf)


def test_quote_relocates_a_miscounted_anchor() -> None:
    """The failure this exists for: the model cites real code but the wrong
    number. The quote identifies the line it was actually looking at."""
    files = parse(SAMPLE_DIFF)
    commentable = {f.path: f.commentable for f in files}
    new_text = {f.path: f.new_text for f in files}
    _, comments = build_review(
        [_vf(line=11, quote="    added_two()")],
        commentable=commentable,
        new_text=new_text,
        already=set(),
        tier="T2",
        cost=0.1,
        elapsed=1.0,
        killed=0,
    )
    assert len(comments) == 1
    assert comments[0]["line"] == 14  # where added_two() actually is, not 11


def test_invented_quote_loses_its_anchor() -> None:
    """A quote matching nothing in the diff means the citation is fabricated."""
    files = parse(SAMPLE_DIFF)
    _, comments = build_review(
        [_vf(line=13, quote="    never_written_anywhere()")],
        commentable={f.path: f.commentable for f in files},
        new_text={f.path: f.new_text for f in files},
        already=set(),
        tier="T2",
        cost=0.1,
        elapsed=1.0,
        killed=0,
    )
    assert comments == []


def test_ambiguous_quote_loses_its_anchor() -> None:
    """A quote true of many lines is evidence for none of them."""
    diff = SAMPLE_DIFF.replace("+    added_two()", "+    added_one()")
    files = parse(diff)
    _, comments = build_review(
        [_vf(line=99, quote="    added_one()")],
        commentable={f.path: f.commentable for f in files},
        new_text={f.path: f.new_text for f in files},
        already=set(),
        tier="T2",
        cost=0.1,
        elapsed=1.0,
        killed=0,
    )
    assert comments == []


def test_matching_quote_is_left_where_it_is() -> None:
    files = parse(SAMPLE_DIFF)
    _, comments = build_review(
        [_vf(line=13, quote="    added_one()")],
        commentable={f.path: f.commentable for f in files},
        new_text={f.path: f.new_text for f in files},
        already=set(),
        tier="T2",
        cost=0.1,
        elapsed=1.0,
        killed=0,
    )
    assert len(comments) == 1
    assert comments[0]["line"] == 13


def test_clean_review_still_posts_a_summary() -> None:
    body, comments = build_review(
        [], commentable={}, already=set(), tier="T1", cost=0.02, elapsed=8.0, killed=0
    )
    assert comments == []
    assert "No findings survived verification" in body


def test_span_quote_resolves_to_the_span_start() -> None:
    """Asked for one line, models often quote the whole cited span. The span is
    contiguous, so a match on its i-th line puts the start i lines above."""
    files = parse(SAMPLE_DIFF)
    _, comments = build_review(
        [_vf(line=99, quote="    added_one()\n    added_two()")],
        commentable={f.path: f.commentable for f in files},
        new_text={f.path: f.new_text for f in files},
        already=set(),
        tier="T2",
        cost=0.1,
        elapsed=1.0,
        killed=0,
    )
    assert len(comments) == 1
    assert comments[0]["line"] == 13


def test_span_quote_survives_a_truncated_first_line() -> None:
    """The first quoted line is the one most often cut off mid-expression, so a
    later line has to be able to carry the anchor."""
    files = parse(SAMPLE_DIFF)
    _, comments = build_review(
        [_vf(line=99, quote="    added_o\n    added_two()")],
        commentable={f.path: f.commentable for f in files},
        new_text={f.path: f.new_text for f in files},
        already=set(),
        tier="T2",
        cost=0.1,
        elapsed=1.0,
        killed=0,
    )
    assert len(comments) == 1
    assert comments[0]["line"] == 13  # 14 matched at offset 1
