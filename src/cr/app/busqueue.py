"""The queue, for deployments that run more than one replica.

`JobQueue` keeps its state in memory, which is correct on a laptop and wrong
the moment a second container exists: both would debounce against their own
private view and both would review the same push.

The fix is not to move the state into Service Bus. It is to notice that the
state is *already* in Postgres — `enqueue_job` supersedes older rows for a key
at write time — and that what is actually missing is a way to wake some worker,
somewhere, and to have exactly one of them act. So:

    web      enqueue_job() -> row id            (Postgres decides what is current)
             send({row_id}) ------------------> Service Bus
    worker   receive() -> claim_job(row_id)     (Postgres decides who runs it)
                            |- None  -> superseded or already taken; drop it
                            '- row   -> run it

Service Bus carries no state of its own beyond "look at row N", which means a
duplicated delivery, a redelivery after a crash, and a message that lost its
race all converge on the same answer without coordination.

Two honest limitations:

**A running job cannot be cancelled across processes.** In-process, a newer
push cancels the asyncio task. Here the older review keeps running; it is
`job_is_current()` immediately before posting that stops it commenting on a
commit nobody is looking at any more. The work is wasted, the output is not
wrong.

**Debounce is a scheduled message, not a sliding window.** Five pushes schedule
five messages; four of them find a superseded row and cost one Postgres read
each. That is the trade for not holding a timer in a process that may vanish.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from cr.app.jobs import QueuedJob
from cr.store import db as store

log = logging.getLogger(__name__)

Handler = Callable[[QueuedJob], Awaitable[None]]

# How long a worker may hold a message before Service Bus assumes it died and
# redelivers. Reviews are minutes, so the lock is renewed rather than set long:
# a genuinely dead worker should not hold work hostage for its whole timeout.
LOCK_RENEW_S = 30.0


class ServiceBusQueue:
    """Drop-in replacement for `JobQueue`, backed by Azure Service Bus.

    Deliberately the same surface — `submit`, `start`, `stop`, `drain`,
    `snapshot`, `depth` — so `AppService` does not know which one it has.
    """

    def __init__(
        self,
        handler: Handler,
        *,
        connection_string: str,
        queue_name: str = "cr-jobs",
        concurrency: int = 2,
        consume: bool = True,
    ) -> None:
        self._handler = handler
        self._conn = connection_string
        self._queue_name = queue_name
        self._concurrency = max(1, concurrency)
        # The web tier only ever produces. Starting a receiver there would have
        # ingress replicas competing with workers for the same messages.
        self._consume = consume

        self._client: Any = None
        self._sender: Any = None
        self._loop_task: asyncio.Task | None = None
        self._running: dict[str, asyncio.Task] = {}
        self._stopping = False
        self._sem = asyncio.Semaphore(self._concurrency)
        # The SDK sender opens one AMQP link. Simultaneous first sends during
        # orphan recovery can corrupt that link and strand persisted rows.
        self._send_lock = asyncio.Lock()

    # --- lifecycle ----------------------------------------------------------

    async def start(self, *, recover: bool = True) -> None:
        from azure.servicebus.aio import ServiceBusClient

        self._stopping = False
        self._client = ServiceBusClient.from_connection_string(self._conn)
        self._sender = self._client.get_queue_sender(queue_name=self._queue_name)

        if self._consume:
            self._loop_task = asyncio.create_task(self._consume_loop(), name="cr-bus-consumer")
            if recover:
                # Rows left `running` by a worker that died are invisible to
                # Service Bus, whose message was completed or expired. Re-send
                # a nudge for each so they are not stranded.
                self._requeue_orphans()

    async def stop(self, *, grace_s: float = 30.0) -> None:
        self._stopping = True
        if self._loop_task is not None:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
            self._loop_task = None

        if self._running:
            # Let in-flight reviews finish: they have already been paid for.
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*self._running.values(), return_exceptions=True),
                    timeout=grace_s,
                )
        for task in list(self._running.values()):
            task.cancel()
        self._running.clear()

        if self._sender is not None:
            await self._sender.close()
            self._sender = None
        if self._client is not None:
            await self._client.close()
            self._client = None

    def _requeue_orphans(self) -> None:
        rows = store.pending_jobs()
        for row in rows:
            asyncio.get_running_loop().create_task(self._send(row.id, row.key, 0.0))
        if rows:
            log.info("re-sent %d orphaned job(s) to the queue", len(rows))

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
        """Persist the job, then nudge the queue.

        Synchronous like `JobQueue.submit`, because the webhook handler must
        return 202 without awaiting anything. The Postgres write is the part
        that matters — if the send fails, the row is still queued and the next
        message for that key, or a restart, will pick it up.
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
        if row_id is None:
            log.error("could not persist job %s %s; not sending", kind, key)
            return None

        # Fire-and-forget: `submit` is called from the ingress and must not
        # block it. The row is already durable, so a lost send is recoverable.
        with contextlib.suppress(RuntimeError):  # no running loop (sync tests)
            asyncio.get_running_loop().create_task(self._send(row_id, key, delay_s))
        return row_id

    async def _send(self, row_id: int, key: str, delay_s: float) -> None:
        from azure.servicebus import ServiceBusMessage

        if self._sender is None:
            log.error("no Service Bus sender; job %s stays queued for recovery", row_id)
            return
        for attempt in range(3):
            try:
                async with self._send_lock:
                    if self._sender is None or self._stopping:
                        log.warning("queue stopped; job %s stays queued for recovery", row_id)
                        return
                    # Build a fresh SDK message for each attempt: a failed send
                    # may already have transferred ownership of the prior one.
                    msg = ServiceBusMessage(
                        json.dumps({"row_id": row_id, "key": key}),
                        content_type="application/json",
                        # Postgres decides which job is current. The subject
                        # only helps inspect the queue in the portal.
                        subject=key[:128],
                    )
                    if delay_s > 0:
                        when = datetime.now(UTC) + timedelta(seconds=delay_s)
                        await self._sender.schedule_messages(msg, when)
                    else:
                        await self._sender.send_messages(msg)
                return
            except Exception as e:  # noqa: BLE001 - a duplicate nudge is safe
                if attempt == 2:
                    log.error("could not send job %s to Service Bus: %s", row_id, e)
                else:
                    await asyncio.sleep(2**attempt)

    # --- consumption --------------------------------------------------------

    async def _consume_loop(self) -> None:
        receiver = self._client.get_queue_receiver(
            queue_name=self._queue_name, max_wait_time=5, prefetch_count=self._concurrency
        )
        async with receiver:
            while not self._stopping:
                try:
                    batch = await receiver.receive_messages(
                        max_message_count=self._concurrency, max_wait_time=5
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 - a transient broker error is not fatal
                    log.warning("receive failed, retrying: %s", e)
                    await asyncio.sleep(2)
                    continue

                for msg in batch:
                    await self._sem.acquire()
                    task = asyncio.create_task(self._process(receiver, msg))
                    task.add_done_callback(lambda _t: self._sem.release())

    async def _process(self, receiver: Any, msg: Any) -> None:
        try:
            body = json.loads(str(msg))
            row_id = int(body["row_id"])
        except Exception as e:  # noqa: BLE001 - unparseable means nobody can ever act on it
            log.error("dead-lettering an unreadable message: %s", e)
            await receiver.dead_letter_message(msg, reason="unparseable")
            return

        row = store.claim_job(row_id)
        if row is None:
            # Superseded, already taken, or gone. Completing is correct: there
            # is nothing to retry, and leaving it would redeliver forever.
            await receiver.complete_message(msg)
            return

        job = QueuedJob(
            kind=row.kind,
            key=row.key,
            payload=dict(row.payload or {}),
            repo=row.repo,
            installation_id=row.installation_id,
            row_id=row.id,
        )
        renew = asyncio.create_task(self._renew(receiver, msg))
        self._running[job.key] = asyncio.current_task()  # type: ignore[assignment]
        try:
            await self._handler(job)
            store.set_job_status(job.row_id, "done")
            await receiver.complete_message(msg)
        except asyncio.CancelledError:
            store.set_job_status(job.row_id, "queued")
            with contextlib.suppress(Exception):
                await receiver.abandon_message(msg)
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("job %s failed: %s", job.key, e)
            store.set_job_status(job.row_id, "failed", error=str(e)[:2000])
            # Abandon rather than complete: Service Bus retries, and after
            # max delivery count it dead-letters, which is the audit trail.
            with contextlib.suppress(Exception):
                await receiver.abandon_message(msg)
        finally:
            renew.cancel()
            self._running.pop(job.key, None)

    async def _renew(self, receiver: Any, msg: Any) -> None:
        """Hold the lock while a review runs. Reviews outlast any sane lock."""
        while True:
            await asyncio.sleep(LOCK_RENEW_S)
            try:
                await receiver.renew_message_lock(msg)
            except Exception as e:  # noqa: BLE001
                log.warning("could not renew message lock: %s", e)
                return

    # --- introspection ------------------------------------------------------

    def depth(self) -> int:
        return len(store.pending_jobs())

    def active(self) -> int:
        return len(self._running)

    def snapshot(self) -> dict:
        return {
            "backend": "servicebus",
            "queue": self._queue_name,
            "waiting": self.depth(),
            "running": self.active(),
            "concurrency": self._concurrency,
            "consuming": self._consume,
        }

    async def drain(self, timeout: float = 120.0) -> bool:
        """Wait for in-flight work. Used by tests and by graceful shutdown."""
        deadline = asyncio.get_running_loop().time() + timeout
        while self._running and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        return not self._running
