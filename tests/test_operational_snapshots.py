from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from event_handler import EventHandler
from scheduler import InMemoryScheduleStore, ScheduleStatus, Scheduler
from subagents import SubAgentJobStatus, SubAgentManager
from teams import TeamBus


class _LLM:
    model = "stub/model"

    def tool_definitions(self):
        return []


def test_scheduler_snapshot_is_compact_json_safe_and_counts_statuses() -> None:
    store = InMemoryScheduleStore()
    scheduler = Scheduler(EventHandler(workers=0), store=store)
    old = datetime.now(timezone.utc) - timedelta(seconds=30)
    active = scheduler.schedule_at(old, task_id=1, payload={"secret": "hidden"})
    retrying = scheduler.schedule_at(old, task_id=2)
    failed = scheduler.schedule_at(old, task_id=3)
    retrying.status = ScheduleStatus.RETRYING
    failed.status = ScheduleStatus.FAILED
    store.save(retrying)
    store.save(failed)

    snapshot = scheduler.snapshot()

    json.dumps(snapshot)
    assert snapshot["component"] == "scheduler"
    assert snapshot["pending"] == 2
    assert snapshot["retrying"] == 1
    assert snapshot["failed"] == 1
    assert snapshot["status_counts"][ScheduleStatus.ACTIVE.value] == 1
    assert snapshot["oldest_pending_age_seconds"] >= 0
    assert "secret" not in json.dumps(snapshot)
    assert active.id not in json.dumps(snapshot)


def test_subagent_snapshot_reports_workers_agents_jobs_and_outbox(tmp_path) -> None:
    manager = SubAgentManager(
        _LLM(),
        EventHandler(workers=0),
        state_path=tmp_path / "subagents.json",
        emit_progress_events=False,
        workers=2,
    )
    active = manager.create_agent("active", "general")
    disabled = manager.create_agent("disabled", "general")
    manager.update_agent(disabled.id, status="disabled")
    queued = manager.submit("queued secret objective", agent_id=active.id)
    running = manager.submit("running", agent_id=active.id)
    waiting = manager.submit("waiting", agent_id=active.id)
    failed = manager.submit("failed", agent_id=active.id)
    with manager._lock:
        manager._jobs[running.id].status = SubAgentJobStatus.RUNNING
        manager._jobs[waiting.id].status = SubAgentJobStatus.WAITING
        manager._jobs[failed.id].status = SubAgentJobStatus.FAILED
        manager._queue_outbox_locked(
            manager._jobs[failed.id], "subagent.failed", "failure detail", 20
        )

    snapshot = manager.snapshot()

    json.dumps(snapshot)
    assert snapshot["workers"] == {"configured": 2, "alive": 0}
    assert snapshot["agents"]["total"] == 2
    assert snapshot["agents"]["status_counts"] == {"active": 1, "disabled": 1}
    assert snapshot["pending"] == 2
    assert snapshot["inflight"] == 1
    assert snapshot["failed"] == 1
    assert snapshot["retry_pending"] == 1
    assert snapshot["oldest_pending_age_seconds"] >= 0
    encoded = json.dumps(snapshot)
    assert "queued secret objective" not in encoded
    assert queued.id not in encoded


def test_team_bus_snapshot_uses_aggregates_and_hides_message_content(tmp_path) -> None:
    path = tmp_path / "teams.sqlite3"
    sender = TeamBus(path, instance_id="sender", team="ops")
    receiver = TeamBus(path, instance_id="receiver", team="ops")
    try:
        queued = sender.send("receiver", "sensitive body", kind="message")
        claimed = sender.send("receiver", "job body", kind="job")
        failed = sender.send("receiver", "failure body", kind="job")
        assert receiver._claim_for_poll(claimed.id)
        receiver.complete_job(failed.id, "failed detail", success=False)

        snapshot = receiver.snapshot()

        json.dumps(snapshot)
        assert snapshot["component"] == "team_bus"
        assert snapshot["pending"] == 1
        assert snapshot["inflight"] == 1
        assert snapshot["failed"] == 1
        assert snapshot["oldest_pending_age_seconds"] >= 0
        encoded = json.dumps(snapshot)
        assert "sensitive body" not in encoded
        assert queued.id not in encoded
    finally:
        receiver.close()
        sender.close()


def test_team_bus_snapshot_after_close_is_safe(tmp_path) -> None:
    bus = TeamBus(tmp_path / "teams.sqlite3", instance_id="one", team="ops")
    bus.close()

    snapshot = bus.snapshot()

    assert snapshot["closed"] is True
    assert snapshot["running"] is False
    json.dumps(snapshot)
