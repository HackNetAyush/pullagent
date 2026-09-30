"""The cross-process queue.

The in-process queue is tested by its behaviour; this one is tested by its
*contracts with Postgres*, because that is where the multi-replica correctness
actually lives. Service Bus is faked — what matters is not that the SDK works
but that two workers handed the same message cannot both run the job, and that
a superseded job is dropped rather than executed.
"""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType

import pytest

from cr.store import db as store


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CR_CACHE_DIR", str(tmp_path / "cache"))
    store.reset_for_tests()
    store.init(f"sqlite:///{(tmp_path / 'q.db').as_posix()}")
    yield
    store.reset_for_tests()


def test_only_one_worker_can_claim_a_job(db):
    """Two replicas receive the same message. Exactly one may act on it."""
    row_id = store.enqueue_job("review", "review:me/repo#1", payload={"pr_number": 1})
    first = store.claim_job(row_id)
    second = store.claim_job(row_id)
    assert first is not None
    assert second is None


def test_a_burst_of_pushes_collapses_to_one_job(db):
    """Five pushes, one review. The older rows are closed at write time, so the
    messages they sent find nothing to do."""
    key = "review:me/repo#1"
    ids = [store.enqueue_job("review", key, payload={"n": i}) for i in range(5)]

    claimed = [i for i in ids if store.claim_job(i) is not None]
    assert len(claimed) == 1
    assert claimed[0] == ids[-1]  # the newest push wins


def test_superseded_job_is_no_longer_current(db):
    """The guard that stops a stale review posting on a commit nobody is
    looking at any more."""
    key = "review:me/repo#7"
    first = store.enqueue_job("review", key)
    assert store.claim_job(first) is not None
    assert store.job_is_current(first) is True

    store.enqueue_job("review", key)  # a newer push arrives mid-review
    assert store.job_is_current(first) is False


def test_missing_job_is_treated_as_current(db):
    """A check that cannot find its row must not silently swallow a review."""
    assert store.job_is_current(None) is True
    assert store.job_is_current(999_999) is True


def test_web_role_does_not_consume(monkeypatch):
    """An ingress replica that consumed would review on a box with no mirror,
    and would compete with the workers for the same messages."""
    from cr.app.busqueue import ServiceBusQueue

    async def handler(job):  # pragma: no cover - never invoked here
        raise AssertionError("web must not run jobs")

    q = ServiceBusQueue(
        handler, connection_string="Endpoint=sb://x/;SharedAccessKey=y", consume=False
    )
    assert q.snapshot()["consuming"] is False
    assert q.snapshot()["backend"] == "servicebus"


def test_settings_pick_the_backend(tmp_path, monkeypatch):
    from cr.app.jobs import JobQueue
    from cr.app.service import AppService
    from cr.config import Settings

    monkeypatch.setenv("CR_CACHE_DIR", str(tmp_path))
    plain = AppService(Settings(servicebus_connection=None))
    assert isinstance(plain.queue, JobQueue)

    bussed = AppService(
        Settings(
            servicebus_connection="Endpoint=sb://x/;SharedAccessKeyName=k;SharedAccessKey=v",
            app_role="worker",
        )
    )
    assert bussed.queue.snapshot()["backend"] == "servicebus"
    assert bussed.queue.snapshot()["consuming"] is True


@pytest.mark.asyncio
async def test_concurrent_sends_share_one_sender_safely(monkeypatch):
    from cr.app.busqueue import ServiceBusQueue

    azure = ModuleType("azure")
    servicebus = ModuleType("azure.servicebus")
    servicebus.ServiceBusMessage = lambda body, **kwargs: (body, kwargs)
    monkeypatch.setitem(sys.modules, "azure", azure)
    monkeypatch.setitem(sys.modules, "azure.servicebus", servicebus)

    class Sender:
        active = False
        sent = []

        async def send_messages(self, message):
            assert not self.active
            self.active = True
            await asyncio.sleep(0)
            self.sent.append(message)
            self.active = False

    async def handler(_job):
        pass

    queue = ServiceBusQueue(handler, connection_string="unused")
    queue._sender = Sender()
    await asyncio.gather(queue._send(1, "first", 0), queue._send(2, "second", 0))
    assert len(queue._sender.sent) == 2
