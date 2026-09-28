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

    def render(self, max_chars: int = 120_000) -> str:
        """Additions matter more than deletions; deleted files collapse to a list.

        Same prioritisation as pr-agent's compression strategy.
        """
        out: list[str] = []
        used = 0
        deleted = [f.path for f in self.files if f.is_deleted]
        for f in self.files:
            if f.is_deleted:
                continue
            block = f"--- {f.path}\n{f.patch}\n"
            if used + len(block) > max_chars:
                out.append(f"\n[truncated: {f.path} and later files omitted for budget]\n")
                break
            out.append(block)
            used += len(block)
        if deleted:
            out.append("\nDeleted files:\n" + "\n".join(f"- {p}" for p in sorted(deleted)))
        return "".join(out)


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
        for hunk in pf:
            for line in hunk:
                # Added and context lines both carry a new-side number and both
                # are valid inline-comment anchors. Removed lines are not.
                if line.target_line_no is not None and not line.is_removed:
                    commentable.add(line.target_line_no)
        files.append(
            FileDiff(
                path=path,
                patch=str(pf),
                added=pf.added,
                removed=pf.removed,
                is_new=pf.is_added_file,
                is_deleted=pf.is_removed_file,
                commentable=commentable,
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
