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
    Delivery,
    FindingRow,
    Installation,
    Job,
    Meta,
    PRState,
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

    _ENGINE = create_engine(url, **kwargs)
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
        "verified_count": "INTEGER DEFAULT 0",
        "cache_hit": "BOOLEAN DEFAULT 0",
        "cached_cost_usd": "FLOAT DEFAULT 0.0",
        "judge_cost_usd": "FLOAT DEFAULT 0.0",
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


def recent_jobs(limit: int = 50, url: str | None = None) -> list[Job]:
    return jobs_page(limit=limit, url=url)[0]


def jobs_page(
    *,
    limit: int = 50,
    offset: int = 0,
    kind: str | None = None,
    status: str | None = None,
    repo: str | None = None,
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
