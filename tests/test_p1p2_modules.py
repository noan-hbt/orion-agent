import json
from io import StringIO
from types import SimpleNamespace

import pytest

from approvals import ApprovalStore
from budgets import BudgetExceeded, BudgetLimits, BudgetTracker
from memory_store import MemoryStore
from observability import JsonLogger
from workflows import WorkflowEngine


def test_budget_tracker_enforces_calls():
    tracker = BudgetTracker(BudgetLimits(max_calls=2))
    tracker.record(calls=1)
    tracker.check()
    with pytest.raises(BudgetExceeded):
        tracker.record(calls=1)


def test_memory_store_is_namespace_isolated_and_respects_consent(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    try:
        first = store.put("private note", namespace="user-a", consent=False)
        store.put("same words", namespace="user-b")
        assert store.get(first.id, namespace="user-b") is None
        assert store.search(namespace="user-a") == []
        assert store.search(namespace="user-a", include_without_consent=True)[0].content == "private note"
    finally:
        store.close()


def test_workflow_pauses_for_and_resumes_after_approval(tmp_path):
    approvals = ApprovalStore(str(tmp_path / "approvals.sqlite3"))
    engine = WorkflowEngine(str(tmp_path / "workflows.sqlite3"), approvals=approvals)
    run = engine.start(
        "deploy",
        [{"approval": {"requester": "ci", "scope": "deploy"}}, {"action": "finish"}],
    )
    assert run["state"] == "waiting_approval"
    approvals.approve(run["approval_id"], decided_by="owner")
    actions = []
    done = engine.resume(run["id"], executor=lambda action, context: actions.append(action))
    assert done["state"] == "completed"
    assert actions == ["finish"]


def test_json_logger_redacts_sensitive_fields():
    stream = StringIO()
    JsonLogger(stream, enabled=True).log(
        "info", "unit", correlation_id="corr-1", content="secret", ok=True
    )
    record = json.loads(stream.getvalue())
    assert record["ok"] is True
    assert "content" not in record
    assert record["correlation_id"] == "corr-1"


def test_subagent_completion_outbox_preserves_originating_route(tmp_path):
    from event_handler import EventHandler
    from subagents import SubAgentManager

    class FakeLLM:
        model = "openai/test-model"

    published = []
    handler = EventHandler()
    original_publish = handler.publish
    handler.publish = lambda *args, **kwargs: (published.append(kwargs), original_publish(*args, **kwargs))[1]
    manager = SubAgentManager(
        FakeLLM(), handler, state_path=tmp_path / "subagents.json", emit_progress_events=False
    )
    manager.create_agent("Researcher", "research")
    job = manager.submit(
        "research",
        route_metadata={"channel": "telegram", "reply_to": "12345"},
    )
    with manager._lock:
        manager._queue_outbox_locked(job, "subagent.completed", "done", 20)
    manager._drain_outbox()
    assert published
    assert published[-1]["metadata"]["channel"] == "telegram"
    assert published[-1]["metadata"]["reply_to"] == "12345"


def test_completed_subagent_result_skips_second_provider_call():
    from event_handler import Event
    from runtime import AgentRuntime, RunPhase

    transitions = []
    fake_runtime = SimpleNamespace(
        llm_client=object(),
        _transition=lambda *args: transitions.append(args),
    )
    context = SimpleNamespace(
        event=Event("subagent.completed", {"result": "résultat vérifié"}),
        answer=None,
        phase=None,
    )
    AgentRuntime._run_agent_loop(fake_runtime, context)
    assert context.answer == "résultat vérifié"
    assert context.phase is RunPhase.ANSWER
    assert transitions
