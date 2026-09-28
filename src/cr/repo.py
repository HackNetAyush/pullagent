"""Warm repo cache: bare mirror per repo, throwaway worktree per review.

Cloning a large repo per review is 60-120s. Fetching a delta into a warm mirror
is ~2s. The mirror is shared and read-only; each review gets its own worktree.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"(https://)[^@/]+@")


def _safe(cmd: list[str]) -> str:
    """Command text with any embedded credential removed, for logs and errors."""
    return _TOKEN_RE.sub(r"\1***@", " ".join(cmd))


def default_cache_dir() -> Path:
    env = os.environ.get("CR_CACHE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".cache" / "cr"


def _run(args: list[str], cwd: Path | None = None, timeout: int = 900) -> str:
    p = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if p.returncode != 0:
        raise RuntimeError(f"{_safe(args)} failed: {(p.stderr or '').strip()[:400]}")
    return p.stdout


@dataclass
class RepoCache:
    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        (self.root / "mirrors").mkdir(parents=True, exist_ok=True)

    def mirror_path(self, slug: str) -> Path:
        return self.root / "mirrors" / (slug.replace("/", "__") + ".git")

    def is_warm(self, slug: str) -> bool:
        return (self.mirror_path(slug) / "HEAD").exists()

    def _auth_url(self, slug: str, token: str | None) -> str:
        if token:
            return f"https://x-access-token:{token}@github.com/{slug}.git"
        return f"https://github.com/{slug}.git"

    def ensure(self, slug: str, token: str | None = None, *, fetch: bool = True) -> Path:
        """Clone the mirror if absent, otherwise fetch. Returns the mirror path."""
        path = self.mirror_path(slug)
        url = self._auth_url(slug, token)

        if not self.is_warm(slug):
            log.info("cold: cloning %s (one time)", slug)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(tempfile.mkdtemp(dir=str(path.parent)))
            try:
                _run(["git", "clone", "--mirror", "--filter=blob:none", url, str(tmp / "m.git")])
                (tmp / "m.git").rename(path)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        elif fetch:
            log.info("warm: fetching %s", slug)
            # The remote may have been recorded with a stale/absent token.
            _run(["git", "remote", "set-url", "origin", url], cwd=path)
            _run(["git", "fetch", "--prune", "origin", "+refs/heads/*:refs/heads/*"], cwd=path)
        return path

    def fetch_pr(self, slug: str, number: int, token: str | None = None) -> None:
        """PR head commits are not on any branch until merged; fetch them explicitly."""
        path = self.ensure(slug, token)
        _run(
            ["git", "fetch", "origin", f"+refs/pull/{number}/head:refs/cr/pr/{number}"],
            cwd=path,
        )

    def has_commit(self, slug: str, sha: str) -> bool:
        try:
            _run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=self.mirror_path(slug))
            return True
        except RuntimeError:
            return False

    @contextlib.contextmanager
    def worktree(self, slug: str, sha: str) -> Iterator[Path]:
        """Check out `sha` into a throwaway worktree. Always cleaned up."""
        mirror = self.mirror_path(slug)
        dest = Path(tempfile.mkdtemp(prefix="cr-wt-"))
        try:
            _run(["git", "worktree", "add", "--detach", "--force", str(dest), sha], cwd=mirror)
            yield dest
        finally:
            with contextlib.suppress(RuntimeError, OSError):
                _run(["git", "worktree", "remove", "--force", str(dest)], cwd=mirror)
            shutil.rmtree(dest, ignore_errors=True)

    def merge_base(self, slug: str, a: str, b: str) -> str | None:
        try:
            return _run(["git", "merge-base", a, b], cwd=self.mirror_path(slug)).strip() or None
        except RuntimeError:
            return None

    def co_change(
        self,
        slug: str,
        paths: set[str],
        *,
        months: int = 6,
        top: int = 12,
        min_count: int = 2,
    ) -> list[tuple[str, int]]:
        """Files that historically change in the same commit as `paths`.

        Catches coupling no AST can see — the config that must move with a schema.
        """
        if not paths:
            return []
        try:
            out = _run(
                ["git", "log", f"--since={months}.months", "--name-only", "--pretty=format:%H"],
                cwd=self.mirror_path(slug),
                timeout=180,
            )
        except RuntimeError:
            return []

        counts: dict[str, int] = {}
        current: list[str] = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            if len(line) == 40 and all(c in "0123456789abcdef" for c in line):
                if paths & set(current):
                    for f in current:
                        if f not in paths:
                            counts[f] = counts.get(f, 0) + 1
                current = []
            else:
                current.append(line)
        if paths & set(current):
            for f in current:
                if f not in paths:
                    counts[f] = counts.get(f, 0) + 1

        # A single co-occurrence is usually the initial commit, where every file
        # landed together. That is noise, and noise in context costs precision.
        ranked = [(f, n) for f, n in counts.items() if n >= min_count]
        return sorted(ranked, key=lambda kv: kv[1], reverse=True)[:top]
