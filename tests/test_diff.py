def test_number_patch_gutter_matches_commentable_lines() -> None:
    """What the model is shown and what GitHub will accept must come from the
    same walk, or anchoring silently drifts."""
    from cr.diff import number_patch, parse

    diff = "--- /dev/null\n+++ b/app/new.py\n@@ -0,0 +1,3 @@\n+alpha()\n+beta()\n+gamma()\n"
    numbered = number_patch(diff)
    gutter = {}
    for row in numbered.splitlines():
        head = row[:5].strip()
        if head.isdigit():
            gutter[int(head)] = row[6:].lstrip("+- ")

    assert gutter == {1: "alpha()", 2: "beta()", 3: "gamma()"}
    assert gutter.keys() == parse(diff)[0].commentable


def test_number_patch_leaves_removed_lines_unnumbered() -> None:
    """Removed lines have no new-side number and cannot be commented on."""
    from cr.diff import number_patch

    diff = "--- a/app/x.py\n+++ b/app/x.py\n@@ -1,1 +1,1 @@\n-gone()\n+kept()\n"
    rows = [r for r in number_patch(diff).splitlines() if r[5:].strip().startswith(("+", "-"))]
    removed = next(r for r in rows if "gone()" in r)
    added = next(r for r in rows if "kept()" in r)
    assert removed[:5].strip() == ""
    assert added[:5].strip() == "1"


def test_number_patch_falls_back_rather_than_blanking_the_diff() -> None:
    """A fragment with no headers parses to zero files without raising. Handing
    the model an empty diff is far worse than an unnumbered one."""
    from cr.diff import number_patch

    fragment = "+created = 2026-09-28T14:03:11"
    assert number_patch(fragment) == fragment
