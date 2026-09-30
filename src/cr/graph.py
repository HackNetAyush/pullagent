"""Symbol index over a checked-out repo, persisted per commit.

This is the "learning" step, and it is worth being precise about what it is: the
model learns nothing and remembers nothing between calls. What we build here is an
index, so that when a PR arrives we can select the right 20K tokens of context out
of a 500K-file repo in seconds instead of guessing from the diff alone.
"""

from __future__ import annotations

import gzip
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter as ts
from tree_sitter_language_pack import get_language, get_parser

log = logging.getLogger(__name__)

EXT_LANG: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
}

# Definition captures per language. Kept deliberately small: we need "what is
# defined here" and "what is referenced here", not a compiler.
DEF_QUERIES: dict[str, str] = {
    "python": """
        (function_definition name: (identifier) @def)
        (class_definition name: (identifier) @def)
    """,
    "javascript": """
        (function_declaration name: (identifier) @def)
        (class_declaration name: (identifier) @def)
        (variable_declarator
            name: (identifier) @def
            value: [(arrow_function) (function_expression)])
        (method_definition name: (property_identifier) @def)
    """,
    "go": """
        (function_declaration name: (identifier) @def)
        (method_declaration name: (field_identifier) @def)
        (type_spec name: (type_identifier) @def)
    """,
    "rust": """
        (function_item name: (identifier) @def)
        (struct_item name: (type_identifier) @def)
        (enum_item name: (type_identifier) @def)
    """,
    "java": """
        (method_declaration name: (identifier) @def)
        (class_declaration name: (identifier) @def)
    """,
    "ruby": """
        (method name: (identifier) @def)
        (class name: (constant) @def)
    """,
    "c": "(function_definition declarator: (function_declarator declarator: (identifier) @def))",
    "csharp": """
        (method_declaration name: (identifier) @def)
        (class_declaration name: (identifier) @def)
    """,
}
# TypeScript names classes with (type_identifier), JavaScript with (identifier).
# Sharing one query makes the whole thing invalid and silently indexes nothing.
DEF_QUERIES["typescript"] = """
    (function_declaration name: (identifier) @def)
    (class_declaration name: (type_identifier) @def)
    (variable_declarator
        name: (identifier) @def
        value: [(arrow_function) (function_expression)])
    (method_definition name: (property_identifier) @def)
    (interface_declaration name: (type_identifier) @def)
    (type_alias_declaration name: (type_identifier) @def)
"""
DEF_QUERIES["tsx"] = DEF_QUERIES["typescript"]
DEF_QUERIES["cpp"] = DEF_QUERIES["c"]
DEF_QUERIES["php"] = "(function_definition name: (name) @def)"

REF_QUERIES: dict[str, str] = {
    "python": "(call function: [(identifier) @ref (attribute attribute: (identifier) @ref)])",
    "javascript": (
        "(call_expression function: [(identifier) @ref"
        " (member_expression property: (property_identifier) @ref)])"
    ),
    "go": (
        "(call_expression function: [(identifier) @ref"
        " (selector_expression field: (field_identifier) @ref)])"
    ),
    "rust": (
        "(call_expression function: [(identifier) @ref"
        " (field_expression field: (field_identifier) @ref)])"
    ),
    "java": "(method_invocation name: (identifier) @ref)",
    "ruby": "(call method: (identifier) @ref)",
    "c": "(call_expression function: (identifier) @ref)",
    "csharp": (
        "(invocation_expression function: [(identifier) @ref"
        " (member_access_expression name: (identifier) @ref)])"
    ),
}
REF_QUERIES["typescript"] = REF_QUERIES["javascript"]
REF_QUERIES["tsx"] = REF_QUERIES["javascript"]
REF_QUERIES["cpp"] = REF_QUERIES["c"]
REF_QUERIES["php"] = "(function_call_expression function: (name) @ref)"

SKIP_DIRS = {
    ".git",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "target",
    ".venv",
    "venv",
    "__pycache__",
    ".next",
    ".nuxt",
    "coverage",
    ".mypy_cache",
    ".pytest_cache",
}
MAX_FILE_BYTES = 400_000
SCHEMA = 1


@dataclass
class RepoGraph:
    commit: str = ""
    # name -> [(file, line)]
    defs: dict[str, list[list]] = field(default_factory=dict)
    refs: dict[str, list[list]] = field(default_factory=dict)
    files: int = 0
    build_seconds: float = 0.0

    def to_json(self) -> dict:
        return {
            "schema": SCHEMA,
            "commit": self.commit,
            "defs": self.defs,
            "refs": self.refs,
            "files": self.files,
            "build_seconds": self.build_seconds,
        }

    @classmethod
    def from_json(cls, d: dict) -> RepoGraph:
        return cls(
            commit=d.get("commit", ""),
            defs=d.get("defs", {}),
            refs=d.get("refs", {}),
            files=d.get("files", 0),
            build_seconds=d.get("build_seconds", 0.0),
        )

    def drop_files(self, paths: set[str]) -> None:
        """Remove entries for these files so they can be re-indexed cleanly."""
        for table in (self.defs, self.refs):
            for name in list(table):
                kept = [loc for loc in table[name] if loc[0] not in paths]
                if kept:
                    table[name] = kept
                else:
                    del table[name]

    def definitions_in(self, paths: set[str]) -> set[str]:
        """Symbol names defined in the given files."""
        out: set[str] = set()
        for name, locs in self.defs.items():
            for f, _line in locs:
                if f in paths:
                    out.add(name)
                    break
        return out

    def callers_of(
        self, names: set[str], exclude: set[str], limit: int = 40
    ) -> list[tuple[str, str, int]]:
        """(symbol, file, line) for references outside the changed files."""
        out: list[tuple[str, str, int]] = []
        for name in sorted(names):
            for f, line in self.refs.get(name, []):
                if f in exclude:
                    continue
                out.append((name, f, line))
                if len(out) >= limit:
                    return out
        return out


def _iter_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix not in EXT_LANG:
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        found.append(p)
    return found


_QUERY_CACHE: dict[tuple[str, str], ts.Query] = {}


def _query(lang_name: str, kind: str, src: str) -> ts.Query | None:
    key = (lang_name, kind)
    if key not in _QUERY_CACHE:
        try:
            _QUERY_CACHE[key] = ts.Query(get_language(lang_name), src)
        except Exception as e:  # noqa: BLE001 - a bad grammar query must not kill indexing
            log.debug("query %s/%s unavailable: %s", lang_name, kind, e)
            return None
    return _QUERY_CACHE[key]


def index_files(root: Path, paths: list[Path], graph: RepoGraph) -> None:
    for path in paths:
        lang_name = EXT_LANG.get(path.suffix)
        if not lang_name:
            continue
        try:
            src = path.read_bytes()
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()

        try:
            tree = get_parser(lang_name).parse(src)
        except Exception:  # noqa: BLE001
            continue

        for kind, table, queries in (
            ("def", graph.defs, DEF_QUERIES),
            ("ref", graph.refs, REF_QUERIES),
        ):
            q = _query(lang_name, kind, queries.get(lang_name, ""))
            if q is None:
                continue
            try:
                caps = ts.QueryCursor(q).captures(tree.root_node)
            except Exception:  # noqa: BLE001
                continue
            for nodes in caps.values():
                for n in nodes:
                    name = n.text.decode("utf-8", "replace")
                    if not name or len(name) < 3:
                        continue
                    table.setdefault(name, []).append([rel, n.start_point[0] + 1])
        graph.files += 1


def build(root: Path, commit: str = "") -> RepoGraph:
    started = time.monotonic()
    graph = RepoGraph(commit=commit)
    files = _iter_files(root)
    log.info("indexing %d files", len(files))
    index_files(root, files, graph)
    graph.build_seconds = time.monotonic() - started
    log.info(
        "indexed %d files, %d symbols, %d refs in %.1fs",
        graph.files,
        len(graph.defs),
        len(graph.refs),
        graph.build_seconds,
    )
    return graph


def cache_path(cache_root: Path, slug: str, commit: str) -> Path:
    d = cache_root / "graphs" / slug.replace("/", "__")
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{commit[:12]}.json.gz"


def save(graph: RepoGraph, path: Path) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(graph.to_json(), fh)


def load(path: Path) -> RepoGraph | None:
    if not path.exists():
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            d = json.load(fh)
        if d.get("schema") != SCHEMA:
            return None
        return RepoGraph.from_json(d)
    except (OSError, json.JSONDecodeError):
        return None


def latest_cached(cache_root: Path, slug: str) -> Path | None:
    d = cache_root / "graphs" / slug.replace("/", "__")
    if not d.is_dir():
        return None
    entries = sorted(d.glob("*.json.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    return entries[0] if entries else None


def render_slice(
    graph: RepoGraph,
    changed: set[str],
    co_changed: list[tuple[str, int]],
    *,
    root: Path | None = None,
    max_chars: int = 20_000,
) -> str:
    """The context block: who calls what changed, plus historical coupling.

    This is the whole point of the index — it turns "here is a diff" into "here is
    a diff and the three callers it will break".
    """
    changed_syms = graph.definitions_in(changed)
    callers = sorted(graph.callers_of(changed_syms, exclude=changed))

    parts: list[str] = []

    if changed_syms:
        parts.append(
            "Symbols defined in the changed files: " + ", ".join(sorted(changed_syms)[:40])
        )

    if callers:
        parts.append("\nCallers elsewhere in the repo (verify these still work):")
        by_sym: dict[str, list[str]] = {}
        for name, f, line in callers:
            by_sym.setdefault(name, []).append(f"{f}:{line}")
        for name, locs in list(by_sym.items())[:20]:
            parts.append(f"  {name} <- {', '.join(locs[:6])}")

        if root is not None:
            parts.append("\nCaller code (line-numbered at the reviewed commit):")
            shown = 0
            for _name, f, line in callers:
                if shown >= 12:
                    break
                snippet = _snippet(root, f, line, radius=16)
                if snippet:
                    parts.append(snippet)
                    shown += 1

    if root is not None:
        # Callers alone cannot establish what a changed call returns. Include callees too.
        referenced = sorted(
            name for name, refs in graph.refs.items() if any(f in changed for f, _line in refs)
        )
        locations = sorted(
            {
                (f, line)
                for name in referenced
                for f, line in graph.defs.get(name, [])
                if f not in changed
            }
        )
        if locations:
            parts.append("\nDefinitions used by the changed code:")
            for f, line in locations[:12]:
                parts.append(_snippet(root, f, line, radius=20))

    if co_changed:
        parts.append(
            "\nFiles that historically change together (up to 2,000 reviewed-commit ancestors):\n  "
            + ", ".join(f"{f} ({n}x)" for f, n in co_changed[:10])
        )

    out = "\n".join(parts)
    return out[:max_chars]


def _line_at(path: Path, line: int) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for i, text in enumerate(fh, 1):
                if i == line:
                    return text.strip()[:160]
    except OSError:
        return ""
    return ""


def _snippet(root: Path, file: str, line: int, radius: int) -> str:
    path = (root / file).resolve()
    if not path.is_relative_to(root.resolve()):
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start, end = max(1, line - radius), min(len(lines), line + radius)
        return f"\n### {file}\n" + "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))
    except OSError:
        return ""
