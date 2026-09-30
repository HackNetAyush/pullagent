"""Cached prompt-prefix construction (PIPELINE.md §2.1).

This is where 82% of the input cost is won or lost. Two rules govern everything:

1. Caching is a **prefix match from byte 0**. Render order is tools -> system ->
   messages. Anything volatile placed early invalidates everything after it.
2. The role instruction lives in the LAST content block, never in `system`. That
   is what lets finder and verifier passes share the same two cache entries.

Layout:

    system[0]   agent preamble                     -.
    system[1]   repo guidelines + conventions       |  L3a, 1h TTL
                <<< cache breakpoint 1 >>>         -'

    user[0]     PR payload: diff, graph, lint      -.  L3b, 5m TTL
                <<< cache breakpoint 2 >>>         -'
    user[1]     role instruction                       volatile, ~200 tok
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from cr.diff import number_patch

# Patterns that silently destroy a cache entry if they appear in a cached block.
# Every one of these has burned somebody. See PIPELINE.md §2.2 "Trap 2".
_INVALIDATORS: list[tuple[str, re.Pattern[str]]] = [
    ("ISO timestamp", re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")),
    ("UUID", re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)),
    ("epoch seconds", re.compile(r"\b1[6-9]\d{8}\b")),
    ("run/request id", re.compile(r"\b(?:run|request|delivery|trace)[_-]?id\b\s*[:=]", re.I)),
]


class CacheInvalidatorError(ValueError):
    """Raised when volatile content is found inside a block we intend to cache."""


def assert_cacheable(text: str, where: str) -> None:
    """Fail loud at build time rather than quietly at billing time."""
    for name, pattern in _INVALIDATORS:
        m = pattern.search(text)
        if m:
            raise CacheInvalidatorError(
                f"{name} found in cached block {where!r}: {m.group(0)!r}. "
                "Volatile content must go after the last cache breakpoint."
            )


def stable_json(obj: Any) -> str:
    """Deterministic serialisation. An unsorted dict is a silent invalidator."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class RepoContext:
    """L3a payload — stable for the lifetime of a repo's configuration."""

    slug: str
    guidelines: str = ""
    conventions: str = ""
    languages: tuple[str, ...] = ()

    def render(self) -> str:
        parts = [f"# Repository: {self.slug}"]
        if self.languages:
            parts.append(f"Primary languages: {', '.join(sorted(self.languages))}")
        if self.conventions:
            parts.append(f"\n## Conventions\n{self.conventions}")
        if self.guidelines:
            parts.append(f"\n## Review guidelines\n{self.guidelines}")
        return "\n".join(parts)


@dataclass(frozen=True)
class PRContext:
    """L3b payload — stable for the lifetime of one head SHA."""

    title: str
    description: str
    diff: str
    graph_slice: str = ""
    lint_output: str = ""
    suppressed_rules: tuple[str, ...] = ()

    def render(self) -> str:
        parts = [f"# Pull request: {self.title}"]
        if self.description:
            parts.append(f"\n{self.description}")
        if self.suppressed_rules:
            # D1: the deterministic layer subtracts. Tell the model what not to say.
            parts.append(
                "\n## Already covered by linters — DO NOT comment on these\n"
                + "\n".join(f"- {r}" for r in sorted(self.suppressed_rules))
            )
        if self.lint_output:
            parts.append(f"\n## Static analysis output\n```\n{self.lint_output}\n```")
        if self.graph_slice:
            parts.append(
                f"\n## Related code (callers, implementors, co-changed)\n{self.graph_slice}"
            )
        # Numbered only here, at the last step before the model sees it. `self.diff`
        # stays a parseable unified diff because `review_chunks` and `parse` both
        # re-read it downstream; the gutter would break them.
        parts.append(
            "\n## Diff\n"
            "The number before each line is its line number in the new file. Cite those "
            "numbers; never count lines yourself.\n"
            f"```diff\n{number_patch(self.diff)}\n```"
        )
        return "\n".join(parts)


@dataclass
class PrefixBuilder:
    """Builds request kwargs sharing two cache breakpoints across every pass."""

    preamble: str
    repo: RepoContext
    pr: PRContext | None = None
    repo_ttl: str = "1h"  # L3a: reused across every PR in the repo for an hour
    pr_ttl: str = "5m"  # L3b: the fan-out completes in ~2 minutes
    _validated: bool = field(default=False, init=False)

    def _validate_once(self) -> None:
        if self._validated:
            return
        assert_cacheable(self.preamble, "system.preamble")
        assert_cacheable(self.repo.render(), "system.repo")
        # NOTE: the PR block is deliberately NOT validated for timestamps — a diff
        # legitimately contains them. It is cached per head SHA, so it is stable
        # for its own TTL even though it is not stable across PRs.
        self._validated = True

    def system(self) -> list[dict[str, Any]]:
        """Breakpoint 1 sits on the last system block, covering tools + system."""
        self._validate_once()
        return [
            {"type": "text", "text": self.preamble},
            {
                "type": "text",
                "text": self.repo.render(),
                "cache_control": {"type": "ephemeral", "ttl": self.repo_ttl},
            },
        ]

    def messages(self, role_instruction: str) -> list[dict[str, Any]]:
        """Breakpoint 2 sits on the PR block. The role instruction follows it and
        is the only thing that differs between a finder pass and a verifier pass."""
        if self.pr is None:
            raise ValueError("PrefixBuilder.pr must be set before building messages")
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": self.pr.render(),
                        "cache_control": {"type": "ephemeral", "ttl": self.pr_ttl},
                    },
                    {"type": "text", "text": role_instruction},
                ],
            }
        ]

    def build(self, role_instruction: str) -> dict[str, Any]:
        return {"system": self.system(), "messages": self.messages(role_instruction)}

    def warm_payload(self) -> dict[str, Any]:
        """A `max_tokens: 0` pre-warm request that writes L3a without generating output.

        Returns immediately, bills zero output tokens, and leaves the repo-level
        prefix readable at 0.1x for every PR in the next hour. Singleflight this
        per repo (PIPELINE.md §4.2) or three concurrent PRs each write it.

        `output_config.format` is rejected with max_tokens: 0 — so no schema here.
        """
        return {
            "max_tokens": 0,
            "system": self.system(),
            "messages": [{"role": "user", "content": "warmup"}],
        }
