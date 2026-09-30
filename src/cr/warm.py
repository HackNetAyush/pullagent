"""Clone-and-index orchestration.

The one-time cost per repo, and the fast path on every PR after it.

    cr index acme/api       # cold: clone + index. Minutes on a monorepo.
    cr review-pr ...        # warm: fetch delta + reuse index. Seconds.

Nothing here is model memory. The index is ours, on disk, and its whole job is to
let us select the right 20K tokens of context instead of sending the diff alone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from cr import graph as g
from cr.repo import RepoCache, default_cache_dir

log = logging.getLogger(__name__)


@dataclass
class WarmResult:
    slug: str
    commit: str
    files: int
    symbols: int
    refs: int
    seconds: float
    was_cold: bool
    reused: bool = False


def index_repo(
    slug: str,
    token: str | None = None,
    *,
    cache_root: Path | None = None,
    ref: str = "HEAD",
    force: bool = False,
) -> WarmResult:
    """Clone if needed, then build and persist the symbol index for `ref`."""
    cache = RepoCache(cache_root or default_cache_dir())
    was_cold = not cache.is_warm(slug)

    mirror = cache.ensure(slug, token)
    from cr.repo import _run  # noqa: PLC0415 - internal helper, same module family

    commit = _run(["git", "rev-parse", ref], cwd=mirror).strip()

    path = g.cache_path(cache.root, slug, commit)
    if path.exists() and not force:
        cached = g.load(path)
        if cached:
            log.info("index already built for %s@%s", slug, commit[:8])
            return WarmResult(
                slug=slug,
                commit=commit,
                files=cached.files,
                symbols=len(cached.defs),
                refs=len(cached.refs),
                seconds=cached.build_seconds,
                was_cold=was_cold,
                reused=True,
            )

    with cache.worktree(slug, commit) as tree:
        built = g.build(tree, commit=commit)
    g.save(built, path)

    return WarmResult(
        slug=slug,
        commit=commit,
        files=built.files,
        symbols=len(built.defs),
        refs=len(built.refs),
        seconds=built.build_seconds,
        was_cold=was_cold,
    )


def context_for_pr(
    slug: str,
    number: int,
    head_sha: str,
    base_sha: str,
    changed: set[str],
    token: str | None = None,
    *,
    cache_root: Path | None = None,
    build_if_missing: bool = True,
) -> str:
    """The graph slice for one PR: callers of changed symbols, plus co-change.

    Degrades to an empty string on any failure — a review without cross-file
    context is still a review, and a crashed review is not.
    """
    try:
        cache = RepoCache(cache_root or default_cache_dir())
        cache.ensure(slug, token, fetch=False)

        # PR head commits live under refs/pull/N/head until the PR merges.
        if not cache.has_commit(slug, head_sha):
            cache.fetch_pr(slug, number, token)

        index_sha = base_sha if cache.has_commit(slug, base_sha) else head_sha
        path = g.cache_path(cache.root, slug, index_sha)
        graph = g.load(g.cache_path(cache.root, slug, head_sha)) or g.load(path)

        if graph is None:
            latest = g.latest_cached(cache.root, slug)
            if latest:
                graph = g.load(latest)
                if graph:
                    log.info("using nearest cached index (%s)", graph.commit[:8])
            if graph is None:
                if not build_if_missing:
                    return ""
                log.info("no index for %s; building now (one time)", slug)
                with cache.worktree(slug, index_sha) as tree:
                    graph = g.build(tree, commit=index_sha)
                g.save(graph, path)

        # Historical evaluation must never select context using future commits.
        co = cache.co_change(slug, changed, ref=head_sha)

        with cache.worktree(slug, head_sha) as tree:
            # Base-graph + delta (PIPELINE.md 3.2). The PR's files may not exist
            # at the base commit at all, so re-index them from head and merge.
            # Without this, a PR that adds files has no symbols and the slice is
            # empty — which is the whole feature silently doing nothing.
            from cr.repo import _run

            # A reused index may be much older (or newer) than this PR's base.
            # Repair *every* changed file between the index and head, not just PR files.
            delta = set(changed)
            if graph.commit and graph.commit != head_sha:
                delta.update(
                    _run(
                        ["git", "diff", "--name-only", graph.commit, head_sha],
                        cwd=cache.mirror_path(slug),
                    ).splitlines()
                )
            graph.drop_files(delta)
            g.index_files(tree, [tree / c for c in sorted(delta) if (tree / c).is_file()], graph)
            graph.commit = head_sha
            g.save(graph, g.cache_path(cache.root, slug, head_sha))
            return g.render_slice(graph, changed, co, root=tree)
    except Exception as e:  # noqa: BLE001 - context is an enhancement, never a hard dependency
        log.warning("graph context unavailable: %s", e)
        return ""
