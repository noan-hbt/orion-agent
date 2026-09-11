"""Regression tests for durable sub-agent lifecycle and isolation guarantees."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Barrier, Thread

import pytest

from event_handler import EventHandler
from subagents import SubAgentJobStatus, SubAgentManager, _clip


class _LLM:
    model = "stub/model"

    def tool_definitions(self):
        return []

    def complete(self, messages, **kwargs):
        return {"choices": [{"message": {"role": "assistant", "content": "done"}}]}


class _RecordingEvents:
    def __init__(self) -> None:
        self.published: list[dict] = []

    def publish(self, event_type, payload, **kwargs):
        self.published.append(
            {"event_type": event_type, "payload": payload, "kwargs": kwargs}
        )


def _manager(tmp_path: Path, *, scope: str = "tenant-a", events=None, **kwargs):
    return SubAgentManager(
        _LLM(),
        events or EventHandler(),
        state_path=tmp_path / "subagents.json",
        scope=scope,
        emit_progress_events=False,
        **kwargs,
    )


def test_restart_requeues_running_jobs_and_preserves_session(tmp_path):
    manager = _manager(tmp_path)
    agent = manager.create_agent("worker", "does work")
    job = manager.submit("continue this", agent_id=agent.id)
    with manager._lock:
        stored = manager._jobs[job.id]
        stored.status = SubAgentJobStatus.RUNNING
        manager._sessions[stored.session_id].messages = [
            {"role": "system", "content": "instructions"},
            {"role": "user", "content": "history"},
        ]
        manager._save_locked()

    manager.close()
    restarted = _manager(tmp_path)
    recovered = restarted.get_job(job.id)
    session = restarted.get_session(job.session_id)

    assert recovered is not None
    assert recovered.status is SubAgentJobStatus.QUEUED
    assert "repris" in (recovered.error or "").lower()
    assert session is not None
    assert session.job_id == job.id
    assert session.messages[-1]["content"] == "history"


def test_session_and_job_access_are_scope_isolated(tmp_path):
    manager = _manager(tmp_path, scope="tenant-a")
    agent = manager.create_agent("worker", "does work")
    job = manager.submit("private objective", agent_id=agent.id)

    with pytest.raises(PermissionError):
        manager.get_job(job.id, caller_scope="tenant-b")
    with pytest.raises(PermissionError):
        manager.get_session(job.session_id, caller_scope="tenant-b")
    assert manager.list_jobs(caller_scope="tenant-b") == []
    assert manager.get_job(job.id, caller_scope="tenant-a") is not None


def test_submit_rejects_cross_scope_handoff_even_for_typed_context(tmp_path):
    from handoff_context import HandoffContext

    manager = _manager(tmp_path, scope="tenant-a")
    agent = manager.create_agent("worker", "does work")
    handoff = HandoffContext.create(
        kind="subagent",
        objective="cross tenant",
        correlation_id="corr",
        source_scope="tenant-a",
        source_instance_id="orion",
        target_scope="tenant-b",
        target_instance_id="other",
        target_agent_id=agent.id,
    )
    with pytest.raises(PermissionError):
        manager.submit("cross tenant", agent_id=agent.id, handoff_context=handoff)


def test_submit_and_results_are_bounded(tmp_path):
    manager = _manager(
        tmp_path,
        max_context_chars=12,
        max_result_chars=10,
        max_tool_output_chars=9,
    )
    agent = manager.create_agent("worker", "does work")
    job = manager.submit("objective that is too long", agent_id=agent.id, context="context that is too long")
    assert len(job.objective) <= 12
    assert len(job.context) <= 12
    assert _clip("x" * 100, manager.max_result_chars).endswith("…")
    assert len(_clip("x" * 100, manager.max_tool_output_chars)) <= 9


def test_list_jobs_caps_requested_limit(tmp_path):
    manager = _manager(tmp_path)
    agent = manager.create_agent("worker", "does work")
    for index in range(105):
        manager.submit(f"objective {index}", agent_id=agent.id)
    assert len(manager.list_jobs(limit=10)) == 10
    assert len(manager.list_jobs(limit=10_000)) <= 100
    assert manager.list_jobs(limit=0) == []


def test_cancel_queued_job_transitions_session_and_emits_once(tmp_path):
    events = _RecordingEvents()
    manager = _manager(tmp_path, events=events)
    agent = manager.create_agent("worker", "does work")
    job = manager.submit("cancel me", agent_id=agent.id)
    cancelled = manager.cancel_job(job.id)

    assert cancelled.status is SubAgentJobStatus.CANCELLED
    assert manager.get_session(job.session_id).status == "cancelled"
    assert [item["event_type"] for item in events.published] == ["subagent.cancelled"]

    # A restart must not duplicate an already acknowledged outbox event.
    manager.close()
    restarted = _manager(tmp_path, events=events)
    assert [item["event_type"] for item in events.published] == ["subagent.cancelled"]
    assert restarted.get_job(job.id).status is SubAgentJobStatus.CANCELLED


def test_session_compaction_keeps_tool_exchange_atomic(tmp_path):
    manager = _manager(tmp_path, max_session_messages=20)
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "objective"}]
    for index in range(20):
        messages.extend(
            [
                {"role": "assistant", "tool_calls": [{"id": f"call-{index}", "function": {"name": "web_search"}}]},
                {"role": "tool", "tool_call_id": f"call-{index}", "content": f"result-{index}"},
            ]
        )
    compacted = manager._bounded_messages(messages)
    assert len(compacted) <= 20
    for index, message in enumerate(compacted):
        if message.get("role") == "tool":
            assert index > 0
            assert compacted[index - 1].get("role") == "assistant"
            assert compacted[index - 1].get("tool_calls")
            assert message["tool_call_id"] in {
                call["id"] for call in compacted[index - 1]["tool_calls"]
            }


def test_corrupt_state_is_reported(tmp_path):
    state = tmp_path / "subagents.json"
    state.write_text("not-json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="illisible"):
        _manager(tmp_path)


def test_subagent_allowed_tools_may_narrow_but_not_exceed_operator_ceiling(tmp_path):
    manager = _manager(tmp_path, default_tools=["web"])

    narrowed = manager.create_agent(
        "researcher",
        "researches",
        allowed_tools=["web"],
    )
    assert narrowed.allowed_tools == ["web"]

    with pytest.raises(PermissionError, match="terminal"):
        manager.create_agent(
            "unsafe",
            "tries to escalate",
            allowed_tools=["web", "terminal"],
        )


def test_update_rejects_tool_capability_escalation_and_preserves_existing_tools(tmp_path):
    manager = _manager(tmp_path, default_tools=["web"])
    agent = manager.create_agent("worker", "does work", allowed_tools=["web"])

    with pytest.raises(PermissionError, match="terminal"):
        manager.update_agent(agent.id, allowed_tools=["web", "terminal"])

    assert manager.get_agent(agent.id).allowed_tools == ["web"]


def test_restart_constrains_persisted_tools_to_current_operator_ceiling(tmp_path):
    original = _manager(tmp_path, default_tools=["web", "terminal"])
    agent = original.create_agent(
        "legacy-worker",
        "old broader permissions",
        allowed_tools=["web", "terminal"],
    )

    original.close()
    restarted = _manager(tmp_path, default_tools=["web"])
    recovered = restarted.get_agent(agent.id)

    assert recovered is not None
    assert recovered.allowed_tools == ["web"]

    persisted = json.loads((tmp_path / "subagents.json").read_text(encoding="utf-8"))
    persisted_agent = next(item for item in persisted["agents"] if item["id"] == agent.id)
    assert persisted_agent["allowed_tools"] == ["web"]


def test_empty_operator_default_tools_is_an_empty_capability_ceiling(tmp_path):
    manager = _manager(tmp_path, default_tools=[])
    agent = manager.create_agent("tool-less", "no tools")

    assert agent.allowed_tools == []
    with pytest.raises(PermissionError, match="web"):
        manager.update_agent(agent.id, allowed_tools=["web"])


def test_omitted_default_tools_is_fail_closed(tmp_path):
    manager = _manager(tmp_path)
    agent = manager.create_agent("tool-less-default", "no implicit tools")

    assert manager.default_tools == ()
    assert agent.allowed_tools == []


def test_stop_does_not_terminalize_unconfirmed_running_job(tmp_path):
    events = _RecordingEvents()
    manager = _manager(tmp_path, events=events)
    agent = manager.create_agent("worker", "does work")
    job = manager.submit("running work", agent_id=agent.id)
    with manager._lock:
        manager._jobs[job.id].status = SubAgentJobStatus.RUNNING
        manager._save_locked()

    manager.stop(wait=False)
    stopped = manager.get_job(job.id)
    assert stopped is not None
    assert stopped.status is SubAgentJobStatus.RUNNING

    # With no worker owning this synthetic RUNNING record, restart applies the
    # existing crash-recovery rule and requeues it instead of persisting a
    # false terminal outcome while an external action could still be in flight.
    manager.close()
    restarted = _manager(tmp_path, events=events)
    assert restarted.get_job(job.id).status is SubAgentJobStatus.QUEUED
    assert [item["event_type"] for item in events.published].count("subagent.failed") == 0


def test_max_runtime_is_enforced_before_next_model_turn(tmp_path, monkeypatch):
    manager = _manager(tmp_path, max_runtime_seconds=1, default_tools=["web"])
    manager.llm_client.tool_definitions = lambda: [
        {"type": "function", "function": {"name": "web_search"}}
    ]
    manager.llm_client.execute_tool_call = lambda call, **kwargs: {
        "role": "tool",
        "tool_call_id": call["id"],
        "name": "web_search",
        "content": "still working",
    }
    manager.llm_client.complete = lambda messages, **kwargs: {
        "choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call-1", "type": "function", "function": {"name": "web_search", "arguments": "{}"}}
        ]}}]
    }
    agent = manager.create_agent("worker", "does work", allowed_tools=["web"], max_turns=3)
    job = manager.submit("bounded work", agent_id=agent.id)
    ticks = iter((10.0, 10.0, 12.0))
    monkeypatch.setattr("subagents.time.monotonic", lambda: next(ticks))

    with pytest.raises(TimeoutError, match="Durée maximale"):
        manager._run_agent(agent, job.id)


def test_progress_and_tool_arguments_redact_credentials(tmp_path):
    manager = _manager(tmp_path)
    agent = manager.create_agent("worker", "does work")
    job = manager.submit("redact logs", agent_id=agent.id)
    manager._record_progress(job.id, "Authorization: Bearer super-secret-token")
    manager._record_tool_call(
        job.id,
        {
            "function": {
                "name": "web_search",
                "arguments": {"api_key": "sk-1234567890abcdef"},
            }
        },
    )

    stored = manager.get_job(job.id)
    assert stored is not None
    durable = " ".join(stored.progress) + " " + " ".join(
        str(call["arguments"]) for call in stored.tool_calls
    )
    assert "super-secret-token" not in durable
    assert "sk-1234567890abcdef" not in durable
    assert "[REDACTED]" in durable


def test_outbox_claim_prevents_duplicate_concurrent_delivery(tmp_path):
    events = _RecordingEvents()
    manager = _manager(tmp_path, events=events)
    agent = manager.create_agent("worker", "does work")
    job = manager.submit("deliver once", agent_id=agent.id)
    with manager._lock:
        manager._queue_outbox_locked(job, "subagent.completed", "done", 20)

    barrier = Barrier(2)

    def drain():
        barrier.wait()
        manager._drain_outbox()

    threads = [Thread(target=drain) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert [item["event_type"] for item in events.published] == ["subagent.completed"]


def test_completed_handoff_carries_acknowledged_terminal_state(tmp_path):
    events = _RecordingEvents()
    manager = _manager(tmp_path, events=events)
    agent = manager.create_agent("worker", "does work")
    job = manager.submit("handoff result", agent_id=agent.id)
    with manager._lock:
        current = manager._jobs[job.id]
        current.status = SubAgentJobStatus.COMPLETED
        current.result = "verified result"
        current.state_version += 1
        current.handoff_context = current.handoff_context.with_state(
            "completed", result=current.result
        )
        manager._queue_outbox_locked(current, "subagent.completed", current.result, 20)
        manager._save_locked()
    manager._drain_outbox()

    payload = events.published[0]["payload"]
    assert payload["handoff_context"]["state"]["status"] == "completed"
    assert payload["handoff_context"]["output"]["result"] == "verified result"
    assert payload["handoff_id"] == job.handoff_context.handoff_id
