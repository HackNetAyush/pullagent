"""Store access. Sync SQLAlchemy — the review path makes two calls, not two hundred."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from cr.models import ReviewResult, VerifiedFinding
from cr.store.models import SCHEMA_VERSION, Base, FindingRow, Meta, Run, Suppression

log = logging.getLogger(__name__)

_ENGINE = None
_SESSION: sessionmaker[Session] | None = None


def default_url() -> str:
    from cr.repo import default_cache_dir

    root = default_cache_dir()
    root.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{(root / 'cr.db').as_posix()}"


def init(url: str | None = None) -> sessionmaker[Session]:
    global _ENGINE, _SESSION
    if _SESSION is not None:
        return _SESSION

    url = url or default_url()
    if url.startswith("sqlite:///") and (p := url[len("sqlite:///") :]):
        Path(p).parent.mkdir(parents=True, exist_ok=True)

    _ENGINE = create_engine(url, future=True)
    Base.metadata.create_all(_ENGINE)
    _add_missing_columns(_ENGINE)
    _SESSION = sessionmaker(bind=_ENGINE, future=True)

    with _SESSION() as s:
        row = s.get(Meta, "schema_version")
        if row is None:
            s.add(Meta(key="schema_version", value=str(SCHEMA_VERSION)))
            s.commit()
        elif row.value != str(SCHEMA_VERSION):
            # _add_missing_columns has already run, so the database now matches.
            # Stamp it, or every future start-up warns about a resolved gap.
            log.info("store migrated v%s -> v%s", row.value, SCHEMA_VERSION)
            row.value = str(SCHEMA_VERSION)
            s.commit()
    return _SESSION


def _add_missing_columns(engine) -> None:
    """create_all never alters an existing table, so a new column would break
    every query against a database created by an older version. Add them."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if "runs" not in insp.get_table_names():
        return
    have = {c["name"] for c in insp.get_columns("runs")}
    wanted = {
        "source": "VARCHAR(16) DEFAULT 'pr'",
        "actor": "VARCHAR(128) DEFAULT ''",
    }
    with engine.begin() as conn:
        for name, ddl in wanted.items():
            if name not in have:
                conn.execute(text(f"ALTER TABLE runs ADD COLUMN {name} {ddl}"))
                log.info("store: added runs.%s", name)


def reset_for_tests() -> None:
    global _ENGINE, _SESSION
    _ENGINE, _SESSION = None, None


@contextmanager
def session(url: str | None = None) -> Iterator[Session]:
    factory = init(url)
    s = factory()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


# --- suppression memory ------------------------------------------------------


def suppressed_fingerprints(repo: str, url: str | None = None) -> set[str]:
    try:
        with session(url) as s:
            rows = s.execute(
                select(Suppression.fingerprint).where(Suppression.repo == repo)
            ).scalars()
            return set(rows)
    except Exception as e:  # noqa: BLE001 - memory is an enhancement, not a dependency
        log.warning("suppression lookup failed: %s", e)
        return set()


def suppress(
    repo: str,
    fingerprint: str,
    *,
    reason: str,
    claim: str = "",
    file: str = "",
    note: str = "",
    pr_number: int | None = None,
    url: str | None = None,
) -> bool:
    """Record a human rejection. Returns True if newly added."""
    with session(url) as s:
        existing = s.execute(
            select(Suppression).where(
                Suppression.repo == repo, Suppression.fingerprint == fingerprint
            )
        ).scalar_one_or_none()
        if existing:
            return False
        s.add(
            Suppression(
                repo=repo,
                fingerprint=fingerprint,
                reason=reason,
                claim=claim[:2000],
                file=file,
                note=note[:2000],
                pr_number=pr_number,
            )
        )
        return True


def bump_hits(repo: str, fingerprints: set[str], url: str | None = None) -> None:
    """Count how often suppressions actually fire — proof the memory is earning its keep."""
    if not fingerprints:
        return
    try:
        with session(url) as s:
            for fp in fingerprints:
                row = s.execute(
                    select(Suppression).where(
                        Suppression.repo == repo, Suppression.fingerprint == fp
                    )
                ).scalar_one_or_none()
                if row:
                    row.hits += 1
    except Exception as e:  # noqa: BLE001
        log.debug("bump_hits failed: %s", e)


# --- run history -------------------------------------------------------------


def record_run(
    repo: str,
    result: ReviewResult,
    *,
    model: str,
    cost: float,
    pr_number: int | None = None,
    head_sha: str = "",
    url: str | None = None,
) -> int | None:
    try:
        with session(url) as s:
            u = result.usage
            run = Run(
                repo=repo,
                pr_number=pr_number,
                head_sha=head_sha,
                tier=result.tier,
                model=model,
                posted=len(result.posted),
                suppressed=len(result.suppressed),
                killed_by_verifier=len(result.suppressed),
                input_tokens=u.input_tokens,
                output_tokens=u.output_tokens,
                cache_read_tokens=u.cache_read_input_tokens,
                cache_write_tokens=u.cache_creation_input_tokens,
                cost_usd=cost,
                elapsed_s=result.elapsed_s,
            )
            s.add(run)
            s.flush()

            for vf, posted in [(v, 1) for v in result.posted] + [
                (v, 0) for v in result.suppressed
            ]:
                s.add(_row(run.id, repo, vf, posted))
            return run.id
    except Exception as e:  # noqa: BLE001 - never fail a review over bookkeeping
        log.warning("could not record run: %s", e)
        return None


def _row(run_id: int, repo: str, vf: VerifiedFinding, posted: int) -> FindingRow:
    f = vf.finding
    return FindingRow(
        run_id=run_id,
        repo=repo,
        fingerprint=f.fingerprint(),
        claim=f.claim,
        failure_scenario=f.failure_scenario,
        category=str(f.category),
        severity=str(vf.final_severity),
        confidence=f.confidence,
        file=f.anchor_file,
        line=f.anchor_line,
        found_by=f.found_by,
        was_posted=posted,
        verdicts=[{"lens": v.lens, "refuted": v.refuted} for v in vf.verdicts],
    )


def stats(repo: str | None = None, url: str | None = None) -> dict:
    with session(url) as s:
        q = select(
            func.count(Run.id),
            func.sum(Run.posted),
            func.sum(Run.killed_by_verifier),
            func.sum(Run.cost_usd),
        )
        if repo:
            q = q.where(Run.repo == repo)
        runs, posted, killed, cost = s.execute(q).one()

        sq = select(func.count(Suppression.id), func.sum(Suppression.hits))
        if repo:
            sq = sq.where(Suppression.repo == repo)
        n_sup, sup_hits = s.execute(sq).one()

        return {
            "runs": runs or 0,
            "posted": posted or 0,
            "killed": killed or 0,
            "cost_usd": float(cost or 0.0),
            "suppressions": n_sup or 0,
            "suppression_hits": sup_hits or 0,
        }


# --- live progress -----------------------------------------------------------

STAGES = [
    "triage",
    "context",
    "find",
    "prefilter",
    "verify",
    "gate",
    "post",
    "done",
]


def start_run(
    repo: str,
    *,
    tier: str,
    model: str,
    pr_number: int | None = None,
    head_sha: str = "",
    source: str = "pr",
    actor: str = "",
    url: str | None = None,
) -> int | None:
    """Create the row a dashboard watches while the review is in flight."""
    try:
        with session(url) as s:
            run = Run(
                repo=repo,
                pr_number=pr_number,
                head_sha=head_sha,
                tier=tier,
                model=model,
                source=source,
                actor=actor,
                status="running",
                stage="triage",
                stage_index=0,
            )
            s.add(run)
            s.flush()
            return run.id
    except Exception as e:  # noqa: BLE001
        log.debug("start_run failed: %s", e)
        return None


def set_stage(run_id: int | None, stage: str, url: str | None = None) -> None:
    if run_id is None:
        return
    try:
        with session(url) as s:
            run = s.get(Run, run_id)
            if run:
                run.stage = stage
                run.stage_index = STAGES.index(stage) if stage in STAGES else run.stage_index
    except Exception as e:  # noqa: BLE001
        log.debug("set_stage failed: %s", e)


def finish_run(
    run_id: int | None,
    result: ReviewResult,
    *,
    cost: float,
    error: str = "",
    url: str | None = None,
) -> None:
    if run_id is None:
        return
    try:
        with session(url) as s:
            run = s.get(Run, run_id)
            if not run:
                return
            u = result.usage
            run.status = "failed" if error else "done"
            run.stage = "done"
            run.stage_index = len(STAGES) - 1
            run.error = error[:2000]
            run.finished_at = _utcnow()
            run.posted = len(result.posted)
            run.suppressed = len(result.suppressed)
            run.killed_by_verifier = len(result.suppressed)
            run.input_tokens = u.input_tokens
            run.output_tokens = u.output_tokens
            run.cache_read_tokens = u.cache_read_input_tokens
            run.cache_write_tokens = u.cache_creation_input_tokens
            run.cost_usd = cost
            run.elapsed_s = result.elapsed_s
            for vf, posted in [(v, 1) for v in result.posted] + [
                (v, 0) for v in result.suppressed
            ]:
                s.add(_row(run.id, run.repo, vf, posted))
    except Exception as e:  # noqa: BLE001
        log.warning("finish_run failed: %s", e)


def _utcnow():
    from datetime import UTC, datetime

    return datetime.now(UTC)
