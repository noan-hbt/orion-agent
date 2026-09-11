from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from durable_events import DurableEventStore
from event_handler import DuplicateEventError, Event, EventHandler, EventQueueFullError
from orion_config import OrionConfig


def _durable_rows(path):
    store = DurableEventStore(path, namespace="event_handler")
    try:
        return store.list_events(limit=100)
    finally:
        store.close()


def test_publish_is_durable_before_ram_and_restart_replays(tmp_path, monkeypatch):
    path = tmp_path / "events.sqlite3"
    first = EventHandler(workers=0, durable_path=str(path))

    def fail_ram(*args, **kwargs):
        raise EventQueueFullError()

    monkeypatch.setattr(first, "_enqueue_ram_once", fail_ram)
    accepted = first.publish("message", {"text": "survive"}, message_id="msg-1")
    assert accepted.message_id == "msg-1"
    first.close()

    rows = _durable_rows(path)
    assert len(rows) == 1
    assert rows[0].status == "queued"

    seen = []
    restarted = EventHandler(workers=0, durable_path=str(path))
    restarted.register("message", seen.append)
    try:
        restarted.start()
        assert restarted.queue.qsize() == 1
        replayed = restarted.dispatch_one(timeout=0.1)
        assert replayed is not None
        assert replayed.payload == {"text": "survive"}
        assert [event.id for event in seen] == [replayed.id]
        assert _durable_rows(path)[0].status == "acked"
    finally:
        restarted.close()


def test_durable_ram_backpressure_is_accepted_and_delivered_once(tmp_path):
    path = tmp_path / "events.sqlite3"
    handler = EventHandler(workers=0, queue_size=1, durable_path=str(path))
    seen = []
    handler.register("message", lambda event: seen.append(event.payload["text"]))
    try:
        first = handler.publish("message", {"text": "first"})
        second = handler.publish("message", {"text": "second"})
        assert first.id != second.id
        assert handler.queue.qsize() == 1
        assert len(_durable_rows(path)) == 2

        assert handler.dispatch_one(timeout=0.1) is not None
        assert handler.dispatch_one(timeout=0.1) is not None
        assert seen == ["first", "second"]
        assert [row.status for row in _durable_rows(path)] == ["acked", "acked"]
    finally:
        handler.close()


def test_direct_enqueue_is_durable_before_ram(tmp_path):
    path = tmp_path / "events.sqlite3"
    first = EventHandler(workers=0, durable_path=str(path))
    event = Event("message", {"text": "direct"}, message_id="direct-1")
    accepted = first.enqueue(event)
    assert accepted.id == event.id
    first.close()

    restarted = EventHandler(workers=0, durable_path=str(path))
    seen = []
    restarted.register("message", seen.append)
    try:
        restarted.start()
        replayed = restarted.dispatch_one(timeout=0.1)
        assert replayed is not None
        assert replayed.id == event.id
        assert [item.id for item in seen] == [event.id]
    finally:
        restarted.close()


def test_durable_conflict_survives_restart(tmp_path):
    path = tmp_path / "events.sqlite3"
    first = EventHandler(workers=0, durable_path=str(path))
    first.publish("message", {"text": "first"}, message_id="msg-1")
    first.close()

    restarted = EventHandler(workers=0, durable_path=str(path))
    try:
        with pytest.raises(DuplicateEventError):
            restarted.publish("message", {"text": "changed"}, message_id="msg-1")
        assert len(_durable_rows(path)) == 1
    finally:
        restarted.close()


def test_restart_and_duplicate_publish_do_not_duplicate_ram_delivery(tmp_path):
    path = tmp_path / "events.sqlite3"
    first = EventHandler(workers=0, durable_path=str(path))
    original = first.publish("message", {"text": "same"}, message_id="msg-1")
    first.close()

    restarted = EventHandler(workers=0, durable_path=str(path))
    restarted.register("message", lambda event: None)
    try:
        restarted.start()
        assert restarted.queue.qsize() == 1
        duplicate = restarted.publish("message", {"text": "same"}, message_id="msg-1")
        assert duplicate.id == original.id
        assert restarted.queue.qsize() == 1

        restarted.dispatch_one(timeout=0.1)
        assert restarted.queue.qsize() == 0

        acknowledged_duplicate = restarted.publish(
            "message", {"text": "same"}, message_id="msg-1"
        )
        assert acknowledged_duplicate.id == original.id
        assert restarted.queue.qsize() == 0
        assert len(_durable_rows(path)) == 1
    finally:
        restarted.close()


def test_retry_then_terminal_failure_is_durable_and_dead_lettered(tmp_path):
    path = tmp_path / "events.sqlite3"
    handler = EventHandler(
        workers=0,
        durable_path=str(path),
        retry_delay=0,
        default_max_attempts=2,
    )
    calls = []

    def fail(event):
        calls.append(event.attempts)
        raise RuntimeError("boom")

    handler.register("message", fail)
    try:
        published = handler.publish("message", {"text": "retry"}, message_id="msg-1")
        first = handler.dispatch_one(timeout=0.1)
        assert first is not None
        assert handler.queue.qsize() == 1
        after_retry = _durable_rows(path)[0]
        assert after_retry.status == "queued"
        assert after_retry.attempts == 1
        assert handler.dead_letters == []

        second = handler.dispatch_one(timeout=0.1)
        assert second is not None
        terminal = _durable_rows(path)[0]
        assert terminal.status == "failed"
        assert terminal.attempts == 2
        assert terminal.last_error == "boom"
        assert [event.id for event in handler.dead_letters] == [published.id]
        assert calls == [1, 2]
    finally:
        handler.close()


def test_background_worker_claims_and_acks_durable_receipt(tmp_path):
    path = tmp_path / "events.sqlite3"
    handler = EventHandler(workers=1, durable_path=str(path), retry_delay=0)
    delivered = threading.Event()
    handler.register("message", lambda event: delivered.set())
    try:
        handler.start()
        handler.publish("message", {"text": "worker"}, message_id="msg-worker")
        assert delivered.wait(timeout=2.0)
        assert handler.wait_until_empty(timeout=2.0)

        row = _durable_rows(path)[0]
        assert row.status == "acked"
        assert row.attempts == 1
        assert row.fence_token == 1
        assert row.owner_id is None
        assert row.lease_until is None
    finally:
        handler.close()


def test_long_dispatch_renews_durable_lease_and_prevents_replay(tmp_path):
    path = tmp_path / "events.sqlite3"
    first = EventHandler(workers=1, durable_path=str(path), retry_delay=0)
    second = EventHandler(workers=0, durable_path=str(path), retry_delay=0)
    first._DURABLE_LEASE_SECONDS = 0.15
    second._DURABLE_LEASE_SECONDS = 0.15
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def blocking(event):
        calls.append(("first", event.id))
        entered.set()
        assert release.wait(timeout=2.0)

    first.register("message", blocking)
    second.register("message", lambda event: calls.append(("second", event.id)))
    try:
        first.start()
        published = first.publish("message", {"text": "slow"}, message_id="slow-1")
        assert entered.wait(timeout=1.0)
        time.sleep(0.3)

        assert second.dispatch_one(timeout=0) is None
        rows = _durable_rows(path)
        assert len(rows) == 1
        assert rows[0].status == "processing"
        assert rows[0].event_id == published.id
        assert calls == [("first", published.id)]

        release.set()
        assert first.wait_until_empty(timeout=1.0)
        assert _durable_rows(path)[0].status == "acked"
        assert second.dispatch_one(timeout=0) is None
        assert calls == [("first", published.id)]
    finally:
        release.set()
        first.close()
        second.close()


def test_lost_heartbeat_never_acks_with_stale_claim(tmp_path, monkeypatch):
    path = tmp_path / "events.sqlite3"
    handler = EventHandler(workers=0, durable_path=str(path), retry_delay=0)
    handler._DURABLE_LEASE_SECONDS = 0.06
    handler.register("message", lambda event: time.sleep(0.08))
    handler.publish("message", {"text": "uncertain"}, message_id="lost-heartbeat")

    store = handler._durable_store
    assert store is not None
    monkeypatch.setattr(store, "renew_claim", lambda *args, **kwargs: False)
    try:
        handler.dispatch_one(timeout=0)
        row = _durable_rows(path)[0]
        assert row.status == "processing"
        assert row.owner_id is not None
    finally:
        handler.close()


def test_durable_duplicate_ignores_volatile_received_at_metadata(tmp_path):
    path = tmp_path / "events.sqlite3"
    first = EventHandler(workers=0, durable_path=str(path))
    try:
        original = first.publish(
            "message",
            {"text": "same"},
            source="telegram",
            metadata={"channel": "telegram", "received_at": "2026-09-11T08:00:00+00:00"},
            message_id="msg-stable",
        )
    finally:
        first.close()

    restarted = EventHandler(workers=0, durable_path=str(path))
    try:
        duplicate = restarted.publish(
            "message",
            {"text": "same"},
            source="telegram",
            metadata={"channel": "telegram", "received_at": "2026-09-11T08:01:00+00:00"},
            message_id="msg-stable",
        )
        assert duplicate.id == original.id
        assert len(_durable_rows(path)) == 1
    finally:
        restarted.close()


def test_stop_wait_false_does_not_overlap_worker_generation(tmp_path):
    path = tmp_path / "events.sqlite3"
    handler = EventHandler(workers=1, durable_path=str(path), retry_delay=0)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def blocking(event):
        calls.append(event.payload["text"])
        if event.payload["text"] == "first":
            entered.set()
            assert release.wait(timeout=2.0)

    handler.register("message", blocking)
    try:
        handler.start()
        handler.publish("message", {"text": "first"})
        assert entered.wait(timeout=1.0)
        old_thread = handler._threads[0]

        handler.stop(wait=False, drain=False)
        assert old_thread.is_alive()
        assert handler._threads == [old_thread]
        restarted = threading.Event()

        def restart_handler():
            handler.start()
            restarted.set()

        starter = threading.Thread(target=restart_handler)
        starter.start()
        time.sleep(0.05)
        assert not restarted.is_set()
        assert handler._threads == [old_thread]

        handler.publish("message", {"text": "second"})
        assert calls == ["first"]
        release.set()
        starter.join(timeout=1.0)
        assert restarted.is_set()
        assert not old_thread.is_alive()
        assert handler.wait_until_empty(timeout=1.0)
        assert calls == ["first", "second"]
    finally:
        release.set()
        handler.close()


def test_close_waits_for_cancelled_inflight_worker_before_closing_store(tmp_path):
    path = tmp_path / "events.sqlite3"
    handler = EventHandler(workers=1, durable_path=str(path), retry_delay=0)
    entered = threading.Event()
    release = threading.Event()

    def blocking(_event):
        entered.set()
        assert release.wait(timeout=2.0)

    handler.register("message", blocking)
    handler.start()
    published = handler.publish("message", {"text": "slow-close"})
    assert entered.wait(timeout=1.0)
    handler.cancel()

    closed = threading.Event()
    close_errors = []

    def close_handler():
        try:
            handler.close()
        except Exception as exc:  # pragma: no cover - asserted below
            close_errors.append(exc)
        finally:
            closed.set()

    closer = threading.Thread(target=close_handler)
    closer.start()
    time.sleep(0.05)
    assert not closed.is_set()
    release.set()
    closer.join(timeout=1.0)
    assert closed.is_set()
    assert close_errors == []

    store = DurableEventStore(path, namespace="event_handler")
    try:
        row = store.list_events(limit=10)[0]
        assert row.event_id == published.id
        assert row.status == "acked"
    finally:
        store.close()


def test_start_explicitly_recovers_stale_processing_claim(tmp_path):
    path = tmp_path / "events.sqlite3"
    first = EventHandler(workers=0, durable_path=str(path))
    first.publish("message", {"text": "stale"}, message_id="msg-1")
    receipt = first._durable_store.list_events(status="queued")[0]
    claim = first._durable_store.claim_receipt(
        receipt.receipt_id, owner_id="crashed-owner", lease_seconds=60
    )
    assert claim is not None
    first.close()

    db = sqlite3.connect(path)
    try:
        db.execute(
            "UPDATE durable_events SET lease_until=0 WHERE receipt_id=?",
            (receipt.receipt_id,),
        )
        db.commit()
    finally:
        db.close()

    restarted = EventHandler(workers=0, durable_path=str(path))
    restarted.register("message", lambda event: None)
    try:
        restarted.start()
        recovered = restarted._durable_store.get(receipt.receipt_id)
        assert recovered is not None
        assert recovered.status == "queued"
        restarted.dispatch_one(timeout=0.1)
        assert restarted._durable_store.get(receipt.receipt_id).status == "acked"
    finally:
        restarted.close()


def test_event_config_durable_path_is_opt_in_and_build_is_relative(tmp_path, monkeypatch):
    assert OrionConfig().events.durable_path is None
    assert OrionConfig().runtime.durable_path is None

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    (tmp_path / "ORION_CORE.md").write_text("core", encoding="utf-8")
    (tmp_path / "tools").mkdir()
    config = OrionConfig.from_mapping(
        {
            "events": {"durable_path": "data/events.sqlite3"},
            "runtime": {"durable_path": "data/events.sqlite3"},
            "reflection": {"enabled": False},
            "context": {"reflection_enabled": False},
            "subagents": {"enabled": False},
            "scheduler": {"enabled": False},
            "memory": {"enabled": False},
            "tools": {"directory": "tools"},
        }
    )
    config.config_path = tmp_path / "orion.toml"

    app = config.build()
    try:
        assert app.events.durable_path == str(tmp_path / "data" / "events.sqlite3")
        assert app.events._durable_store is not None
        assert app.runtime.durable_path == str(tmp_path / "data" / "events.sqlite3")
        assert app.runtime._durable_store is not None
        assert app.events._durable_store.namespace == "event_handler"
        assert app.runtime._durable_store.namespace == "runtime"
    finally:
        app.stop()
