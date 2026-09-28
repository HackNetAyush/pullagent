"""Local git diff acquisition and light parsing.

Phase 0 keeps this minimal. Phase 3 replaces the hunk handling with the vendored
`vendor/pr_agent/git_patch_processing.py`, which already handles the edge cases
that will otherwise bite you: `\\ No newline at end of file`, CRLF, and the
Unicode line separators (\\x1c \\x1d \\x1e \\x85 U+2028 U+2029).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field

HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class FileDiff:
    path: str
    patch: str
    added: int = 0
    removed: int = 0
    is_new: bool = False
    is_deleted: bool = False

    @property
    def hunks(self) -> int:
        n = sum(1 for line in self.patch.splitlines() if HUNK_HEADER.match(line))
        return n or (1 if self.patch.strip() else 0)


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

    def render(self, max_chars: int = 120_000) -> str:
        out: list[str] = []
        used = 0
        # Additions matter more than deletions; deleted files collapse to a list.
        # (Same prioritisation as pr-agent's compression strategy.)
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


def _run(args: list[str], cwd: str) -> str:
    proc = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def collect(repo: str = ".", base: str | None = None, *, context_lines: int = 3) -> DiffSet:
    """Diff the working tree (or `base`..HEAD) into a DiffSet.

    With no base, reviews uncommitted changes — the fast local dev loop.
    """
    if base:
        args = ["git", "diff", f"--unified={context_lines}", f"{base}...HEAD"]
        head = _run(["git", "rev-parse", "HEAD"], repo).strip()
    else:
        args = ["git", "diff", f"--unified={context_lines}", "HEAD"]
        head = "WORKTREE"

    raw = _run(args, repo)
    files: list[FileDiff] = []
    current: FileDiff | None = None
    buf: list[str] = []

    for line in raw.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current:
                current.patch = "".join(buf)
                files.append(current)
            m = re.match(r"diff --git a/(.+?) b/(.+)", line)
            path = m.group(2) if m else "unknown"
            current = FileDiff(path=path, patch="")
            buf = []
        elif current is not None:
            if line.startswith("new file mode"):
                current.is_new = True
            elif line.startswith("deleted file mode"):
                current.is_deleted = True
            elif line.startswith("+") and not line.startswith("+++"):
                current.added += 1
            elif line.startswith("-") and not line.startswith("---"):
                current.removed += 1
            buf.append(line)

    if current:
        current.patch = "".join(buf)
        files.append(current)

    return DiffSet(files=files, base=base or "HEAD", head=head)
