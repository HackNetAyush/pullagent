"""Deterministic analysis (D1) — runs before the model and *subtracts* from it.

Whatever a linter already reports, the model is forbidden to comment on. This is
the cheapest precision win available: it deletes the nitpick class at zero token
cost. Phase 3 replaces this with SARIF normalisation across every tool
(BACKLOG.md CR-14); for now it covers the two cases that matter most and degrades
silently when a tool is absent.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 8_000


@dataclass
class LintResult:
    output: str = ""
    rules: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.output and not self.rules


def _run(args: list[str], cwd: str, timeout: int = 120) -> tuple[int, str]:
    try:
        p = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return p.returncode, p.stdout or p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        log.debug("lint %s failed: %s", args[0], e)
        return -1, ""


def _ruff(repo: str, files: list[str]) -> LintResult:
    py = [f for f in files if f.endswith(".py")]
    if not py or not shutil.which("ruff"):
        return LintResult()
    code, out = _run(["ruff", "check", "--output-format", "json", "--force-exclude", *py], repo)
    if code < 0 or not out.strip():
        return LintResult()
    try:
        items = json.loads(out)
    except json.JSONDecodeError:
        return LintResult()

    rules: set[str] = set()
    lines: list[str] = []
    for it in items[:200]:
        rule = it.get("code") or ""
        msg = it.get("message") or ""
        loc = it.get("filename", "")
        row = (it.get("location") or {}).get("row", "")
        if rule:
            rules.add(f"{rule} ({msg[:60]})")
        lines.append(f"{loc}:{row}: {rule} {msg}")
    return LintResult(output="\n".join(lines)[:MAX_OUTPUT_CHARS], rules=tuple(sorted(rules)))


def _semgrep(repo: str, files: list[str]) -> LintResult:
    if not files or not shutil.which("semgrep"):
        return LintResult()
    code, out = _run(
        ["semgrep", "--config", "auto", "--json", "--quiet", "--no-git-ignore", *files],
        repo,
        timeout=240,
    )
    if code < 0 or not out.strip():
        return LintResult()
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return LintResult()

    rules: set[str] = set()
    lines: list[str] = []
    for r in (data.get("results") or [])[:100]:
        rid = r.get("check_id", "")
        msg = (r.get("extra") or {}).get("message", "")[:80]
        path = r.get("path", "")
        line = (r.get("start") or {}).get("line", "")
        if rid:
            rules.add(f"{rid.split('.')[-1]} ({msg[:60]})")
        lines.append(f"{path}:{line}: {rid} {msg}")
    return LintResult(output="\n".join(lines)[:MAX_OUTPUT_CHARS], rules=tuple(sorted(rules)))


def analyse(repo: str, files: list[str]) -> LintResult:
    """Run every available tool over the changed files and merge the results."""
    outputs: list[str] = []
    rules: set[str] = set()
    for fn in (_ruff, _semgrep):
        res = fn(repo, files)
        if res.output:
            outputs.append(res.output)
        rules.update(res.rules)
    if not outputs and not rules:
        log.info("no linters available or nothing reported")
    return LintResult(
        output="\n".join(outputs)[:MAX_OUTPUT_CHARS],
        rules=tuple(sorted(rules)),
    )
