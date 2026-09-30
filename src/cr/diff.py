"""Diff acquisition and parsing.

Parsing is delegated to `unidiff`, which already handles renames, mode changes,
binary markers and `\\ No newline at end of file`. Do not hand-roll this.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from unidiff import PatchSet
from unidiff.errors import UnidiffParseError

# Untracked files are invisible to `git diff`, but a newly added file is exactly
# what a reviewer most needs to see. Synthesise an all-additions patch for each.
UNTRACKED_MAX_BYTES = 200_000


@dataclass
class FileDiff:
    path: str
    patch: str
    added: int = 0
    removed: int = 0
    is_new: bool = False
    is_deleted: bool = False
    # New-side line numbers GitHub will accept an inline comment on. Only lines
    # present in the diff are commentable; anything else is a 422 from the API.
    commentable: set[int] = field(default_factory=set)
    # New-side text of each commentable line, keyed by line number. An anchor is
    # only trustworthy if the quote the model cited is actually there.
    new_text: dict[int, str] = field(default_factory=dict)
    hunks: int = 0

    def __post_init__(self) -> None:
        # Derive the hunk count when a caller builds a FileDiff directly (tests,
        # synthesised untracked patches) rather than through `parse`.
        if self.hunks == 0 and self.patch.strip():
            counted = sum(1 for ln in self.patch.splitlines() if ln.startswith("@@"))
            self.hunks = counted or 1


@dataclass
class DiffSet:
    files: list[FileDiff] = field(default_factory=list)
    base: str = ""
    head: str = ""

    @property
    def total_hunks(self) -> int:
        return sum(f.hunks for f in self.files)

    @property
    def total_files(self) -> int:
        return len(self.files)

    def commentable_map(self) -> dict[str, set[int]]:
        return {f.path: f.commentable for f in self.files if f.commentable}

    def new_text_map(self) -> dict[str, dict[int, str]]:
        return {f.path: f.new_text for f in self.files if f.new_text}

    def render(self, max_chars: int | None = None) -> str:
        """Render every reviewable patch; never silently truncate later files."""
        out: list[str] = []
        used = 0
        for f in self.files:
            patch = f.patch
            if patch.startswith("@@"):
                patch = f"--- /dev/null\n+++ b/{f.path}\n{patch}"
            block = patch.rstrip("\n") + "\n"
            if max_chars is not None and used + len(block) > max_chars:
                raise ValueError("Diff exceeds requested budget; split it into review chunks")
            out.append(block)
            used += len(block)
        return "".join(out)


# Width of the line-number gutter `number_patch` writes. Five digits covers any
# file we will ever review; the blank gutter on removed lines is the same width
# so the diff markers stay in one column.
_GUTTER = 5


def number_patch(unified: str) -> str:
    """Prefix every line with the new-side number an inline comment can anchor to.

    A bare `@@ -0,0 +1,144 @@` header asks the model to track 144 lines in its
    head before it can cite one. It miscounts, and nothing downstream can tell,
    because inside a new file every wrong guess is still a commentable line.

    The numbers come from the same walk that fills `FileDiff.commentable`, so
    what the model is shown and what GitHub will accept cannot drift apart.
    Removed lines get a blank gutter: they have no new-side number and are not
    valid anchors.

    Display only. The result is deliberately NOT a parseable unified diff, so it
    must be applied after chunking, never stored back onto `PRContext.diff`.
    """
    try:
        patch_set = PatchSet(unified)
    except (UnidiffParseError, UnicodeDecodeError, ValueError):
        # Never let presentation break a review; the model sees the raw diff.
        return unified

    out: list[str] = []
    for pf in patch_set:
        out.append(f"--- {pf.source_file}")
        out.append(f"+++ {pf.target_file}")
        for hunk in pf:
            header = (
                f"@@ -{hunk.source_start},{hunk.source_length} "
                f"+{hunk.target_start},{hunk.target_length} @@"
            )
            if hunk.section_header:
                header += f" {hunk.section_header}"
            out.append(f"{' ' * _GUTTER} {header}")
            for line in hunk:
                no = line.target_line_no
                gutter = (
                    f"{no:>{_GUTTER}}" if no is not None and not line.is_removed else " " * _GUTTER
                )
                marker = "+" if line.is_added else "-" if line.is_removed else " "
                out.append(f"{gutter} {marker}{line.value.rstrip(chr(10))}")
    # A fragment with no headers parses to zero files without raising. Numbering
    # it would hand the model an empty diff, which is far worse than an unnumbered
    # one, so anything that produces nothing falls back to the original text.
    if not out:
        return unified
    return "\n".join(out)


def review_chunks(text: str, max_chars: int) -> list[str]:
    """Pack complete file patches, splitting oversized files at hunk boundaries.

    Adaptation of PR-Agent's multi-diff approach: no later file silently disappears.
    Oversized hunks are split with recalculated source/target coordinates.
    """
    if len(text) <= max_chars:
        return [text]
    if max_chars <= 0:
        raise ValueError("Review budget must be positive")
    patch_set = PatchSet(text)
    if not patch_set:
        raise ValueError("Cannot split oversized unparseable diff")
    blocks: list[str] = []
    for f in patch_set:
        patch = str(f)
        if len(patch) <= max_chars:
            blocks.append(patch)
            continue
        header = patch.split("@@ ", 1)[0]
        if not f or len(header) + 128 >= max_chars:
            raise ValueError(f"Patch header in {f.path} exceeds review budget")
        for hunk in f:
            source, target = hunk.source_start, hunk.target_start
            lines: list = []
            size = len(header) + 128  # reserve space for recalculated coordinates

            def flush(header: str = header) -> None:
                nonlocal source, target, lines, size
                if not lines:
                    return
                old = sum(line.is_context or line.is_removed for line in lines)
                new = sum(line.is_context or line.is_added for line in lines)
                # For an empty range unified diff points to the preceding line.
                a = source if old else max(0, source - 1)
                b = target if new else max(0, target - 1)
                block = header + f"@@ -{a},{old} +{b},{new} @@\n"
                blocks.append(block + "".join(str(line) for line in lines))
                source += old
                target += new
                lines, size = [], len(header) + 128

            # Zero-length source/target ranges point *before* the next line.
            source += int(hunk.source_length == 0)
            target += int(hunk.target_length == 0)
            for line in hunk:
                if size + len(str(line)) > max_chars:
                    flush()
                if size + len(str(line)) > max_chars:
                    raise ValueError(f"Single line in {f.path} exceeds review budget")
                lines.append(line)
                size += len(str(line))
            flush()
    out: list[str] = []
    current = ""
    for block in blocks:
        if current and len(current) + len(block) > max_chars:
            out.append(current)
            current = ""
        current += block
    if current:
        out.append(current)
    return out


def parse(unified: str) -> list[FileDiff]:
    """Parse a unified diff into FileDiffs, recording commentable line numbers."""
    try:
        patch_set = PatchSet(unified)
    except (UnidiffParseError, UnicodeDecodeError):
        return []

    files: list[FileDiff] = []
    for pf in patch_set:
        path = pf.path  # unidiff strips the a/ b/ prefixes and handles renames
        commentable: set[int] = set()
        new_text: dict[int, str] = {}
        for hunk in pf:
            for line in hunk:
                # Added and context lines both carry a new-side number and both
                # are valid inline-comment anchors. Removed lines are not.
                if line.target_line_no is not None and not line.is_removed:
                    commentable.add(line.target_line_no)
                    new_text[line.target_line_no] = line.value.rstrip("\n")
        files.append(
            FileDiff(
                path=path,
                patch=str(pf),
                added=pf.added,
                removed=pf.removed,
                is_new=pf.is_added_file,
                is_deleted=pf.is_removed_file,
                commentable=commentable,
                new_text=new_text,
                hunks=len(pf) or (1 if str(pf).strip() else 0),
            )
        )
    return files


def _run(args: list[str], cwd: str) -> str:
    proc = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def untracked_files(repo: str) -> list[FileDiff]:
    """Collect untracked, non-ignored files as all-addition patches."""
    out: list[FileDiff] = []
    listing = _run(["git", "ls-files", "--others", "--exclude-standard"], repo)
    for raw in listing.splitlines():
        rel = raw.strip()
        if not rel:
            continue
        path = Path(repo) / rel
        try:
            if path.stat().st_size > UNTRACKED_MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # binary, unreadable, or vanished
        lines = text.splitlines()
        if not lines:
            continue
        header = "@@ -0,0 +1," + str(len(lines)) + " @@\n"
        body = "".join("+" + ln + "\n" for ln in lines)
        out.append(
            FileDiff(
                path=rel,
                patch=header + body,
                added=len(lines),
                is_new=True,
                commentable=set(range(1, len(lines) + 1)),
                new_text=dict(enumerate(lines, start=1)),
                hunks=1,
            )
        )
    return out


def collect(
    repo: str = ".",
    base: str | None = None,
    *,
    context_lines: int = 3,
    include_untracked: bool = True,
    staged: bool = False,
) -> DiffSet:
    """Diff the working tree (or `base`..HEAD) into a DiffSet.

    With no base, reviews uncommitted changes — the fast local dev loop.
    Untracked files are included by default: `git diff` alone would miss every
    newly added file, which is precisely what a reviewer needs to see.
    """
    if base:
        args = ["git", "diff", f"--unified={context_lines}", f"{base}...HEAD"]
        head = _run(["git", "rev-parse", "HEAD"], repo).strip()
    elif staged:
        # Only the index. `git add`-ed new files already appear here, so the
        # untracked sweep would double-count them.
        args = ["git", "diff", "--cached", f"--unified={context_lines}"]
        head = "STAGED"
        include_untracked = False
    else:
        args = ["git", "diff", f"--unified={context_lines}", "HEAD"]
        head = "WORKTREE"

    files = parse(_run(args, repo))

    if include_untracked and not base:
        known = {f.path for f in files}
        files.extend(f for f in untracked_files(repo) if f.path not in known)

    return DiffSet(files=files, base=base or "HEAD", head=head)
