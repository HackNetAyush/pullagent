"""The work queue: in-process, durable, and deliberately not Redis.

A webhook must be answered in milliseconds; a review takes minutes. Something
has to hold the work in between. The obvious answer is arq + Redis, and the
backlog still says so — but a queue is only worth its infrastructure when you
need workers on other machines. Until then, Redis buys one property (survive a
restart) that a table already gives us, in exchange for a service to run,
monitor and secure.

So: the *queue* is in memory, the *record* is in SQLite. Restart replays
anything unfinished, because GitHub does not redeliver a webhook you already
answered 202.

Three behaviours the naive version gets wrong:

**Debounce.** Someone pushes four fixup commits in ninety seconds. Each one is
a `synchronize`. Without a delay that is four reviews of nearly identical
code, and three of them are obsolete before they finish.

**Singleflight.** One review per PR at a time. Two concurrent reviews of the
same PR race to post and produce doubled comments.

**Supersede.** A push during a running review makes that review's output
stale. It is cancelled, not allowed to finish and post against an old commit.

Swapping this for arq later is a change to this file alone: `submit()` and
the handler contract are the whole surface.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from cr.store import db as store

log = logging.getLogger(__name__)


@dataclass
class QueuedJob:
    kind: str
    key: str
    payload: dict = field(default_factory=dict)
    repo: str = ""
    installation_id: int | None = None
    row_id: int | None = None
    run_at: float = 0.0
    # Set when a newer job for the same key cancelled this one, so the handler
    # can tell "superseded" from "crashed" in its own cleanup.
    superseded: bool = False


Handler = Callable[[QueuedJob], Awaitable[None]]


class JobQueue:
    """Bounded-concurrency scheduler with per-key singleflight and debounce."""

    def __init__(
        self,
        handler: Handler,
        *,
        concurrency: int = 2,
        tick_s: float = 0.5,
    ) -> None:
        self._handler = handler
        self._concurrency = max(1, concurrency)
        self._tick = tick_s

        # One waiting job per key. A newer submission replaces the older one
        # outright — that *is* the debounce.
        self._waiting: dict[str, QueuedJob] = {}
        self._running: dict[str, asyncio.Task] = {}
        self._jobs_by_task: dict[asyncio.Task, QueuedJob] = {}

        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task | None = None
        self._stopping = False

    # --- lifecycle ----------------------------------------------------------

    async def start(self, *, recover: bool = True) -> None:
        if self._loop_task is not None:
            return
        self._stopping = False
        self._loop_task = asyncio.create_task(self._scheduler(), name="cr-job-scheduler")
        if recover:
            self.recover()

    async def stop(self, *, grace_s: float = 5.0) -> None:
        """Stop accepting work and put running jobs back on the board.

        A cancelled job is reset to `queued` rather than `failed`: the work
        still needs doing, and the next process start will pick it up.
        """
        self._stopping = True
        self._wake.set()

        if self._loop_task is not None:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
            self._loop_task = None

        for key, task in list(self._running.items()):
            job = self._jobs_by_task.get(task)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, timeout=grace_s)
            if job is not None and job.row_id is not None:
                store.set_job_status(job.row_id, "queued")
            self._running.pop(key, None)

        self._jobs_by_task.clear()
        self._waiting.clear()

    def recover(self) -> int:
        """Re-queue everything the last process left unfinished."""
        rows = store.pending_jobs()
        for row in rows:
            job = QueuedJob(
                kind=row.kind,
                key=row.key,
                payload=dict(row.payload or {}),
                repo=row.repo,
                installation_id=row.installation_id,
                row_id=row.id,
                run_at=time.time(),
            )
            # Straight into the waiting map: the row already exists, so going
            # through submit() would supersede the row we are recovering.
            self._waiting[job.key] = job
        if rows:
            log.info("recovered %d unfinished job(s) from the store", len(rows))
            self._wake.set()
        return len(rows)

    # --- submission ---------------------------------------------------------

    def submit(
        self,
        kind: str,
        key: str,
        *,
        payload: dict | None = None,
        repo: str = "",
        installation_id: int | None = None,
        delay_s: float = 0.0,
        cancel_running: bool = True,
    ) -> int | None:
        """Queue work under `key`, replacing anything older with that key.

        Synchronous on purpose: the webhook handler calls this and returns 202
        without awaiting anything, so a slow queue can never turn into a
        GitHub delivery timeout.
        """
        if self._stopping:
            log.warning("queue is stopping; dropping %s %s", kind, key)
            return None

        row_id = store.enqueue_job(
            kind,
            key,
            payload=payload,
            repo=repo,
            installation_id=installation_id,
            delay_s=delay_s,
        )
        job = QueuedJob(
            kind=kind,
            key=key,
            payload=payload or {},
            repo=repo,
            installation_id=installation_id,
            row_id=row_id,
            run_at=time.time() + max(0.0, delay_s),
        )

        if key in self._waiting:
            log.info("debounced %s: replacing a job still waiting", key)
        self._waiting[key] = job

        # A running job for this key is now producing an answer to a question
        # nobody is asking any more.
        if cancel_running and (task := self._running.get(key)) is not None:
            running = self._jobs_by_task.get(task)
            if running is not None:
                running.superseded = True
            log.info("superseding the in-flight job for %s", key)
            task.cancel()

        self._wake.set()
        return row_id

    # --- scheduling ---------------------------------------------------------

    async def _scheduler(self) -> None:
        while not self._stopping:
            delay = self._dispatch_ready()
            self._wake.clear()
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=delay)

    def _dispatch_ready(self) -> float:
        """Start what is due and return how long to sleep before looking again."""
        now = time.time()
        next_due = self._tick * 20  # nothing waiting: idle poll, not a spin

        for key, job in sorted(self._waiting.items(), key=lambda kv: kv[1].run_at):
            if len(self._running) >= self._concurrency:
                next_due = self._tick
                break
            if key in self._running:
                # Wait for the cancelled predecessor to actually finish before
                # starting the replacement, or both will post.
                next_due = min(next_due, self._tick)
                continue
            if job.run_at > now:
                next_due = min(next_due, job.run_at - now)
                continue

            self._waiting.pop(key, None)
            task = asyncio.create_task(self._run(job), name=f"cr-job:{key}")
            self._running[key] = task
            self._jobs_by_task[task] = job

        return max(0.05, next_due)

    async def _run(self, job: QueuedJob) -> None:
        store.set_job_status(job.row_id, "running")
        started = time.monotonic()
        try:
            await self._handler(job)
        except asyncio.CancelledError:
            status = "superseded" if job.superseded else "queued"
            store.set_job_status(job.row_id, status)
            log.info("job %s %s after %.0fs", job.key, status, time.monotonic() - started)
            raise
        except Exception as exc:  # noqa: BLE001 - one bad job must not stop the queue
            log.exception("job %s failed", job.key)
            store.set_job_status(job.row_id, "failed", error=f"{type(exc).__name__}: {exc}")
        else:
            store.set_job_status(job.row_id, "done")
            log.info("job %s done in %.0fs", job.key, time.monotonic() - started)
        finally:
            task = asyncio.current_task()
            if task is not None:
                self._jobs_by_task.pop(task, None)
                if self._running.get(job.key) is task:
                    self._running.pop(job.key, None)
            self._wake.set()

    # --- introspection ------------------------------------------------------

    @property
    def depth(self) -> int:
        return len(self._waiting)

    @property
    def active(self) -> int:
        return len(self._running)

    def snapshot(self) -> dict:
        return {
            "waiting": [
                {"key": k, "kind": j.kind, "in_s": round(max(0.0, j.run_at - time.time()), 1)}
                for k, j in self._waiting.items()
            ],
            "running": sorted(self._running),
            "concurrency": self._concurrency,
        }

    async def drain(self, timeout: float = 120.0) -> bool:
        """Block until nothing is waiting or running. Tests and shutdown only."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._waiting and not self._running:
                return True
            await asyncio.sleep(0.05)
        return False


# --- key builders ------------------------------------------------------------
#
# The key IS the concurrency policy. Two jobs share a key exactly when the
# later one makes the earlier one pointless.


def review_key(repo: str, pr_number: int) -> str:
    """All reviews of one PR collapse, whatever triggered them."""
    return f"review:{repo}#{pr_number}"


def reply_key(repo: str, comment_id: int) -> str:
    """Replies never collapse: each comment deserves its own answer."""
    return f"reply:{repo}:{comment_id}"


def index_key(repo: str) -> str:
    return f"index:{repo}"
