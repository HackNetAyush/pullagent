"""The CodeRabbit baseline parser decides what the blind judge sees as the
competitor's findings, so its edge cases are worth pinning down: findings that
could not be posted inline arrive blockquoted, praise must not become a
candidate, and comments attached to a superseded commit must not leak in."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from rabbit_baseline import extract, from_review_body, strip_markup  # noqa: E402

HEAD = "a" * 40
OLD = "b" * 40


def rabbit(**kw):
    node = {"user": {"login": "coderabbitai[bot]"}}
    node.update(kw)
    return node


INLINE_BODY = """_⚠️ Potential issue_ | _🔴 Critical_

**Guard against a null session before dereferencing it.**

`resolve()` returns null when the token has expired, so this dereference throws.

<details>
<summary>♻️ Proposed fix</summary>

```diff
-  session.id
+  session?.id
```
</details>

<details>
<summary>🤖 Prompt for AI Agents</summary>

```
Do not leak this scaffolding into the claim.
```

</details>
"""

# Findings CodeRabbit could not post inline come back blockquoted inside the
# single review body, and sit alongside a praise section that is not a defect.
REVIEW_BODY = """**Actionable comments posted: 1**

> <details>
> <summary>⚠️ Outside diff range comments (1)</summary><blockquote>
>
> <details>
> <summary>src/auth/session.ts (1)</summary><blockquote>
>
> `246-274`: **Handle token-parse failures instead of persisting stale creds.**
>
> Parsing failures still write the old token set, which masks refresh errors.
>
> </blockquote></details>
>
> </blockquote></details>

<details>
<summary>🧹 Nitpick comments (1)</summary><blockquote>

<details>
<summary>src/auth/policy.ts (1)</summary><blockquote>

`58-63`: **Consider making the policy `static final`.**

These instances are immutable and can be shared.

</blockquote></details>

</blockquote></details>

<details>
<summary>🔇 Additional comments (1)</summary><blockquote>

<details>
<summary>src/auth/types.ts (1)</summary><blockquote>

`12-12`: **LGTM! Type import is correct.**

No behaviour change.

</blockquote></details>

</blockquote></details>

<details>
<summary>📜 Review details</summary>

`99-99`: **Metadata, not a finding.**

</details>
"""


def test_strip_markup_drops_scaffolding_and_keeps_prose():
    out = strip_markup(INLINE_BODY)
    assert "Guard against a null session" in out
    assert "Proposed fix" not in out
    assert "Prompt for AI Agents" not in out
    assert "session?.id" not in out


def test_blockquoted_outside_diff_findings_are_not_dropped():
    found = from_review_body(REVIEW_BODY)
    claims = {c["claim"] for c in found}
    assert any("Handle token-parse failures" in c for c in claims)
    assert any(c["path"] == "src/auth/session.ts" and c["line"] == "246-274" for c in found)


def test_praise_section_is_excluded_by_default_but_recoverable():
    default = extract([], [rabbit(body=REVIEW_BODY, state="COMMENTED")], HEAD)
    assert default["praise_excluded"] == 1
    assert not any("LGTM" in t for t in default["texts"])

    with_praise = extract(
        [], [rabbit(body=REVIEW_BODY, state="COMMENTED")], HEAD, include_praise=True
    )
    assert any("LGTM" in t for t in with_praise["texts"])


def test_metadata_sections_never_become_candidates():
    claims = {c["claim"] for c in from_review_body(REVIEW_BODY)}
    assert not any("Metadata, not a finding" in c for c in claims)


def test_extract_combines_surfaces_and_reports_actionable_count():
    comments = [
        rabbit(
            body=INLINE_BODY, path="src/auth/session.ts", original_line=41, original_commit_id=HEAD
        )
    ]
    result = extract(comments, [rabbit(body=REVIEW_BODY, state="COMMENTED")], HEAD)
    assert result["inline_count"] == 1
    assert result["body_count"] == 2  # outside-diff + nitpick, praise and metadata excluded
    assert result["actionable_posted"] == 1
    assert result["texts"][0].startswith("src/auth/session.ts:41 ")


@pytest.mark.parametrize(
    "comment",
    [
        rabbit(body=INLINE_BODY, path="a.ts", original_line=1, original_commit_id=OLD),
        rabbit(
            body=INLINE_BODY,
            path="a.ts",
            original_line=1,
            original_commit_id=HEAD,
            in_reply_to_id=7,
        ),
        {
            "user": {"login": "someone"},
            "body": INLINE_BODY,
            "path": "a.ts",
            "original_line": 1,
            "original_commit_id": HEAD,
        },
    ],
    ids=["superseded-commit", "reply-thread", "not-coderabbit"],
)
def test_inline_candidates_are_filtered(comment):
    assert extract([comment], [], HEAD)["inline_count"] == 0


def test_identical_text_across_surfaces_is_deduplicated():
    body = REVIEW_BODY + REVIEW_BODY
    result = extract([], [rabbit(body=body, state="COMMENTED")], HEAD)
    assert result["body_count"] == 2
