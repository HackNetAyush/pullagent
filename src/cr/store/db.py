"""Store access. Sync SQLAlchemy — the review path makes two calls, not two hundred."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from cr.models import ReviewResult, VerifiedFinding
from cr.store.models import (
    SCHEMA_VERSION,
    Account,
    Base,
    CustomTier,
    Delivery,
    FindingRow,
    Installation,
    Job,
    KeyTest,
    Meta,
    ProviderConnection,
    PRState,
    RepoSettings,
    RoutingRules,
    Run,
    Suppression,
    User,
    UserSession,
)

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


_ENGINE = None
_SESSION: sessionmaker[Session] | None = None


def default_url() -> str:
    """CR_DB_URL when set, else SQLite under the cache dir.

    A container has no persistent disk, so a deployed instance must be pointed
    at Postgres; the SQLite default is for a laptop, where it is exactly right.
    """
    from cr.config import settings

    if settings.db_url:
        return settings.db_url

    from cr.repo import default_cache_dir

    root = default_cache_dir()
    root.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{(root / 'cr.db').as_posix()}"


def init(url: str | None = None) -> sessionmaker[Session]:
    global _ENGINE, _SESSION
    if _SESSION is not None:
        return _SESSION

    url = url or default_url()
    kwargs: dict = {"future": True}
    if url.startswith("sqlite:///") and (p := url[len("sqlite:///") :]):
        Path(p).parent.mkdir(parents=True, exist_ok=True)
    elif not url.startswith("sqlite"):
        # Container Apps scales the worker to zero and Postgres closes idle
        # connections; without pre-ping the first query after a quiet spell
        # fails on a dead socket instead of reconnecting.
        kwargs |= {"pool_pre_ping": True, "pool_recycle": 1800, "pool_size": 5, "max_overflow": 5}

    engine = create_engine(url, **kwargs)
    factory = sessionmaker(bind=engine, future=True)
    with _schema_lock(engine):
        Base.metadata.create_all(engine)
        _add_missing_columns(engine)
        with factory() as s:
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
    # Published only once the schema is in place: a failed start-up leaves
    # nothing half-initialised for the next call to trust.
    _ENGINE, _SESSION = engine, factory
    return _SESSION


# Any fixed number, the same in every process: the key of the advisory lock
# that serialises schema changes.
_SCHEMA_LOCK = 0x70756C6C  # "pull"


@contextmanager
def _schema_lock(engine) -> Iterator[None]:
    """One process at a time through schema creation and migration.

    The web and worker apps start together after a deploy and both run it; on
    Postgres, two concurrent CREATE TABLEs for the same new table fail the
    second. A session-level advisory lock makes the second wait, then find the
    tables there. SQLite serialises writers by itself.
    """
    if engine.dialect.name != "postgresql":
        yield
        return
    from sqlalchemy import text

    with engine.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _SCHEMA_LOCK})
        try:
            yield
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _SCHEMA_LOCK})


def _add_missing_columns(engine) -> None:
    """create_all never alters an existing table, so a new column would break
    every query against a database created by an older version. Add them."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    wanted: dict[str, dict[str, str]] = {
        "runs": {
            "source": "VARCHAR(16) DEFAULT 'pr'",
            "actor": "VARCHAR(128) DEFAULT ''",
            "verified_count": "INTEGER DEFAULT 0",
            "cache_hit": "BOOLEAN DEFAULT FALSE",
            "cached_cost_usd": "FLOAT DEFAULT 0.0",
            "judge_cost_usd": "FLOAT DEFAULT 0.0",
            "billing": "VARCHAR(16) DEFAULT 'managed'",
            "account": "VARCHAR(255) DEFAULT ''",
            "model_costs": "JSON DEFAULT '[]'",
        },
        # JSON is TEXT on SQLite and JSON on Postgres; both accept '[]'.
        "users": {"orgs": "JSON DEFAULT '[]'", "admin_orgs": "JSON DEFAULT '[]'"},
    }
    # Columns whose VARCHAR length grew. SQLite ignores lengths; Postgres
    # enforces them, and widening one there is a catalogue change, not a rewrite.
    widened = {("runs", "model"): 255}
    postgres = engine.dialect.name == "postgresql"
    # The web and worker apps start together and both run this; on Postgres the
    # one that loses the race must find the column there, not fail on it.
    add = "ADD COLUMN IF NOT EXISTS" if postgres else "ADD COLUMN"
    with engine.begin() as conn:
        for table, columns in wanted.items():
            if table not in tables:
                continue
            have = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in columns.items():
                if name not in have:
                    conn.execute(text(f"ALTER TABLE {table} {add} {name} {ddl}"))
                    log.info("store: added %s.%s", table, name)
        if postgres:
            for (table, name), size in widened.items():
                if table not in tables:
                    continue
                col = next((c for c in insp.get_columns(table) if c["name"] == name), None)
                length = getattr(col["type"], "length", None) if col else None
                if length is not None and length < size:
                    conn.execute(
                        text(f"ALTER TABLE {table} ALTER COLUMN {name} TYPE VARCHAR({size})")
                    )
                    log.info("store: widened %s.%s to %d", table, name, size)
        # Runs from before workspaces existed belong to their repository's
        # owner. One statement: a row-by-row loop would hold the table's lock
        # for as long as there are runs.
        if "runs" in tables:
            owner = (
                "split_part(repo, '/', 1)"
                if postgres
                else "CASE WHEN instr(repo, '/') > 0 "
                "THEN substr(repo, 1, instr(repo, '/') - 1) ELSE repo END"
            )
            done = conn.execute(
                text(
                    f"UPDATE runs SET account = {owner} "
                    "WHERE (account IS NULL OR account = '') AND repo IS NOT NULL"
                )
            ).rowcount
            if done:
                log.info("store: assigned %d runs to their workspace", done)


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
                account=repo.split("/", 1)[0],
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

            for vf, posted in [(v, 1) for v in result.posted] + [(v, 0) for v in result.suppressed]:
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
        claim=vf.final_claim,
        failure_scenario=vf.final_failure_scenario,
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
    "merge",
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
    billing: str = "managed",
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
                billing=billing,
                account=repo.split("/", 1)[0],
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


def add_judge_cost(run_id: int | None, amount: float, url: str | None = None) -> None:
    """Attribute a benchmark's gold-label scoring cost back to the review row
    it graded. Called after engine.review() has already returned and closed
    that row, so it cannot go through finish_run(). Never called by real
    `cr review-pr` usage — only the benchmark script's judge loop uses this."""
    if run_id is None or amount <= 0:
        return
    try:
        with session(url) as s:
            run = s.get(Run, run_id)
            if run:
                run.judge_cost_usd += amount
    except Exception as e:  # noqa: BLE001
        log.debug("add_judge_cost failed: %s", e)


def close_run(run_id: int | None, *, status: str, error: str = "", url: str | None = None) -> None:
    """Close a run that produced no ReviewResult — cancelled or aborted.

    Without this, a review superseded by a new push stays `running` in the
    ledger forever and the dashboard eventually calls it stalled, which reads
    as a crash rather than the deliberate cancellation it was.
    """
    if run_id is None:
        return
    try:
        with session(url) as s:
            run = s.get(Run, run_id)
            if run is None or run.status != "running":
                return
            run.status = status
            run.stage = "done"
            run.stage_index = len(STAGES) - 1
            run.error = error[:2000]
            run.finished_at = _utcnow()
    except Exception as e:  # noqa: BLE001
        log.warning("close_run failed: %s", e)


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
            run.killed_by_verifier = len(result.refuted)
            run.verified_count = result.verified_count
            run.input_tokens = u.input_tokens
            run.output_tokens = u.output_tokens
            run.cache_read_tokens = u.cache_read_input_tokens
            run.cache_write_tokens = u.cache_creation_input_tokens
            run.cost_usd = cost
            run.elapsed_s = result.elapsed_s
            run.cache_hit = result.cache_hit
            run.cached_cost_usd = result.cached_cost_usd
            run.model_costs = [mc.model_dump() for mc in result.model_costs]
            for vf, posted in [(v, 1) for v in result.posted] + [(v, 0) for v in result.suppressed]:
                s.add(_row(run.id, run.repo, vf, posted))
    except Exception as e:  # noqa: BLE001
        log.warning("finish_run failed: %s", e)


# --- GitHub App state --------------------------------------------------------


def mark_delivery(
    delivery_id: str,
    *,
    event: str = "",
    action: str = "",
    repo: str = "",
    url: str | None = None,
) -> bool:
    """Claim a webhook delivery. False means we have already handled it.

    Written before any work starts, so a redelivery arriving while the first is
    still reviewing is rejected too — primary-key uniqueness is what makes that
    a race we cannot lose. GitHub redelivers on timeout and on manual replay,
    and a redelivered `synchronize` is indistinguishable from a real push.
    """
    from sqlalchemy.exc import IntegrityError

    try:
        with session(url) as s:
            s.add(
                Delivery(
                    delivery_id=delivery_id,
                    event=event[:48],
                    action=action[:48],
                    repo=repo[:255],
                )
            )
        return True
    except IntegrityError:
        return False
    except Exception as e:  # noqa: BLE001 - dedup must never take the ingress down
        log.warning("mark_delivery failed, processing anyway: %s", e)
        return True


def prune_deliveries(keep_days: int = 14, url: str | None = None) -> int:
    """Deliveries only need to outlive GitHub's redelivery window."""
    from datetime import timedelta

    cutoff = _utcnow() - timedelta(days=keep_days)
    try:
        with session(url) as s:
            rows = list(s.execute(select(Delivery).where(Delivery.received_at < cutoff)).scalars())
            for r in rows:
                s.delete(r)
            return len(rows)
    except Exception as e:  # noqa: BLE001
        log.debug("prune_deliveries failed: %s", e)
        return 0


def upsert_installation(
    installation_id: int,
    *,
    account: str = "",
    account_type: str = "",
    repos: list[str] | None = None,
    repo_selection: str | None = None,
    suspended: bool | None = None,
    removed: bool | None = None,
    url: str | None = None,
) -> None:
    """Record or update an installation.

    `repos` is merged, never replaced: `installation_repositories.added` names
    only the delta, so replacing would forget every previously granted repo.
    """
    try:
        with session(url) as s:
            row = s.get(Installation, installation_id)
            if row is None:
                row = Installation(id=installation_id, repos=[])
                s.add(row)
            if account:
                row.account = account[:255]
            if account_type:
                row.account_type = account_type[:32]
            if repos is not None:
                row.repos = sorted(set(row.repos or []) | set(repos))
            if repo_selection:
                row.repo_selection = repo_selection[:16]
            if suspended is not None:
                row.suspended = suspended
            if removed is not None:
                row.removed = removed
    except Exception as e:  # noqa: BLE001
        log.warning("upsert_installation failed: %s", e)


def remove_account_installations(account: str, url: str | None = None) -> list[int]:
    """Mark every installation on `account` removed; the ids that changed."""
    with session(url) as s:
        rows = s.execute(
            select(Installation).where(
                func.lower(Installation.account) == account.lower(), ~Installation.removed
            )
        ).scalars()
        gone = []
        for row in rows:
            row.removed = True
            gone.append(row.id)
        return gone


def drop_installation_repos(installation_id: int, repos: list[str], url: str | None = None) -> None:
    try:
        with session(url) as s:
            row = s.get(Installation, installation_id)
            if row:
                row.repos = sorted(set(row.repos or []) - set(repos))
    except Exception as e:  # noqa: BLE001
        log.warning("drop_installation_repos failed: %s", e)


def installations(url: str | None = None) -> list[Installation]:
    with session(url) as s:
        rows = list(s.execute(select(Installation).where(~Installation.removed)).scalars())
        for r in rows:
            s.expunge(r)
        return rows


def installation_for_repo(repo: str, url: str | None = None) -> int | None:
    """The installation id that granted access to `repo`, if we have seen it."""
    try:
        with session(url) as s:
            for row in s.execute(
                select(Installation).where(~Installation.removed, ~Installation.suspended)
            ).scalars():
                if repo in (row.repos or []):
                    return row.id
    except Exception as e:  # noqa: BLE001
        log.debug("installation_for_repo failed: %s", e)
    return None


def pr_state(repo: str, pr_number: int, url: str | None = None) -> PRState | None:
    with session(url) as s:
        row = s.execute(
            select(PRState).where(PRState.repo == repo, PRState.pr_number == pr_number)
        ).scalar_one_or_none()
        if row is not None:
            s.expunge(row)
        return row


def record_pr_review(
    repo: str,
    pr_number: int,
    *,
    head_sha: str,
    base_sha: str = "",
    comments: int = 0,
    installation_id: int | None = None,
    url: str | None = None,
) -> None:
    """Advance the incremental-review watermark for this PR."""
    try:
        with session(url) as s:
            row = s.execute(
                select(PRState).where(PRState.repo == repo, PRState.pr_number == pr_number)
            ).scalar_one_or_none()
            if row is None:
                row = PRState(repo=repo, pr_number=pr_number, reviews=0, comments_posted=0)
                s.add(row)
            row.last_reviewed_sha = head_sha
            if base_sha:
                row.base_sha = base_sha
            if installation_id is not None:
                row.installation_id = installation_id
            row.reviews += 1
            row.comments_posted += comments
            row.last_reviewed_at = _utcnow()
    except Exception as e:  # noqa: BLE001
        log.warning("record_pr_review failed: %s", e)


def enqueue_job(
    kind: str,
    key: str,
    *,
    payload: dict | None = None,
    repo: str = "",
    installation_id: int | None = None,
    delay_s: float = 0.0,
    url: str | None = None,
) -> int | None:
    """Persist a job, superseding any earlier unfinished job with the same key.

    Superseding at write time is what makes five pushes in a minute cost one
    review: the four older rows are closed out before a worker picks them up.
    """
    from datetime import timedelta

    try:
        with session(url) as s:
            for old in s.execute(
                select(Job).where(Job.key == key, Job.status.in_(("queued", "running")))
            ).scalars():
                old.status = "superseded"
                old.finished_at = _utcnow()
            job = Job(
                kind=kind,
                key=key[:320],
                repo=repo[:255],
                installation_id=installation_id,
                payload=payload or {},
                status="queued",
                run_after=_utcnow() + timedelta(seconds=delay_s) if delay_s else _utcnow(),
            )
            s.add(job)
            s.flush()
            return job.id
    except Exception as e:  # noqa: BLE001
        log.warning("enqueue_job failed: %s", e)
        return None


def job_status(job_id: int | None, url: str | None = None) -> str:
    if job_id is None:
        return "unknown"
    try:
        with session(url) as s:
            row = s.get(Job, job_id)
            return row.status if row else "unknown"
    except Exception as e:  # noqa: BLE001
        log.debug("job_status failed: %s", e)
        return "unknown"


def set_job_status(
    job_id: int | None, status: str, *, error: str = "", url: str | None = None
) -> None:
    if job_id is None:
        return
    try:
        with session(url) as s:
            row = s.get(Job, job_id)
            if row is None:
                return
            # A job superseded while running must stay superseded: the worker
            # that finishes last would otherwise report its own outcome for a
            # key a newer job already owns.
            if row.status == "superseded" and status != "running":
                return
            row.status = status
            if error:
                row.error = error[:2000]
                row.attempts += 1
            if status == "running":
                row.started_at = _utcnow()
                row.run_after = _utcnow()  # initial worker lease timestamp
            elif status in ("done", "failed", "skipped"):
                row.finished_at = _utcnow()
    except Exception as e:  # noqa: BLE001
        log.warning("set_job_status failed: %s", e)


def pending_jobs(limit: int = 200, url: str | None = None) -> list[Job]:
    """Jobs to replay after a restart.

    `running` counts as pending: the process that owned it is gone, so nothing
    else will ever finish it, and GitHub does not redeliver a webhook we
    already answered 202.
    """
    try:
        with session(url) as s:
            rows = list(
                s.execute(
                    select(Job)
                    .where(Job.status.in_(("queued", "running")))
                    .order_by(Job.id)
                    .limit(limit)
                ).scalars()
            )
            for r in rows:
                s.expunge(r)
            return rows
    except Exception as e:  # noqa: BLE001
        log.warning("pending_jobs failed: %s", e)
        return []


def heartbeat_job(job_id: int, url: str | None = None) -> None:
    """Keep a running job's lease fresh while its worker is alive."""
    with session(url) as s:
        row = s.get(Job, job_id)
        if row is not None and row.status == "running":
            row.run_after = _utcnow()


def recover_jobs(url: str | None = None, *, limit: int = 200) -> list[tuple[int, str]]:
    """Re-send lost queue nudges and reclaim jobs whose worker disappeared.

    The web tier calls this every minute, so recovery also works after the
    last worker has scaled to zero. `run_after` doubles as the heartbeat for a
    running job; it has no scheduling meaning after the job is claimed.
    """
    now = _utcnow().replace(tzinfo=None)
    queued_cutoff = now - timedelta(seconds=60)
    running_cutoff = now - timedelta(minutes=2)
    nudges: list[tuple[int, str]] = []
    with session(url) as s:
        rows = s.execute(
            select(Job)
            .where(Job.status.in_(("queued", "running")))
            .order_by(Job.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).scalars()
        for row in rows:
            if row.status == "running":
                payload = row.payload or {}
                if row.kind == "review" and payload.get("pr_number"):
                    state = s.execute(
                        select(PRState).where(
                            PRState.repo == row.repo,
                            PRState.pr_number == int(payload["pr_number"]),
                        )
                    ).scalar_one_or_none()
                    if (
                        state is not None
                        and state.last_reviewed_sha == payload.get("head_sha")
                        and state.last_reviewed_at is not None
                        and state.last_reviewed_at >= row.created_at
                    ):
                        row.status = "done"
                        row.finished_at = state.last_reviewed_at
                        continue
                if row.run_after > running_cutoff:
                    continue
                row.status = "queued"
                row.started_at = None
            elif row.run_after > queued_cutoff:
                continue

            row.run_after = now
            nudges.append((row.id, row.key))
    return nudges


def recent_jobs(limit: int = 50, url: str | None = None) -> list[Job]:
    return jobs_page(limit=limit, url=url)[0]


def jobs_page(
    *,
    limit: int = 50,
    offset: int = 0,
    kind: str | None = None,
    status: str | None = None,
    repo: str | None = None,
    account: str | None = None,
    url: str | None = None,
) -> tuple[list[Job], int]:
    """One page of jobs plus the total, so the queue view can page honestly."""
    limit, offset = max(1, min(limit, 200)), max(0, offset)
    with session(url) as s:
        where = []
        if kind:
            where.append(Job.kind == kind)
        if status:
            where.append(Job.status == status)
        if repo:
            where.append(Job.repo == repo)
        if account:
            where.append(func.lower(Job.repo).like(account.lower() + "/%"))
        total = s.execute(select(func.count(Job.id)).where(*where)).scalar_one()
        rows = list(
            s.execute(
                select(Job).where(*where).order_by(Job.id.desc()).limit(limit).offset(offset)
            ).scalars()
        )
        for r in rows:
            s.expunge(r)
        return rows, total


# --- account allowlist, sign-in, sessions ------------------------------------


def account_status(login: str, url: str | None = None) -> str:
    """ "approved", "denied", "pending", or "unknown" for an account never seen.

    Deliberately fails closed: if the store is unreachable we return "unknown"
    and the caller drops the event. A database outage must not become an open
    door onto the model budget.
    """
    if not login:
        return "unknown"
    try:
        with session(url) as s:
            row = s.get(Account, login)
            return row.status if row else "unknown"
    except Exception as e:  # noqa: BLE001
        log.warning("account_status(%s) failed, treating as unknown: %s", login, e)
        return "unknown"


def record_account(
    login: str,
    *,
    status: str | None = None,
    account_type: str = "",
    note: str | None = None,
    decided_by: str | None = None,
    requested_by: str | None = None,
    url: str | None = None,
) -> str:
    """Create or update an account row; returns the resulting status."""
    with session(url) as s:
        row = s.get(Account, login)
        if row is None:
            row = Account(login=login, status=status or "pending")
            s.add(row)
        elif status is not None:
            row.status = status
        if account_type:
            row.account_type = account_type
        if note is not None:
            row.note = note
        if requested_by is not None:
            row.requested_by = requested_by
        if decided_by is not None:
            row.decided_by = decided_by
            row.decided_at = _utcnow()
        return row.status


def note_blocked_event(login: str, account_type: str = "", url: str | None = None) -> None:
    """Count an event we refused to act on.

    Without this an unapproved install is indistinguishable from a broken
    webhook — someone is waiting for a review that will never come, and nothing
    anywhere says so.
    """
    try:
        with session(url) as s:
            row = s.get(Account, login)
            if row is None:
                # Column defaults are applied at flush, so a fresh row's counter
                # is still None here and `+= 1` would raise into the swallow below.
                row = Account(
                    login=login, status="pending", account_type=account_type, blocked_events=0
                )
                s.add(row)
            row.blocked_events = (row.blocked_events or 0) + 1
            row.last_blocked_at = _utcnow()
    except Exception as e:  # noqa: BLE001 - never fail an ingress path on bookkeeping
        log.debug("note_blocked_event failed: %s", e)


def accounts(status: str | None = None, url: str | None = None) -> list[Account]:
    with session(url) as s:
        q = select(Account)
        if status:
            q = q.where(Account.status == status)
        rows = list(s.execute(q.order_by(Account.updated_at.desc())).scalars())
        for r in rows:
            s.expunge(r)
        return rows


def upsert_user(
    user_id: int,
    *,
    login: str,
    name: str = "",
    avatar_url: str = "",
    email: str = "",
    is_admin: bool | None = None,
    orgs: list[str] | None = None,
    admin_orgs: list[str] | None = None,
    url: str | None = None,
) -> User:
    with session(url) as s:
        row = s.get(User, user_id)
        if row is None:
            row = User(id=user_id, login=login)
            s.add(row)
        row.login, row.name, row.avatar_url, row.email = login, name, avatar_url, email
        row.last_seen_at = _utcnow()
        if is_admin is not None:
            row.is_admin = is_admin
        if orgs is not None:
            row.orgs = sorted(set(orgs))
        if admin_orgs is not None:
            row.admin_orgs = sorted(set(admin_orgs))
        s.flush()
        s.expunge(row)
        return row


def create_session(token: str, user_id: int, ttl_s: int, url: str | None = None) -> None:
    with session(url) as s:
        s.add(
            UserSession(
                id=token,
                user_id=user_id,
                expires_at=_utcnow() + timedelta(seconds=ttl_s),
            )
        )


def session_user(token: str, url: str | None = None) -> User | None:
    """The signed-in user for a session token, or None if absent or expired."""
    if not token:
        return None
    try:
        with session(url) as s:
            row = s.get(UserSession, token)
            if row is None:
                return None
            # Compare naive-to-naive: SQLite hands back tz-less datetimes.
            if row.expires_at.replace(tzinfo=None) < _utcnow().replace(tzinfo=None):
                s.delete(row)
                return None
            user = s.get(User, row.user_id)
            if user is not None:
                s.expunge(user)
            return user
    except Exception as e:  # noqa: BLE001
        log.debug("session_user failed: %s", e)
        return None


def delete_session(token: str, url: str | None = None) -> None:
    with session(url) as s:
        if row := s.get(UserSession, token):
            s.delete(row)


def purge_expired_sessions(url: str | None = None) -> int:
    with session(url) as s:
        rows = list(
            s.execute(
                select(UserSession).where(UserSession.expires_at < _utcnow().replace(tzinfo=None))
            ).scalars()
        )
        for r in rows:
            s.delete(r)
        return len(rows)


def admin_count(url: str | None = None) -> int:
    with session(url) as s:
        return int(
            s.execute(select(func.count()).select_from(User).where(User.is_admin)).scalar() or 0
        )


def users(url: str | None = None) -> list[User]:
    with session(url) as s:
        rows = list(s.execute(select(User).order_by(User.last_seen_at.desc())).scalars())
        for r in rows:
            s.expunge(r)
        return rows


def claim_job(row_id: int, url: str | None = None) -> Job | None:
    """Move a job from queued to running, once.

    The return value is the whole concurrency story for a multi-replica
    deployment: two workers handed the same message both call this, exactly one
    sees a `queued` row, and the loser gets None and drops its copy. Superseded
    rows also return None, which is how a debounced burst collapses — the
    message still arrives, it just finds nothing left to do.
    """
    try:
        with session(url) as s:
            row = s.get(Job, row_id, with_for_update=True)
            if row is None or row.status != "queued":
                return None
            row.status = "running"
            row.started_at = _utcnow()
            row.run_after = _utcnow()
            s.flush()
            s.expunge(row)
            return row
    except Exception as e:  # noqa: BLE001
        log.warning("claim_job(%s) failed: %s", row_id, e)
        return None


def job_is_current(row_id: int | None, url: str | None = None) -> bool:
    """Is this job still the one that should post?

    A worker cannot be cancelled across a process boundary, so a long review
    can outlive the push that asked for it. Checking this before posting is
    what stops a superseded review from commenting on a stale commit.
    """
    if row_id is None:
        return True
    try:
        with session(url) as s:
            row = s.get(Job, row_id)
            return row is None or row.status == "running"
    except Exception as e:  # noqa: BLE001 - a check that fails must not block posting
        log.debug("job_is_current(%s) failed: %s", row_id, e)
        return True


# --- bring-your-own-key and custom tiers --------------------------------------
#
# Rows leave the session expunged so callers can read them afterwards. Nothing
# here encrypts or decrypts: the store only ever holds ciphertext, and
# `cr.app.workspace` is the one place that turns it back into a key.


def connections(account: str, url: str | None = None) -> list[ProviderConnection]:
    with session(url) as s:
        rows = list(
            s.scalars(
                select(ProviderConnection)
                .where(func.lower(ProviderConnection.account) == account.lower())
                .order_by(ProviderConnection.created_at, ProviderConnection.id)
            )
        )
        for r in rows:
            s.expunge(r)
        return rows


def connection(account: str, conn_id: int, url: str | None = None) -> ProviderConnection | None:
    with session(url) as s:
        row = s.get(ProviderConnection, conn_id)
        if row is None or row.account.lower() != account.lower():
            return None
        s.expunge(row)
        return row


def save_connection(
    account: str,
    *,
    provider: str,
    label: str,
    models: list[dict],
    ciphertext: str | None = None,
    hint: str | None = None,
    resource: str = "",
    conn_id: int | None = None,
    created_by: str = "",
    tested: bool = True,
    url: str | None = None,
) -> ProviderConnection:
    """Create, or update connection `conn_id` of `account`. A None ciphertext
    keeps the stored key. Raises LookupError for another account's id."""
    with session(url) as s:
        if conn_id is None:
            if ciphertext is None:
                raise ValueError("a new connection needs a key")
            row = ProviderConnection(account=account, provider=provider, created_by=created_by)
            s.add(row)
        else:
            found = s.get(ProviderConnection, conn_id)
            if found is None or found.account.lower() != account.lower():
                raise LookupError(f"no connection {conn_id}")
            row = found
        if ciphertext is not None:
            row.ciphertext, row.hint = ciphertext, hint or ""
        row.label, row.resource, row.models = label, resource, models
        if tested:
            row.tested_at = _utcnow()
        s.flush()
        s.expunge(row)
        return row


def delete_connection(account: str, conn_id: int, url: str | None = None) -> bool:
    with session(url) as s:
        row = s.get(ProviderConnection, conn_id)
        if row is None or row.account.lower() != account.lower():
            return False
        s.delete(row)
        return True


def key_tests_since(account: str, since: datetime, url: str | None = None) -> int:
    with session(url) as s:
        return int(
            s.scalar(
                select(func.count(KeyTest.id)).where(
                    func.lower(KeyTest.account) == account.lower(), KeyTest.created_at >= since
                )
            )
            or 0
        )


def note_key_test(account: str, url: str | None = None) -> None:
    with session(url) as s:
        s.add(KeyTest(account=account))


def custom_tiers(account: str, url: str | None = None) -> list[CustomTier]:
    with session(url) as s:
        rows = list(
            s.scalars(
                select(CustomTier)
                .where(func.lower(CustomTier.account) == account.lower())
                .order_by(CustomTier.name)
            )
        )
        for r in rows:
            s.expunge(r)
        return rows


def custom_tier(account: str, tier_id: int, url: str | None = None) -> CustomTier | None:
    with session(url) as s:
        row = s.get(CustomTier, tier_id)
        if row is None or row.account.lower() != account.lower():
            return None
        s.expunge(row)
        return row


def save_custom_tier(
    account: str,
    *,
    name: str,
    config: dict,
    tier_id: int | None = None,
    created_by: str = "",
    url: str | None = None,
) -> CustomTier:
    """Create, or update the tier `tier_id` belonging to `account`.

    Raises ValueError when the account already uses the name on another tier,
    and LookupError when `tier_id` is not one of this account's tiers.
    """
    with session(url) as s:
        clash = s.scalars(
            select(CustomTier).where(
                func.lower(CustomTier.account) == account.lower(),
                func.lower(CustomTier.name) == name.lower(),
            )
        ).first()
        if clash is not None and clash.id != tier_id:
            raise ValueError(f"a tier named {name!r} already exists")
        if tier_id is None:
            row = CustomTier(account=account, created_by=created_by)
            s.add(row)
        else:
            found = s.get(CustomTier, tier_id)
            if found is None or found.account.lower() != account.lower():
                raise LookupError(f"no tier {tier_id}")
            row = found
        row.name, row.config = name, config
        s.flush()
        s.expunge(row)
        return row


def _rules_row(s: Session, account: str) -> RoutingRules | None:
    return s.scalars(
        select(RoutingRules).where(func.lower(RoutingRules.account) == account.lower())
    ).first()


def _normalise_rules(raw: dict | None) -> dict:
    raw = raw or {}
    return {"all": raw.get("all"), "repos": dict(raw.get("repos") or {})}


def delete_custom_tier(account: str, tier_id: int, url: str | None = None) -> bool:
    """Delete a tier, and drop every routing rule that pointed at it, so those
    repositories go back to CR's managed review."""
    with session(url) as s:
        row = s.get(CustomTier, tier_id)
        if row is None or row.account.lower() != account.lower():
            return False
        s.delete(row)
        routing = _rules_row(s, account)
        if routing is not None:
            rules = _normalise_rules(routing.rules)
            routing.rules = {
                "all": None if rules["all"] == tier_id else rules["all"],
                "repos": {r: t for r, t in rules["repos"].items() if t != tier_id},
            }
        return True


def routing_rules(account: str, url: str | None = None) -> dict:
    with session(url) as s:
        row = _rules_row(s, account)
        return _normalise_rules(row.rules if row is not None else None)


def save_routing_rules(
    account: str, rules: dict, *, updated_by: str = "", url: str | None = None
) -> None:
    with session(url) as s:
        row = _rules_row(s, account)
        if row is None:
            row = RoutingRules(account=account)
            s.add(row)
        row.rules, row.updated_by = _normalise_rules(rules), updated_by


def account_repos(account: str, url: str | None = None) -> list[str]:
    """Repositories of this account CR knows: granted to the App, or reviewed."""
    prefix = account.lower() + "/"
    with session(url) as s:
        names = {
            r
            for i in s.scalars(
                select(Installation).where(func.lower(Installation.account) == account.lower())
            )
            if not i.removed
            for r in (i.repos or [])
        }
        names |= set(s.scalars(select(Run.repo).distinct()))
    return sorted((n for n in names if n.lower().startswith(prefix)), key=str.lower)


def known_accounts(url: str | None = None) -> list[str]:
    """Every account CR has heard of: allowlisted, installed, or reviewed."""
    with session(url) as s:
        names = {a.login for a in s.scalars(select(Account))}
        names |= {i.account for i in s.scalars(select(Installation)) if i.account}
        names |= {r.split("/", 1)[0] for r in s.scalars(select(Run.repo).distinct()) if "/" in r}
    return sorted(names, key=str.lower)


def byok_spend(account: str, since: datetime, url: str | None = None) -> dict[str, float]:
    """Spend on an account's own connections since `since`, keyed by the model
    reference a tier used (`conn:<id>:<model>`)."""
    prefix = account.lower() + "/"
    out: dict[str, float] = {}
    with session(url) as s:
        rows = s.scalars(
            select(Run).where(
                Run.billing == "byok",
                Run.status == "done",
                Run.created_at >= since.replace(tzinfo=None),
            )
        )
        for r in rows:
            if not r.repo.lower().startswith(prefix):
                continue
            for mc in r.model_costs or []:
                out[mc["model"]] = out.get(mc["model"], 0.0) + float(mc.get("cost_usd") or 0.0)
    return out


def repo_guidelines(repo: str, url: str | None = None) -> str:
    """The owner's review guidelines for a repository, or "". Never raises: a
    store hiccup must not fail a review over optional context."""
    try:
        with session(url) as s:
            row = s.get(RepoSettings, repo.lower())
            return row.guidelines if row is not None else ""
    except Exception as e:  # noqa: BLE001
        log.warning("could not read guidelines for %s: %s", repo, e)
        return ""


def account_guidelines(account: str, url: str | None = None) -> dict[str, str]:
    with session(url) as s:
        rows = s.scalars(
            select(RepoSettings).where(
                func.lower(RepoSettings.account) == account.lower(), RepoSettings.guidelines != ""
            )
        )
        return {r.repo: r.guidelines for r in rows}


def save_repo_guidelines(
    repo: str, account: str, text: str, *, updated_by: str = "", url: str | None = None
) -> None:
    """Keyed by the lower-cased repository, since GitHub names are
    case-insensitive and webhooks do not promise one casing."""
    with session(url) as s:
        row = s.get(RepoSettings, repo.lower())
        if row is None:
            row = RepoSettings(repo=repo.lower(), account=account)
            s.add(row)
        row.guidelines, row.updated_by = text, updated_by
