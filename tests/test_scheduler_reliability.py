from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import scheduler as scheduler_module
from event_handler import EventHandler
from scheduler import (
    InMemoryScheduleStore,
    JsonScheduleStore,
    Schedule,
    ScheduleStatus,
    Scheduler,
)


def _past() -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=1)


class _FailFirstFiredSave(JsonScheduleStore):
    def __init__(self, path):
        self.fail_fired_once = True
        super().__init__(path)

    def save(self, schedule):
        if schedule.status == ScheduleStatus.FIRED and self.fail_fired_once:
            self.fail_fired_once = False
            raise OSError("simulated crash window")
        return super().save(schedule)


def test_crash_after_publish_before_fired_persist_retries_after_restart(tmp_path) -> None:
    path = tmp_path / "schedules.json"
    first_events = EventHandler(workers=0)
    first_store = _FailFirstFiredSave(path)
    first = Scheduler(first_events, store=first_store)
    schedule = first.schedule_at(_past(), task_id=7, payload={"wake": True})

    assert first.trigger_due() == 0
    first_event = first_events.queue.get_nowait()
    first.close()
    persisted_store = JsonScheduleStore(path)
    persisted = persisted_store.get(schedule.id)
    assert persisted is not None
    assert persisted.status == ScheduleStatus.RETRYING
    assert persisted.last_error is None
    persisted_store.close()

    restarted_events = EventHandler(workers=0)
    restarted = Scheduler(restarted_events, store=JsonScheduleStore(path))
    assert restarted.trigger_due() == 1
    restarted_event = restarted_events.queue.get_nowait()

    assert restarted_event.idempotency_key == first_event.idempotency_key
    assert restarted_event.idempotency_key == f"scheduler:{schedule.id}:v1"
    assert restarted.store.get(schedule.id).status == ScheduleStatus.FIRED
    restarted.close()


def test_same_process_retry_suppresses_duplicate_after_fired_save_failure(tmp_path) -> None:
    path = tmp_path / "schedules.json"
    events = EventHandler(workers=0)
    scheduler = Scheduler(events, store=_FailFirstFiredSave(path))
    scheduler.schedule_at(_past(), task_id=3, payload={"same": "content"})

    assert scheduler.trigger_due() == 0
    assert events.queue.qsize() == 1
    assert scheduler.trigger_due() == 1
    assert events.queue.qsize() == 1


def test_json_store_stale_concurrent_snapshot_cannot_overwrite_newer_state(tmp_path) -> None:
    path = tmp_path / "schedules.json"

    class DelayedFirstPersistStore(JsonScheduleStore):
        def __init__(self, store_path):
            self.first_snapshot_ready = threading.Event()
            self.release_first = threading.Event()
            super().__init__(store_path)

        def _flush(self, data=None, *, generation=None):
            if generation == 1:
                self.first_snapshot_ready.set()
                assert self.release_first.wait(timeout=1.0)
            return super()._flush(data, generation=generation)

    store = DelayedFirstPersistStore(path)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    first = Schedule(id="schedule-a", run_at=future, task_id=1)
    second = Schedule(id="schedule-b", run_at=future + timedelta(seconds=1), task_id=2)
    errors: list[BaseException] = []

    def save_first() -> None:
        try:
            store.save(first)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    thread = threading.Thread(target=save_first)
    thread.start()
    assert store.first_snapshot_ready.wait(timeout=1.0)

    # The second save captures [A, B] and persists it while the older [A]
    # snapshot is deliberately delayed before entering the persistence fence.
    store.save(second)
    store.release_first.set()
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert errors == []
    assert store._persisted_generation == 2

    store.close()
    restarted = JsonScheduleStore(path)
    assert {item.id for item in restarted.list()} == {"schedule-a", "schedule-b"}
    restarted.close()


def test_json_store_replace_failure_keeps_previous_snapshot_and_retry_is_durable(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "schedules.json"
    store = JsonScheduleStore(path)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    first = Schedule(id="schedule-a", run_at=future, task_id=1)
    second = Schedule(id="schedule-b", run_at=future + timedelta(seconds=1), task_id=2)
    store.save(first)
    before = path.read_bytes()
    real_replace = scheduler_module.os.replace

    def fail_replace(source, destination):
        raise OSError("simulated atomic replace failure")

    monkeypatch.setattr(scheduler_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated atomic replace failure"):
        store.save(second)

    assert path.read_bytes() == before
    assert list(tmp_path.glob("schedules.json.tmp-*")) == []

    monkeypatch.setattr(scheduler_module.os, "replace", real_replace)
    store.save(second)
    store.close()
    restarted = JsonScheduleStore(path)
    assert {item.id for item in restarted.list()} == {"schedule-a", "schedule-b"}
    restarted.close()


def test_publish_retry_exhaustion_is_persisted_as_failed() -> None:
    class FailingPublisher:
        def __init__(self) -> None:
            self.calls = 0

        def publish(self, *args, **kwargs):
            self.calls += 1
            raise RuntimeError("publisher unavailable")

    store = InMemoryScheduleStore()
    publisher = FailingPublisher()
    scheduler = Scheduler(publisher, store=store, max_publish_retries=2)
    schedule = scheduler.schedule_at(_past(), task_id=4)

    assert scheduler.trigger_due() == 0
    assert scheduler.trigger_due() == 0
    assert scheduler.trigger_due() == 0
    assert scheduler.trigger_due() == 0

    failed = store.get(schedule.id)
    assert failed is not None
    assert failed.status == ScheduleStatus.FAILED
    assert failed.publish_attempts == 3
    assert "publisher unavailable" in (failed.last_error or "")
    assert publisher.calls == 3


def test_scheduler_thread_survives_transient_store_and_publish_errors() -> None:
    class FlakyStore(InMemoryScheduleStore):
        def __init__(self) -> None:
            super().__init__()
            self.fail_list_once = True
            self.fail_retry_save_once = True

        def list(self, *, status=None):
            if self.fail_list_once:
                self.fail_list_once = False
                raise OSError("store temporarily unavailable")
            return super().list(status=status)

        def save(self, schedule):
            if (
                schedule.status == ScheduleStatus.RETRYING
                and self.fail_retry_save_once
            ):
                self.fail_retry_save_once = False
                raise OSError("store save temporarily unavailable")
            return super().save(schedule)

    class FlakyPublisher:
        def __init__(self) -> None:
            self.calls = 0
            self.accepted = threading.Event()

        def publish(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("publish temporarily unavailable")
            self.accepted.set()
            return object()

    store = FlakyStore()
    publisher = FlakyPublisher()
    scheduler = Scheduler(
        publisher,
        store=store,
        poll_interval=0.01,
        max_publish_retries=3,
    )
    schedule = scheduler.schedule_at(_past(), task_id=5)

    scheduler.start()
    try:
        assert publisher.accepted.wait(timeout=1.0)
        assert scheduler.running
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            current = store.get(schedule.id)
            if current is not None and current.status == ScheduleStatus.FIRED:
                break
            time.sleep(0.01)
        assert store.get(schedule.id).status == ScheduleStatus.FIRED
    finally:
        scheduler.stop()


def test_legacy_schedule_json_defaults_new_reliability_fields(tmp_path) -> None:
    path = tmp_path / "schedules.json"
    path.write_text(
        json.dumps(
            {
                "schedules": [
                    {
                        "id": "legacy",
                        "run_at": _past().isoformat(),
                        "task_id": 9,
                        "payload": {},
                        "priority": 20,
                        "status": "active",
                        "created_at": _past().isoformat(),
                        "fired_at": None,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    schedule = JsonScheduleStore(path).get("legacy")
    assert schedule is not None
    assert schedule.version == 1
    assert schedule.publish_attempts == 0
    assert schedule.last_error is None


def test_indexed_future_polls_do_not_scan_full_store() -> None:
    class CountingStore(InMemoryScheduleStore):
        def __init__(self) -> None:
            super().__init__()
            self.list_calls = 0
            self.due_queries = 0

        def list(self, *, status=None):
            self.list_calls += 1
            return super().list(status=status)

        def _list_due(self, now):
            self.due_queries += 1
            return super()._list_due(now)

    store = CountingStore()
    scheduler = Scheduler(EventHandler(workers=0), store=store, poll_interval=1.0)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    for task_id in range(1000):
        scheduler.schedule_at(future + timedelta(seconds=task_id), task_id=task_id)

    for _ in range(50):
        assert scheduler._poll_once() == 1.0

    assert store.list_calls == 0
    assert store.due_queries == 0


def test_reschedule_invalidates_old_index_deadline() -> None:
    store = InMemoryScheduleStore()
    events = EventHandler(workers=0)
    scheduler = Scheduler(events, store=store)
    base = datetime.now(timezone.utc)
    scheduled = scheduler.schedule_at(base + timedelta(seconds=10), task_id=12)

    moved = store.get(scheduled.id)
    assert moved is not None
    moved.run_at = base + timedelta(seconds=60)
    store.save(moved)

    assert scheduler.trigger_due(now=base + timedelta(seconds=20)) == 0
    assert events.queue.empty()
    assert scheduler.trigger_due(now=base + timedelta(seconds=61)) == 1
    event = events.queue.get_nowait()
    assert event.idempotency_key == f"scheduler:{scheduled.id}:v1"


def test_schedule_at_wakes_scheduler_after_empty_poll_without_sleep() -> None:
    class SignalingStore(InMemoryScheduleStore):
        def __init__(self) -> None:
            super().__init__()
            self.checked_empty = threading.Event()

        def _next_pending_at(self):
            result = super()._next_pending_at()
            if result is None:
                self.checked_empty.set()
            return result

    class RecordingPublisher:
        def __init__(self) -> None:
            self.accepted = threading.Event()

        def publish(self, *args, **kwargs):
            self.accepted.set()
            return object()

    store = SignalingStore()
    publisher = RecordingPublisher()
    scheduler = Scheduler(publisher, store=store, poll_interval=60.0)
    scheduler.start()
    try:
        assert store.checked_empty.wait(timeout=1.0)
        scheduler.schedule_at(_past(), task_id=13)
        assert publisher.accepted.wait(timeout=1.0)
    finally:
        scheduler.stop()


def test_cancel_racing_with_publish_linearizes_after_fire() -> None:
    class BlockingPublisher:
        def __init__(self) -> None:
            self.entered = threading.Event()
            self.release = threading.Event()

        def publish(self, *args, **kwargs):
            self.entered.set()
            assert self.release.wait(timeout=1.0)
            return object()

    publisher = BlockingPublisher()
    store = InMemoryScheduleStore()
    scheduler = Scheduler(publisher, store=store)
    scheduled = scheduler.schedule_at(_past(), task_id=14)
    trigger_thread = threading.Thread(target=scheduler.trigger_due)
    cancel_started = threading.Event()
    cancel_done = threading.Event()
    cancelled: list = []

    def cancel() -> None:
        cancel_started.set()
        cancelled.append(scheduler.cancel(scheduled.id))
        cancel_done.set()

    trigger_thread.start()
    assert publisher.entered.wait(timeout=1.0)
    cancel_thread = threading.Thread(target=cancel)
    cancel_thread.start()
    assert cancel_started.wait(timeout=1.0)
    assert not cancel_done.is_set()
    publisher.release.set()
    trigger_thread.join(timeout=1.0)
    cancel_thread.join(timeout=1.0)

    assert not trigger_thread.is_alive()
    assert not cancel_thread.is_alive()
    assert cancel_done.is_set()
    assert cancelled[0].status == ScheduleStatus.FIRED
    assert store.get(scheduled.id).status == ScheduleStatus.FIRED


def test_failed_durable_schedule_receipt_is_rearmed_with_same_identity(tmp_path) -> None:
    events = EventHandler(
        workers=0,
        durable_path=str(tmp_path / "durable-events.sqlite3"),
    )

    def fail(event):
        raise RuntimeError("runtime intake failed")

    events.register("schedule", fail)
    scheduler = Scheduler(events, store=InMemoryScheduleStore())
    scheduled = scheduler.schedule_at(_past(), task_id=21, payload={"wake": True})
    assert scheduler.trigger_due() == 1
    expected_key = f"scheduler:{scheduled.id}:v1"
    original_receipt = next(
        item
        for item in events._durable_store.list_events()
        if item.idempotency_key == expected_key
    )

    # Exhaust EventHandler's normal retry budget first. The scheduler recovery
    # path is specifically for a durable receipt that has become terminal
    # FAILED; changing max_attempts here would mask the real retry semantics.
    for _ in range(events.default_max_attempts):
        dispatched = events.dispatch_one()
        assert dispatched is not None
    assert any(
        item.idempotency_key == expected_key
        for item in events._durable_store.list_events(status="failed")
    )
    events.unregister("schedule", fail)
    delivered: list[str] = []
    events.register("schedule", lambda event: delivered.append(event.id))

    # FIRED schedules are normally absent from the pending heap. Recovery must
    # still find the poisoned durable receipt and requeue the exact same event.
    assert scheduler._poll_once() > 0
    assert events.queue.qsize() == 1
    replayed_receipt = next(
        item
        for item in events._durable_store.list_events(status="queued")
        if item.idempotency_key == expected_key
    )
    assert replayed_receipt.event_id == original_receipt.event_id
    events.dispatch_one()
    assert delivered == [original_receipt.event_id]
    assert scheduler.store.get(scheduled.id).status == ScheduleStatus.FIRED
    events.close()


def test_stop_without_wait_cannot_overlap_scheduler_restart() -> None:
    scheduler = Scheduler(EventHandler(workers=0), poll_interval=60.0)
    entered = threading.Event()
    release = threading.Event()

    def blocked_poll() -> float:
        entered.set()
        assert release.wait(timeout=2.0)
        return 60.0

    scheduler._poll_once = blocked_poll
    scheduler.start()
    assert entered.wait(timeout=1.0)
    original_thread = scheduler._thread
    assert original_thread is not None

    scheduler.stop(wait=False)
    scheduler.start()
    assert scheduler._thread is original_thread
    assert original_thread.is_alive()

    release.set()
    original_thread.join(timeout=1.0)
    assert not original_thread.is_alive()
    scheduler.stop(wait=False)


def test_json_schedule_store_is_single_writer_for_object_lifetime(tmp_path) -> None:
    path = tmp_path / "schedules.json"
    first = JsonScheduleStore(path)
    first.save(Schedule(id="owned", run_at=_past(), task_id=1))
    before = path.read_bytes()

    with pytest.raises(RuntimeError, match="déjà ouvert"):
        JsonScheduleStore(path)
    assert path.read_bytes() == before

    # stop/start belongs to Scheduler, not to the store lock lifetime. Closing
    # the owning store is the explicit writer handoff point.
    first.close()
    second = JsonScheduleStore(path)
    try:
        assert second.get("owned") is not None
    finally:
        second.close()


def test_scheduler_stop_start_keeps_json_writer_lock_until_close(tmp_path) -> None:
    path = tmp_path / "schedules.json"
    scheduler = Scheduler(
        EventHandler(workers=0),
        store=JsonScheduleStore(path),
        poll_interval=60.0,
    )
    scheduler.start()
    scheduler.stop()
    with pytest.raises(RuntimeError, match="déjà ouvert"):
        JsonScheduleStore(path)

    scheduler.start()
    assert scheduler.running
    scheduler.stop()
    scheduler.close()

    reopened = JsonScheduleStore(path)
    reopened.close()


def test_json_schedule_store_lock_is_released_when_process_dies(tmp_path) -> None:
    path = tmp_path / "schedules.json"
    code = (
        "import sys,time; "
        "from scheduler import JsonScheduleStore; "
        "s=JsonScheduleStore(sys.argv[1]); "
        "print('LOCKED', flush=True); time.sleep(60)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(path)],
        cwd=str(Path(__file__).resolve().parents[1]),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "LOCKED"
        with pytest.raises(RuntimeError, match="déjà ouvert"):
            JsonScheduleStore(path)
    finally:
        child.kill()
        child.wait(timeout=5)

    reopened = JsonScheduleStore(path)
    reopened.close()


def test_permanent_failed_schedule_delivery_stops_after_bounded_recovery(tmp_path) -> None:
    events = EventHandler(
        workers=0,
        default_max_attempts=1,
        durable_path=str(tmp_path / "durable-events.sqlite3"),
    )
    events.register(
        "schedule",
        lambda _event: (_ for _ in ()).throw(RuntimeError("permanent failure")),
    )
    scheduler = Scheduler(events, store=InMemoryScheduleStore())
    scheduled = scheduler.schedule_at(_past(), task_id=22)
    assert scheduler.trigger_due() == 1
    events.dispatch_one()

    for expected in range(1, scheduler._MAX_DOWNSTREAM_RECOVERIES + 1):
        scheduler._poll_once()
        current = scheduler.store.get(scheduled.id)
        assert current is not None
        assert current.delivery_recoveries == expected
        assert events.queue.qsize() == 1
        events.dispatch_one()

    scheduler._poll_once()
    current = scheduler.store.get(scheduled.id)
    assert current is not None
    assert current.status == ScheduleStatus.FAILED
    assert current.delivery_failed is True
    assert current.delivery_recoveries == 3
    assert events.queue.qsize() == 0
    scheduler._poll_once()
    assert events.queue.qsize() == 0
    events.close()
