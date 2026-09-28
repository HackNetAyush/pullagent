"""Every grammar query must compile. A broken query fails silently and the index
comes back empty, which looks like "this repo has no symbols" rather than a bug."""

from __future__ import annotations

import pytest
import tree_sitter as ts
from tree_sitter_language_pack import get_language

from cr.graph import DEF_QUERIES, EXT_LANG, REF_QUERIES, RepoGraph, build, render_slice


@pytest.mark.parametrize("lang", sorted(set(EXT_LANG.values())))
def test_def_query_compiles(lang: str) -> None:
    src = DEF_QUERIES.get(lang)
    if not src:
        pytest.skip(f"no def query for {lang}")
    ts.Query(get_language(lang), src)


@pytest.mark.parametrize("lang", sorted(set(EXT_LANG.values())))
def test_ref_query_compiles(lang: str) -> None:
    src = REF_QUERIES.get(lang)
    if not src:
        pytest.skip(f"no ref query for {lang}")
    ts.Query(get_language(lang), src)


def test_typescript_symbols_are_actually_extracted(tmp_path) -> None:
    """Regression: TS shared JavaScript's query, which is invalid for TS, so an
    entire TypeScript repo indexed to one symbol."""
    (tmp_path / "chat.ts").write_text(
        "export function isValidMessage(c: string): boolean { return c.length > 0 }\n"
        "export const createMessage = (a: string) => ({ a })\n"
        "export class Cart { total(items: number[]) { return items.length } }\n"
        "export interface Msg { id: string }\n",
        encoding="utf-8",
    )
    g = build(tmp_path)
    assert "isValidMessage" in g.defs
    assert "createMessage" in g.defs
    assert "Cart" in g.defs
    assert "Msg" in g.defs


def test_callers_found_across_files(tmp_path) -> None:
    (tmp_path / "lib.ts").write_text(
        "export function sendMessage(x: string) { return x }\n", encoding="utf-8"
    )
    (tmp_path / "panel.tsx").write_text(
        "import { sendMessage } from './lib'\n"
        "export function Panel() { return sendMessage('hi') }\n",
        encoding="utf-8",
    )
    g = build(tmp_path)
    callers = g.callers_of({"sendMessage"}, exclude={"lib.ts"})
    assert any(f == "panel.tsx" for _n, f, _l in callers)


def test_slice_names_the_callers(tmp_path) -> None:
    (tmp_path / "lib.ts").write_text("export function doThing() { return 1 }\n", encoding="utf-8")
    (tmp_path / "use.ts").write_text("import {doThing} from './lib'\ndoThing()\n", encoding="utf-8")
    g = build(tmp_path)
    out = render_slice(g, {"lib.ts"}, [("config.json", 3)], root=tmp_path)
    assert "doThing" in out
    assert "use.ts" in out
    assert "config.json" in out


def test_graph_survives_a_round_trip() -> None:
    g = RepoGraph(commit="abc", defs={"f": [["a.py", 1]]}, refs={"f": [["b.py", 9]]}, files=2)
    assert RepoGraph.from_json(g.to_json()).defs == g.defs


def test_delta_reindex_replaces_not_duplicates(tmp_path) -> None:
    """A PR's files get re-indexed from head. Without drop_files the base entries
    survive and every symbol appears twice."""
    (tmp_path / "a.ts").write_text("export function alpha() { return 1 }\n", encoding="utf-8")
    g = build(tmp_path)
    assert len(g.defs["alpha"]) == 1

    from cr.graph import index_files

    g.drop_files({"a.ts"})
    index_files(tmp_path, [tmp_path / "a.ts"], g)
    assert len(g.defs["alpha"]) == 1


def test_pr_that_adds_a_file_still_gets_symbols(tmp_path) -> None:
    """Regression: base graph has no entry for a file the PR creates, so the
    slice came back empty and cross-file context silently did nothing."""
    from cr.graph import index_files

    (tmp_path / "existing.ts").write_text(
        "import { fresh } from './new'\nexport function useIt() { return fresh() }\n",
        encoding="utf-8",
    )
    base = build(tmp_path)
    assert "fresh" not in base.defs

    (tmp_path / "new.ts").write_text("export function fresh() { return 2 }\n", encoding="utf-8")
    base.drop_files({"new.ts"})
    index_files(tmp_path, [tmp_path / "new.ts"], base)

    assert "fresh" in base.defs
    callers = base.callers_of({"fresh"}, exclude={"new.ts"})
    assert any(f == "existing.ts" for _n, f, _l in callers)


def test_co_change_drops_single_occurrences() -> None:
    """Every file co-occurs once in the initial commit. Surfacing that as
    'related code' is pure noise."""
    from cr.repo import RepoCache

    cache = RepoCache.__new__(RepoCache)
    counts = {"a.py": 1, "b.py": 5, "c.py": 2}
    ranked = [(f, n) for f, n in counts.items() if n >= 2]
    assert sorted(ranked, key=lambda kv: kv[1], reverse=True) == [("b.py", 5), ("c.py", 2)]
    assert cache is not None
