from __future__ import annotations

import threading
import time

import pytest

from durable_events import DurableEventConflict, DurableEventStore
from event_handler import Event
from runtime import AgentRuntime, RunContext, RuntimeState
from scheduler import JsonScheduleStore, Scheduler
from tasks import InMemoryTaskStore, JsonTaskStore, RunStatus, TaskStatus


def _runtime(*, durable_path=None, durable_store=None, **kwargs):
    return AgentRuntime(
        durable_path=durable_path,
        durable_store=durable_store,
        action_ledger_path=":memory:",
        history_enabled=False,
        **kwargs,
    )


class _AnswerClient:
    def __init__(self, *, entered=None, release=None):
        self.calls = 0
        self.entered = entered
        self.release = release

    def tool_definitions(self):
        return []

    def complete(self, messages, *, tools=None, **kwargs):
        self.calls += 1
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            assert self.release.wait(timeout=2.0)
        return {"choices": [{"message": {"role": "assistant", "content": "done"}}]}


def test_crash_after_durable_accept_is_replayed_after_restart(tmp_path):
    path = tmp_path / "runtime-events.sqlite3"
    first = _runtime(durable_path=str(path))
    event = Event(
        "message",
        {"text": "survive restart"},
        idempotency_key="restart-1",
    )

    first.receive_event(event)
    queued = first._durable_store.list_events(status="queued")
    assert len(queued) == 1
    assert queued[0].event_id == event.id

    # Simulate process death: RAM queue is lost, durable queued receipt remains.
    first._durable_store.close()
    restarted = _runtime(durable_path=str(path))
    try:
        processed = restarted.process_one(timeout=0)

        assert processed is not None
        assert processed.id == event.id
        receipt = restarted._durable_store.list_events()[0]
        assert receipt.status == "acked"
        assert receipt.attempts == 1
    finally:
        restarted._durable_store.close()


def test_durable_accept_survives_ram_enqueue_failure(tmp_path):
    path = tmp_path / "runtime-events.sqlite3"
    first = _runtime(
        durable_path=str(path),
        wake_queue_size=1,
        max_deferred_events=1,
    )
    # Simulate the exact crash window after durable acceptance by making both
    # in-memory destinations unavailable. receive_event must persist first.
    first.wake_queue.put_nowait(Event("local", {"slot": "wake"}))
    first._deferred_events.put_nowait(Event("local", {"slot": "deferred"}))
    event = Event("message", {"text": "persist before RAM"}, idempotency_key="ram-full")

    with pytest.raises(RuntimeError, match="file différée"):
        first.receive_event(event)

    queued = first._durable_store.list_events(status="queued")
    assert len(queued) == 1
    assert queued[0].event_id == event.id
    first._durable_store.close()

    restarted = _runtime(durable_path=str(path))
    try:
        processed = restarted.process_one(timeout=0)
        assert processed is not None
        assert processed.id == event.id
        assert restarted._durable_store.list_events()[0].status == "acked"
    finally:
        restarted._durable_store.close()


def test_process_one_claims_fenced_before_wake_and_acks_after_finalization(tmp_path):
    path = tmp_path / "runtime-events.sqlite3"
    seen_statuses = []
    runtime = None

    def on_state_change(_old, new, event):
        if new is RuntimeState.WAKE and event is not None:
            receipt_id = runtime._durable_receipts_by_event_id[event.id]
            seen_statuses.append(runtime._durable_store.get(receipt_id).status)

    runtime = _runtime(durable_path=str(path), on_state_change=on_state_change)
    event = Event("message", {"text": "claim first"})
    try:
        runtime.receive_event(event)
        processed = runtime.process_one(timeout=0)

        assert processed is not None
        assert seen_statuses == ["processing"]
        receipt_id = runtime._durable_receipts_by_event_id[event.id]
        receipt = runtime._durable_store.get(receipt_id)
        assert receipt.status == "acked"
        assert receipt.fence_token == 1
    finally:
        runtime._durable_store.close()


def test_drain_stop_processes_admitted_events_instead_of_skipping_then_acking(tmp_path):
    path = tmp_path / "runtime-events.sqlite3"
    client = _AnswerClient()
    outputs = []
    runtime = None
    requested = {"done": False}

    def on_state_change(_old, new, _event):
        if new is RuntimeState.WAKE and not requested["done"]:
            requested["done"] = True
            runtime.stop(wait=False, drain=True)

    runtime = _runtime(
        durable_path=str(path),
        llm_client=client,
        on_output=outputs.append,
        on_state_change=on_state_change,
    )
    runtime.receive_event(Event("message", {"text": "first"}, idempotency_key="drain-1"))
    runtime.receive_event(Event("message", {"text": "second"}, idempotency_key="drain-2"))
    try:
        runtime.start()
        thread = runtime._thread
        assert thread is not None
        thread.join(timeout=2.0)
        assert not thread.is_alive()

        assert client.calls == 2
        assert [item.content for item in outputs] == ["done", "done"]
        receipts = runtime._durable_store.list_events()
        assert len(receipts) == 2
        assert all(item.status == "acked" for item in receipts)
    finally:
        runtime.stop(wait=True, drain=False)
        runtime._durable_store.close()


def test_non_draining_stop_during_wake_requeues_instead_of_acking_skipped_work(tmp_path):
    path = tmp_path / "runtime-events.sqlite3"
    client = _AnswerClient()
    runtime = None
    requested = {"done": False}

    def on_state_change(_old, new, _event):
        if new is RuntimeState.WAKE and not requested["done"]:
            requested["done"] = True
            runtime.stop(wait=False, drain=False)

    runtime = _runtime(
        durable_path=str(path),
        llm_client=client,
        on_state_change=on_state_change,
    )
    event = Event("message", {"text": "preserve me"}, idempotency_key="stop-no-drain")
    runtime.receive_event(event)
    try:
        assert runtime.process_one(timeout=0) is None
        assert client.calls == 0
        receipt = runtime._durable_store.list_events()[0]
        assert receipt.status == "queued"
        assert receipt.last_error == "runtime stopped before event was consumed"
    finally:
        runtime._durable_store.close()


def test_long_wake_heartbeats_receipt_so_second_runtime_cannot_recover_live_work(tmp_path):
    path = tmp_path / "runtime-events.sqlite3"
    entered = threading.Event()
    release = threading.Event()
    client = _AnswerClient(entered=entered, release=release)
    first_store = DurableEventStore(path, namespace="runtime")
    second_store = DurableEventStore(path, namespace="runtime")
    first = _runtime(durable_store=first_store, llm_client=client)
    second = _runtime(durable_store=second_store)
    first._DURABLE_LEASE_SECONDS = 0.12
    second._DURABLE_LEASE_SECONDS = 0.12
    event = Event("message", {"text": "long run"}, idempotency_key="heartbeat-live")
    first.receive_event(event)
    result = []

    worker = threading.Thread(target=lambda: result.append(first.process_one(timeout=0)))
    worker.start()
    try:
        assert entered.wait(timeout=1.0)
        time.sleep(0.28)

        receipt_id = first._durable_receipts_by_event_id[event.id]
        receipt = second_store.get(receipt_id)
        assert receipt is not None and receipt.status == "processing"
        assert receipt.lease_until is not None and receipt.lease_until > time.time()
        assert second._recover_durable_inbox() is None
        assert second._hydrate_durable_inbox() == 0
        assert second.wake_queue.empty()

        release.set()
        worker.join(timeout=2.0)
        assert not worker.is_alive()
        assert result == [event]
        assert second_store.get(receipt_id).status == "acked"
    finally:
        release.set()
        worker.join(timeout=2.0)
        first_store.close()
        second_store.close()


def test_live_processing_lease_is_not_replayed_concurrently(tmp_path):
    path = tmp_path / "runtime-events.sqlite3"
    now = [100.0]
    first_store = DurableEventStore(path, namespace="runtime", clock=lambda: now[0])
    second_store = DurableEventStore(path, namespace="runtime", clock=lambda: now[0])
    first = _runtime(durable_store=first_store)
    second = _runtime(durable_store=second_store)
    event = Event("message", {"text": "single owner"}, idempotency_key="lease-live")
    try:
        first.receive_event(event)
        receipt_id = first._durable_receipts_by_event_id[event.id]
        claim = first_store.claim_receipt(
            receipt_id, owner_id="first-owner", lease_seconds=20
        )
        assert claim is not None

        # Duplicate delivery converges on the same processing receipt. The
        # second runtime must neither queue nor execute it while the lease lives.
        second.receive_event(
            Event(
                "message",
                {"text": "single owner"},
                idempotency_key="lease-live",
            )
        )
        assert second.process_one(timeout=0) is None
        assert second.wake_count == 0
        assert second_store.get(receipt_id).status == "processing"
        assert second_store.get(receipt_id).fence_token == claim.fence_token
    finally:
        first_store.close()
        second_store.close()


def test_stale_processing_is_explicitly_recovered_and_old_fence_cannot_ack(tmp_path):
    path = tmp_path / "runtime-events.sqlite3"
    now = [50.0]
    first_store = DurableEventStore(path, namespace="runtime", clock=lambda: now[0])
    first = _runtime(durable_store=first_store)
    event = Event("message", {"text": "recover me"}, idempotency_key="stale-1")
    first.receive_event(event)
    receipt_id = first._durable_receipts_by_event_id[event.id]
    old_claim = first_store.claim_receipt(
        receipt_id, owner_id="crashed-owner", lease_seconds=5
    )
    assert old_claim is not None

    now[0] = 56.0
    second_store = DurableEventStore(path, namespace="runtime", clock=lambda: now[0])
    restarted = _runtime(durable_store=second_store)
    try:
        processed = restarted.process_one(timeout=0)

        assert processed is not None
        receipt = second_store.get(receipt_id)
        assert receipt.status == "acked"
        assert receipt.attempts == 2
        assert receipt.fence_token == old_claim.fence_token + 1
        assert first_store.ack(
            receipt_id,
            owner_id="crashed-owner",
            fence_token=old_claim.fence_token,
        ) is False
    finally:
        first_store.close()
        second_store.close()


def test_durable_idempotency_reuses_receipt_and_conflicting_payload_is_rejected(tmp_path):
    runtime = _runtime(durable_path=str(tmp_path / "runtime-events.sqlite3"))
    try:
        first = Event(
            "message",
            {"text": "same"},
            idempotency_key="request-42",
        )
        runtime.receive_event(first)
        runtime.receive_event(
            Event(
                "message",
                {"text": "same"},
                idempotency_key="request-42",
            )
        )

        assert runtime.wake_queue.qsize() == 1
        assert len(runtime._durable_store.list_events()) == 1

        with pytest.raises(DurableEventConflict):
            runtime.receive_event(
                Event(
                    "message",
                    {"text": "different"},
                    idempotency_key="request-42",
                )
            )
    finally:
        runtime._durable_store.close()


def test_durable_event_still_resumes_exact_waiting_task(tmp_path):
    tasks = InMemoryTaskStore()
    task = tasks.create("wait durably")
    task.wait_for(
        event_type="webhook",
        payload_equals={"job_id": "job-1"},
        description="exact durable wake",
    )
    tasks.save(task)
    runtime = _runtime(
        durable_path=str(tmp_path / "runtime-events.sqlite3"), task_store=tasks
    )
    try:
        runtime.receive_event(Event("webhook", {"job_id": "job-1"}))
        processed = runtime.process_one(timeout=0)

        assert processed is not None
        saved = tasks.get(task.id)
        assert saved is not None
        assert saved.status is TaskStatus.RUNNING
    finally:
        runtime._durable_store.close()


def test_replay_rebinds_task_if_crash_happened_after_wait_resume_was_persisted(tmp_path):
    event_path = tmp_path / "runtime-events.sqlite3"
    task_path = tmp_path / "tasks.json"
    tasks = JsonTaskStore(task_path)
    task = tasks.create("resume exactly once after crash")
    task.wait_for(event_type="webhook", payload_equals={"job_id": "job-1"})
    tasks.save(task)
    first = _runtime(durable_path=str(event_path), task_store=tasks)
    event = Event("webhook", {"job_id": "job-1"}, idempotency_key="wait-resume-crash")
    first.receive_event(event)

    # Exact crash window: durable receipt is still queued, while the matched
    # task transition has already reached disk and therefore no longer matches
    # find_waiting_task on restart.
    resumed = tasks.find_waiting_task(event)
    assert resumed is not None
    resumed.resume_from_wait(event.id)
    tasks.save(resumed)
    first._durable_store.close()

    restarted_tasks = JsonTaskStore(task_path)
    restarted = _runtime(durable_path=str(event_path), task_store=restarted_tasks)
    try:
        processed = restarted.process_one(timeout=0)
        assert processed is not None and processed.id == event.id
        assert restarted.wake_context is not None
        assert restarted.wake_context.task_id == task.id
        saved = restarted_tasks.get(task.id)
        assert saved is not None and saved.status is TaskStatus.RUNNING
        assert any(run.event_id == event.id for run in saved.runs)
        assert restarted._durable_store.list_events()[0].status == "acked"
    finally:
        restarted._durable_store.close()


def test_restart_queues_stable_recovery_for_preempted_paused_task_without_ram_context(tmp_path):
    event_path = tmp_path / "runtime-events.sqlite3"
    task_path = tmp_path / "tasks.json"
    tasks = JsonTaskStore(task_path)
    task = tasks.create("survive preemption restart", priority=30)
    original = Event("message", {"text": "long work"}, priority=30)
    run = task.start_run(original.id)
    task.pause(run_id=run.id, reason="urgent preemption", interrupted_by="urgent-event")
    tasks.save(task)

    restarted_tasks = JsonTaskStore(task_path)
    restarted = _runtime(durable_path=str(event_path), task_store=restarted_tasks)
    try:
        processed = restarted.process_one(timeout=0)
        assert processed is not None
        assert processed.type == restarted._RECOVER_PAUSED_EVENT
        assert processed.id == f"recover-paused:{task.id}:{run.id}"
        saved = restarted_tasks.get(task.id)
        assert saved is not None and saved.status is TaskStatus.RUNNING
        restored_run = next(item for item in saved.runs if item.id == run.id)
        assert restored_run.status is RunStatus.RUNNING
        assert any(
            item.get("event") == "task_resumed" and item.get("event_id") == processed.id
            for item in saved.history
        )
        receipt = restarted._durable_store.list_events()[0]
        assert receipt.status == "acked"
        assert receipt.idempotency_key == processed.id
    finally:
        restarted._durable_store.close()


def test_taskless_subagent_completed_shortcut_is_preserved_with_durable_inbox(tmp_path):
    outputs = []
    runtime = _runtime(
        durable_path=str(tmp_path / "runtime-events.sqlite3"),
        on_output=outputs.append,
    )
    try:
        runtime.receive_event(
            Event(
                "subagent.completed",
                {"job_id": "job-1", "status": "completed", "result": "direct durable result"},
            )
        )

        processed = runtime.process_one(timeout=0)

        assert processed is not None
        assert outputs[-1].content == "direct durable result"
        assert runtime._durable_store.list_events()[0].status == "acked"
    finally:
        runtime._durable_store.close()


def test_acknowledge_pending_event_durably_acks_deferred_receipt_and_restart_does_not_replay(tmp_path):
    path = tmp_path / "runtime-events.sqlite3"
    runtime = _runtime(durable_path=str(path))
    current = Event("message", {"text": "current run"})
    deferred = Event("message", {"text": "handled inline"}, idempotency_key="inline-1")
    runtime._run_context = RunContext(
        event=current,
        task=None,
        run_id="run-current",
        loaded_state={},
    )
    runtime._run_in_progress = True
    runtime.receive_event(deferred)
    receipt_id = runtime._durable_receipts_by_event_id[deferred.id]

    result = runtime._execute_runtime_tool(
        "acknowledge_pending_event",
        {"event_id": deferred.id, "reason": "handled in current run"},
    )

    assert result["acknowledged"] is True
    assert runtime._durable_store.get(receipt_id).status == "acked"
    assert deferred.id not in runtime._deferred_event_index
    assert deferred.id not in runtime._durable_ram_event_ids
    assert deferred.id not in runtime._durable_receipts_by_event_id
    runtime._durable_store.close()

    restarted = _runtime(durable_path=str(path))
    try:
        assert restarted.process_one(timeout=0) is None
        assert restarted.wake_queue.empty()
        assert restarted._deferred_events.empty()
        receipt = restarted._durable_store.get(receipt_id)
        assert receipt is not None and receipt.status == "acked"
    finally:
        restarted._durable_store.close()


def test_replay_rebinds_task_with_existing_run_for_same_event(tmp_path):
    event_path = tmp_path / "runtime-events.sqlite3"
    task_path = tmp_path / "tasks.json"
    tasks = JsonTaskStore(task_path)
    first = _runtime(durable_path=str(event_path), task_store=tasks)
    event = Event("message", {"text": "bind me"}, id="event-bind-existing-run")
    first.receive_event(event)

    # Fault injection: simulate a crash after create/bind/start_run reached disk
    # but before the runtime receipt was ACKed.
    task = tasks.create("durably bound task")
    task.start_run(event.id)
    tasks.save(task)
    first._durable_store.close()

    restarted_tasks = JsonTaskStore(task_path)
    restarted = _runtime(durable_path=str(event_path), task_store=restarted_tasks)
    try:
        processed = restarted.process_one(timeout=0)
        assert processed is not None and processed.id == event.id
        assert restarted.wake_context is not None
        assert restarted.wake_context.task_id == task.id
        saved = restarted_tasks.get(task.id)
        assert saved is not None
        assert len([run for run in saved.runs if run.event_id == event.id]) == 1
        assert restarted._durable_store.list_events()[0].status == "acked"
    finally:
        restarted._durable_store.close()


def test_paused_recovery_replay_rebinds_after_second_crash(tmp_path):
    event_path = tmp_path / "runtime-events.sqlite3"
    task_path = tmp_path / "tasks.json"
    tasks = JsonTaskStore(task_path)
    task = tasks.create("recover twice", priority=30)
    original = Event("message", {"text": "long work"}, priority=30)
    run = task.start_run(original.id)
    task.pause(run_id=run.id, reason="preempted", interrupted_by="urgent")
    tasks.save(task)

    first = _runtime(durable_path=str(event_path), task_store=tasks)
    first._recover_orphaned_paused_tasks()
    recovery = first.wake_queue.get_nowait()
    first.wake_queue.task_done()
    with first._execution_lock:
        first._queued_event_ids.discard(recovery.id)

    # Fault injection: crash after the recovery mutation is durable, before its
    # receipt gets claimed/ACKed.
    restored = first._restore_orphaned_paused_context(recovery)
    assert restored is not None
    first._durable_store.close()

    restarted_tasks = JsonTaskStore(task_path)
    restarted = _runtime(durable_path=str(event_path), task_store=restarted_tasks)
    try:
        processed = restarted.process_one(timeout=0)
        assert processed is not None and processed.id == recovery.id
        saved = restarted_tasks.get(task.id)
        assert saved is not None and saved.status is TaskStatus.RUNNING
        assert restarted.wake_context is not None
        assert restarted.wake_context.task_id == task.id
        assert restarted._durable_store.get(
            restarted._durable_receipts_by_event_id[recovery.id]
        ).status == "acked"
    finally:
        restarted._durable_store.close()


def test_deferred_ack_stays_processing_until_parent_receipt_commits(tmp_path, monkeypatch):
    event_path = tmp_path / "runtime-events.sqlite3"
    runtime = _runtime(durable_path=str(event_path))
    parent = Event("message", {"text": "parent"}, id="parent-event")
    child = Event("message", {"text": "child"}, id="child-event")
    runtime.receive_event(parent)

    original_wake = runtime._wake
    seen = {}

    def wake_with_inline_ack(event):
        if event.id != parent.id:
            return original_wake(event)
        runtime._run_context = RunContext(event=event, task=None, run_id=None, loaded_state={})
        runtime._run_in_progress = True
        runtime.receive_event(child)
        child_receipt_id = runtime._durable_receipts_by_event_id[child.id]
        result = runtime._execute_runtime_tool(
            "acknowledge_pending_event",
            {"event_id": child.id, "reason": "handled inline"},
        )
        seen["result"] = result
        seen["child_receipt_id"] = child_receipt_id
        seen["status_during_parent"] = runtime._durable_store.get(child_receipt_id).status
        runtime._run_in_progress = False
        runtime._promote_deferred_events()
        return True

    monkeypatch.setattr(runtime, "_wake", wake_with_inline_ack)
    try:
        processed = runtime.process_one(timeout=0)
        assert processed is not None and processed.id == parent.id
        assert seen["result"]["acknowledged"] is True
        assert seen["result"]["pending_parent_commit"] is True
        assert seen["status_during_parent"] == "processing"
        assert runtime._durable_store.get(seen["child_receipt_id"]).status == "acked"
    finally:
        runtime._durable_store.close()


def test_deferred_ack_rolls_child_back_if_parent_does_not_commit(tmp_path, monkeypatch):
    event_path = tmp_path / "runtime-events.sqlite3"
    runtime = _runtime(durable_path=str(event_path))
    parent = Event("message", {"text": "parent"}, id="parent-stop")
    child = Event("message", {"text": "child"}, id="child-replay")
    runtime.receive_event(parent)
    child_receipt_id = None
    original_wake = runtime._wake
    injected = False

    def interrupted_parent(event):
        nonlocal child_receipt_id, injected
        if injected:
            return original_wake(event)
        injected = True
        runtime._run_context = RunContext(event=event, task=None, run_id=None, loaded_state={})
        runtime._run_in_progress = True
        runtime.receive_event(child)
        child_receipt_id = runtime._durable_receipts_by_event_id[child.id]
        result = runtime._execute_runtime_tool(
            "acknowledge_pending_event", {"event_id": child.id}
        )
        assert result["pending_parent_commit"] is True
        runtime._run_in_progress = False
        runtime._promote_deferred_events()
        return False

    monkeypatch.setattr(runtime, "_wake", interrupted_parent)
    try:
        assert runtime.process_one(timeout=0) is None
        assert child_receipt_id is not None
        assert runtime._durable_store.get(child_receipt_id).status == "queued"
        replayed = runtime.process_one(timeout=0)
        assert replayed is not None
        assert replayed.id in {parent.id, child.id}
        # Both durable receipts remain recoverable; the child was never made
        # terminal before the parent commit.
        statuses = {item.event_id: item.status for item in runtime._durable_store.list_events()}
        assert statuses[child.id] in {"queued", "acked"}
    finally:
        runtime._durable_store.close()


def test_deferred_ack_releases_child_if_parent_ack_raises(tmp_path, monkeypatch):
    event_path = tmp_path / "runtime-events.sqlite3"
    runtime = _runtime(durable_path=str(event_path))
    parent = Event("message", {"text": "parent"}, id="parent-ack-error")
    child = Event("message", {"text": "child"}, id="child-after-parent-error")
    runtime.receive_event(parent)
    parent_receipt_id = runtime._durable_receipts_by_event_id[parent.id]
    child_receipt_id = None

    def parent_wake(event):
        nonlocal child_receipt_id
        runtime._run_context = RunContext(event=event, task=None, run_id=None, loaded_state={})
        runtime._run_in_progress = True
        runtime.receive_event(child)
        child_receipt_id = runtime._durable_receipts_by_event_id[child.id]
        result = runtime._execute_runtime_tool(
            "acknowledge_pending_event", {"event_id": child.id}
        )
        assert result["pending_parent_commit"] is True
        runtime._run_in_progress = False
        runtime._promote_deferred_events()
        return True

    original_ack = runtime._durable_store.ack

    def failing_parent_ack(receipt_id, *, owner_id, fence_token):
        if receipt_id == parent_receipt_id:
            raise OSError("simulated parent ACK failure")
        return original_ack(receipt_id, owner_id=owner_id, fence_token=fence_token)

    monkeypatch.setattr(runtime, "_wake", parent_wake)
    monkeypatch.setattr(runtime._durable_store, "ack", failing_parent_ack)
    try:
        with pytest.raises(OSError, match="parent ACK failure"):
            runtime.process_one(timeout=0)
        assert child_receipt_id is not None
        assert runtime._durable_store.get(child_receipt_id).status == "queued"
    finally:
        runtime._durable_store.close()


def test_schedule_wait_intent_is_persisted_before_schedule_and_repaired_after_restart(tmp_path):
    task_path = tmp_path / "tasks.json"
    schedule_path = tmp_path / "schedules.json"
    tasks = JsonTaskStore(task_path)
    task = tasks.create("wake later")
    event = Event("message", {"text": "schedule me"})
    run = task.start_run(event.id)
    tasks.save(task)

    class CrashBeforeSchedule:
        def schedule_at(self, *args, **kwargs):
            persisted = JsonTaskStore(task_path).get(task.id)
            assert persisted is not None and persisted.status is TaskStatus.WAITING
            assert persisted.waiting_for
            raise RuntimeError("simulated crash before schedule persistence")

    runtime = _runtime(task_store=tasks, scheduler=CrashBeforeSchedule())
    runtime._current_task = task
    runtime._run_context = RunContext(event=event, task=task, run_id=run.id, loaded_state={})
    with pytest.raises(RuntimeError, match="simulated crash"):
        runtime.schedule_current_task(
            runtime.scheduler,
            event.created_at,
            payload={"kind": "reminder"},
        )

    persisted = JsonTaskStore(task_path).get(task.id)
    assert persisted is not None and persisted.status is TaskStatus.WAITING
    token = persisted.waiting_for[0].payload_equals[runtime._SCHEDULE_WAKE_TOKEN]

    scheduler = Scheduler(
        type("Publisher", (), {"publish": lambda *args, **kwargs: object()})(),
        store=JsonScheduleStore(schedule_path),
    )
    restarted = _runtime(task_store=JsonTaskStore(task_path), scheduler=scheduler)
    assert restarted._recover_schedule_wait_intents() == 1
    schedules = scheduler.store.list()
    assert len(schedules) == 1
    assert schedules[0].payload[runtime._SCHEDULE_WAKE_TOKEN] == token
    assert restarted._recover_schedule_wait_intents() == 0
