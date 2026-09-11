from __future__ import annotations

from datetime import datetime, timedelta, timezone

import action_ledger as action_ledger_module
import workflows as workflows_module
from action_ledger import ActionLedger
from approvals import ApprovalStore
from event_handler import EventHandler, EventPriority
from scheduler import JsonScheduleStore, ScheduleStatus, Scheduler
from subagents import SubAgentJobStatus, SubAgentManager
from teams import TeamBus
from workflows import WorkflowEngine


class _LLMStub:
    model = "stub/model"

    def tool_definitions(self):
        return []


class _FailFirstFiredSave(JsonScheduleStore):
    """Expose the scheduler crash window after EventHandler accepted publish."""

    def __init__(self, path):
        self._fail_fired_once = True
        super().__init__(path)

    def save(self, schedule):
        if schedule.status is ScheduleStatus.FIRED and self._fail_fired_once:
            self._fail_fired_once = False
            raise OSError("simulated crash after publish")
        return super().save(schedule)


def test_scheduler_restart_reuses_key_and_event_handler_suppresses_replay(tmp_path):
    path = tmp_path / "schedules.json"
    events = EventHandler(workers=0)
    due = datetime(2026, 1, 1, tzinfo=timezone.utc)
    first = Scheduler(events, store=_FailFirstFiredSave(path))
    schedule = first.schedule_at(due, task_id=7, payload={"wake": "task"})

    # Publish succeeds, but persisting FIRED fails. The durable schedule remains
    # retryable while EventHandler already owns the accepted event.
    assert first.trigger_due(now=due + timedelta(seconds=1)) == 0
    assert events.queue.qsize() == 1
    original = events.queue.get_nowait()
    events.queue.task_done()
    assert original.idempotency_key == f"scheduler:{schedule.id}:v1"

    # A scheduler restart must publish the exact same contract. EventHandler's
    # dedupe must return the original receipt rather than enqueue a second wake.
    first.close()
    restarted = Scheduler(events, store=JsonScheduleStore(path))
    assert restarted.trigger_due(now=due + timedelta(seconds=2)) == 1
    assert events.queue.qsize() == 0
    persisted = restarted.store.get(schedule.id)
    assert persisted is not None and persisted.status is ScheduleStatus.FIRED
    restarted.close()


def test_subagent_pending_outbox_replay_is_deduped_by_event_handler(tmp_path, monkeypatch):
    path = tmp_path / "subagents.json"
    events = EventHandler(workers=0)
    manager = SubAgentManager(
        _LLMStub(),
        events,
        state_path=path,
        emit_progress_events=False,
        default_tools=[],
    )
    agent = manager.create_agent("worker", "integration worker")
    submitted = manager.submit("finish reliably", agent_id=agent.id)

    with manager._lock:
        job = manager._jobs[submitted.id]
        job.status = SubAgentJobStatus.COMPLETED
        job.result = "done"
        job.state_version += 1
        job.handoff_context = job.handoff_context.with_state("completed", result=job.result)
        manager._queue_outbox_locked(job, "subagent.completed", job.result, EventPriority.NORMAL)
        manager._save_locked()

    # Simulate the same crash window as a real outbox: EventHandler accepts the
    # event, then the manager fails to persist the publish receipt.
    original_save = manager._save_locked
    fail_once = {"pending": True}

    def fail_receipt_save():
        if fail_once["pending"]:
            fail_once["pending"] = False
            raise OSError("simulated crash after EventHandler acceptance")
        return original_save()

    monkeypatch.setattr(manager, "_save_locked", fail_receipt_save)
    manager._drain_outbox()
    assert events.queue.qsize() == 1
    queued = events.queue.get_nowait()
    events.queue.task_done()
    assert queued.idempotency_key == f"subagent:{submitted.id}:v1"

    # Loading the persisted pending outbox automatically replays it. The stable
    # idempotency key must compose with EventHandler dedupe instead of producing
    # another runtime event.
    manager.close()
    restarted = SubAgentManager(
        _LLMStub(),
        events,
        state_path=path,
        emit_progress_events=False,
        default_tools=[],
    )
    assert events.queue.qsize() == 0
    with restarted._lock:
        item = next(iter(restarted._outbox.values()))
        assert item["published"] is True
        assert item["idempotency_key"] == queued.idempotency_key


def test_action_ledger_uncertain_requires_reconcile_before_new_fence(tmp_path, monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr(action_ledger_module.time, "time", lambda: clock["now"])
    path = tmp_path / "actions.sqlite3"
    first = ActionLedger(path, owner_id="worker-a", default_lease_seconds=5)
    second = ActionLedger(path, owner_id="worker-b", default_lease_seconds=5)
    try:
        reservation = first.reserve("charge", {"amount": 10}, target="account:1")
        assert reservation.allowed and reservation.fence_token == 1

        clock["now"] = 106.0
        blocked = second.reserve("charge", {"amount": 10}, target="account:1")
        assert blocked.allowed is False
        assert blocked.reason == "needs_reconciliation"

        second.reconcile(
            reservation.action_key,
            outcome="failed",
            error="provider confirms effect absent",
        )
        retry = second.reserve("charge", {"amount": 10}, target="account:1")
        assert retry.allowed and retry.fence_token == 2

        stale = first.complete(
            reservation.action_key,
            {"receipt": "stale"},
            owner_id="worker-a",
            fence_token=reservation.fence_token,
        )
        assert stale is not None
        assert stale.status == "running"
        assert stale.owner_id == "worker-b"
        assert stale.fence_token == 2
    finally:
        first.close()
        second.close()


def test_workflow_expired_claim_survives_restart_as_uncertain_without_reexecution(
    tmp_path, monkeypatch
):
    clock = {"now": 200.0}
    monkeypatch.setattr(workflows_module.time, "time", lambda: clock["now"])
    workflow_path = str(tmp_path / "workflows.sqlite3")
    approvals_path = str(tmp_path / "approvals.sqlite3")
    first = WorkflowEngine(
        workflow_path,
        approvals=ApprovalStore(approvals_path),
        claim_lease_seconds=5,
    )
    run = first.start(
        "deploy",
        [{"approval": {"scope": "deploy"}, "action": "publish"}],
    )
    first.approvals.approve(run["approval_id"])
    first._clear_approval(run["id"], run["approval_id"])
    claim_state, token = first._claim_action(run["id"], 0)
    assert claim_state == "claimed" and token

    clock["now"] = 206.0
    restarted = WorkflowEngine(
        workflow_path,
        approvals=ApprovalStore(approvals_path),
        claim_lease_seconds=5,
    )
    effects: list[str] = []
    uncertain = restarted.resume(
        run["id"], executor=lambda action, _context: effects.append(action)
    )
    assert uncertain["state"] == "uncertain"
    assert uncertain["reconciliation"]["reason"] == "claim_expired"
    assert effects == []

    restarted.reconcile(run["id"], completed=False)
    completed = restarted.resume(
        run["id"], executor=lambda action, _context: effects.append(action)
    )
    assert completed["state"] == "completed"
    assert effects == ["publish"]


def test_team_bus_recovery_fences_stale_owner_and_preserves_scope(tmp_path):
    path = tmp_path / "teams.sqlite3"
    sender = TeamBus(path, instance_id="sender", team="ops")
    stale = TeamBus(path, instance_id="worker", team="ops", claim_timeout=5)
    replacement = TeamBus(path, instance_id="worker", team="ops", claim_timeout=5)
    other_scope = TeamBus(path, instance_id="worker", team="other", claim_timeout=5)
    try:
        message = sender.send("worker", "process once", kind="job")
        assert stale._claim_for_poll(message.id) is True

        # Model a crashed owner without sleeping: its durable lease is expired,
        # then a second connection performs normal recovery/claim.
        with stale._lock:
            stale._connection.execute(
                "UPDATE team_messages SET claim_expires_at=0 WHERE id=?",
                (message.id,),
            )
            stale._connection.commit()

        assert other_scope.inbox() == []
        assert [item.id for item in replacement.inbox()] == [message.id]
        assert replacement._claim_for_poll(message.id) is True
        assert stale.acknowledge_delivery(message.id) is False
        assert replacement.acknowledge_delivery(message.id) is True
        assert other_scope.get(message.id) is None
    finally:
        other_scope.close()
        replacement.close()
        stale.close()
        sender.close()
