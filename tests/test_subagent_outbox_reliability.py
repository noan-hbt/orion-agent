from __future__ import annotations

import json

import pytest

from event_handler import EventHandler
from subagents import (
    SubAgentJobStatus,
    SubAgentManager,
    _WaitingResult,
)


class _LLM:
    model = "stub/model"

    def tool_definitions(self):
        return []


def _manager(tmp_path, events):
    return SubAgentManager(
        _LLM(),
        events,
        state_path=tmp_path / "subagents.json",
        emit_progress_events=False,
    )


class _RejectingEvents:
    def __init__(self) -> None:
        self.calls = 0

    def publish(self, *args, **kwargs):
        self.calls += 1
        raise RuntimeError("event queue unavailable")


def test_waiting_notification_is_durable_and_replayed_after_restart(tmp_path) -> None:
    rejecting = _RejectingEvents()
    manager = _manager(tmp_path, rejecting)
    agent = manager.create_agent("worker", "waits for input")
    job = manager.submit("ask for missing input", agent_id=agent.id)
    manager._run_agent = lambda _agent, _job_id: _WaitingResult("need account id")

    manager._execute_job(job.id)

    stored = manager.get_job(job.id)
    assert stored is not None
    assert stored.status is SubAgentJobStatus.WAITING
    durable = json.loads((tmp_path / "subagents.json").read_text(encoding="utf-8"))
    waiting = next(item for item in durable["outbox"] if item["event_type"] == "subagent.waiting")
    assert waiting["published"] is False
    assert waiting["idempotency_key"] == f"subagent:{job.id}:v{stored.state_version}"
    assert rejecting.calls == 1

    manager.close()
    events = EventHandler(workers=0)
    restarted = _manager(tmp_path, events)
    replayed = events.queue.get_nowait()

    assert replayed.type == "subagent.waiting"
    assert replayed.payload["waiting_for"] == "need account id"
    assert replayed.idempotency_key == waiting["idempotency_key"]
    reloaded = restarted.get_job(job.id)
    assert reloaded is not None
    assert reloaded.status is SubAgentJobStatus.WAITING
    durable = json.loads((tmp_path / "subagents.json").read_text(encoding="utf-8"))
    waiting = next(item for item in durable["outbox"] if item["event_type"] == "subagent.waiting")
    assert waiting["published"] is True
    assert waiting["receipt_event_id"] == replayed.id


def test_publish_then_save_failure_replays_without_duplicate_event(tmp_path, monkeypatch) -> None:
    events = EventHandler(workers=0)
    manager = _manager(tmp_path, events)
    agent = manager.create_agent("worker", "finishes work")
    job = manager.submit("finish", agent_id=agent.id)
    with manager._lock:
        current = manager._jobs[job.id]
        current.status = SubAgentJobStatus.COMPLETED
        current.result = "done"
        current.state_version += 1
        manager._queue_outbox_locked(
            current, "subagent.completed", current.result, 20
        )
        manager._save_locked()
        key = next(
            key
            for key, item in manager._outbox.items()
            if item["event_type"] == "subagent.completed"
        )

    def fail_receipt_save() -> None:
        raise OSError("simulated crash after publish")

    monkeypatch.setattr(manager, "_save_locked", fail_receipt_save)
    manager._drain_outbox()

    assert events.queue.qsize() == 1
    assert manager._outbox[key]["published"] is False
    durable = json.loads((tmp_path / "subagents.json").read_text(encoding="utf-8"))
    assert next(item for item in durable["outbox"] if item["key"] == key)["published"] is False

    # Reconstructing the manager represents the process coming back after the
    # crash.  Reuse EventHandler here to exercise its idempotency boundary:
    # replay is accepted as the same event rather than queued twice.
    manager.close()
    restarted = _manager(tmp_path, events)

    assert events.queue.qsize() == 1
    queued = events.queue.get_nowait()
    assert queued.idempotency_key == f"subagent:{job.id}:v1"
    durable = json.loads((tmp_path / "subagents.json").read_text(encoding="utf-8"))
    delivered = next(item for item in durable["outbox"] if item["key"] == key)
    assert delivered["published"] is True
    assert delivered["receipt_event_id"] == queued.id
    assert restarted._outbox[key]["published"] is True


@pytest.mark.parametrize(
    ("status", "event_type"),
    [
        (SubAgentJobStatus.COMPLETED, "subagent.completed"),
        (SubAgentJobStatus.FAILED, "subagent.failed"),
        (SubAgentJobStatus.CANCELLED, "subagent.cancelled"),
    ],
)
def test_terminal_notifications_use_stable_job_version_idempotency_key(
    tmp_path, status, event_type
) -> None:
    events = EventHandler(workers=0)
    manager = _manager(tmp_path, events)
    agent = manager.create_agent("worker", "terminal state")
    job = manager.submit("work", agent_id=agent.id)

    with manager._lock:
        current = manager._jobs[job.id]
        current.status = status
        current.state_version += 1
        if status is SubAgentJobStatus.COMPLETED:
            current.result = "done"
        elif status is SubAgentJobStatus.FAILED:
            current.error = "failed"
        manager._queue_outbox_locked(current, event_type, "state changed", 20)
        manager._save_locked()
        version = current.state_version

    manager._drain_outbox()
    event = events.queue.get_nowait()

    expected = f"subagent:{job.id}:v{version}"
    assert event.idempotency_key == expected
    assert event.metadata["idempotency_key"] == expected
    durable = json.loads((tmp_path / "subagents.json").read_text(encoding="utf-8"))
    item = next(item for item in durable["outbox"] if item["event_type"] == event_type)
    assert item["idempotency_key"] == expected
    assert item["published"] is True
    assert item["receipt_event_id"] == event.id


def test_terminal_outbox_revives_failed_durable_eventhandler_receipt(tmp_path) -> None:
    events = EventHandler(
        workers=0,
        durable_path=str(tmp_path / "durable-events.sqlite3"),
    )
    failures: list[str] = []

    def fail_once(event):
        failures.append(event.id)
        raise RuntimeError("runtime intake failed after durable acceptance")

    events.register("subagent.completed", fail_once)
    manager = _manager(tmp_path, events)
    agent = manager.create_agent("worker", "finishes work")
    job = manager.submit("finish", agent_id=agent.id)
    with manager._lock:
        current = manager._jobs[job.id]
        current.status = SubAgentJobStatus.COMPLETED
        current.result = "done"
        current.state_version += 1
        manager._queue_outbox_locked(
            current, "subagent.completed", current.result, 20
        )
        snapshot = manager._prepare_save_locked()
        key = next(
            key
            for key, item in manager._outbox.items()
            if item["event_type"] == "subagent.completed"
        )
    manager._persist_snapshot(snapshot)
    manager._drain_outbox()

    stable_key = f"subagent:{job.id}:v1"
    original_receipt = next(
        item
        for item in events._durable_store.list_events()
        if item.idempotency_key == stable_key
    )
    events.dispatch_one()
    failed = next(
        item
        for item in events._durable_store.list_events(status="failed")
        if item.idempotency_key == stable_key
    )
    assert failed.event_id == original_receipt.event_id

    events.unregister("subagent.completed", fail_once)
    delivered: list[str] = []
    events.register("subagent.completed", lambda event: delivered.append(event.id))

    # Published producer state is not terminal delivery state. The next drain
    # re-arms the exact failed receipt and hydrates it under the same identity.
    manager._drain_outbox()
    assert events.queue.qsize() == 1
    replayed_receipt = next(
        item
        for item in events._durable_store.list_events(status="queued")
        if item.idempotency_key == stable_key
    )
    assert replayed_receipt.event_id == original_receipt.event_id
    assert manager._outbox[key]["downstream_recoveries"] == 1
    assert manager._outbox[key]["downstream_acked"] is False

    events.dispatch_one()
    manager._drain_outbox()
    assert delivered == [original_receipt.event_id]
    assert manager._outbox[key]["downstream_acked"] is True
    events.close()


def test_terminal_outbox_permanent_downstream_failure_is_bounded(tmp_path) -> None:
    events = EventHandler(
        workers=0,
        default_max_attempts=1,
        durable_path=str(tmp_path / "durable-events.sqlite3"),
    )
    events.register(
        "subagent.completed",
        lambda _event: (_ for _ in ()).throw(RuntimeError("permanent failure")),
    )
    manager = _manager(tmp_path, events)
    agent = manager.create_agent("worker", "finishes work")
    job = manager.submit("finish", agent_id=agent.id)
    with manager._lock:
        current = manager._jobs[job.id]
        current.status = SubAgentJobStatus.COMPLETED
        current.result = "done"
        current.state_version += 1
        manager._queue_outbox_locked(
            current, "subagent.completed", current.result, 20
        )
        snapshot = manager._prepare_save_locked()
        key = next(
            key
            for key, item in manager._outbox.items()
            if item["event_type"] == "subagent.completed"
        )
    manager._persist_snapshot(snapshot)
    manager._drain_outbox()
    events.dispatch_one()

    for expected in range(1, manager._MAX_DOWNSTREAM_RECOVERIES + 1):
        manager._drain_outbox()
        assert manager._outbox[key]["downstream_recoveries"] == expected
        assert events.queue.qsize() == 1
        events.dispatch_one()

    manager._drain_outbox()
    assert events.queue.qsize() == 0
    assert manager._outbox[key]["delivery_failed"] is True
    assert manager._outbox[key]["published"] is False
    assert manager._outbox[key]["downstream_recoveries"] == 3

    # Further maintenance passes are stable and never hot-loop the poison key.
    for _ in range(3):
        manager._drain_outbox()
    assert events.queue.qsize() == 0
    assert manager._outbox[key]["downstream_recoveries"] == 3
    events.close()
