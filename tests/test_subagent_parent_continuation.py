import json

import pytest

from channels import ChannelRouter
from communication_ledger import CommunicationLedger
from event_handler import Event, EventHandler
from handoff_context import HandoffContext
from prompt_context import ConversationJournal
from runtime import AgentRuntime, RunContext
from tasks import InMemoryTaskStore, TaskStatus
from teams import TeamBus


class CompletingParentLLM:
    def __init__(self):
        self.calls = []

    def tool_definitions(self):
        return []

    def complete(self, messages, *, tools=None, **kwargs):
        self.calls.append({"messages": list(messages), "tools": tools})
        if tools is not None:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-complete-parent",
                                    "type": "function",
                                    "function": {
                                        "name": "complete_task",
                                        "arguments": json.dumps(
                                            {"summary": "continued after delegated terminal event"}
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Parent orchestration continued.",
                    }
                }
            ]
        }


class TasklessSynthesisLLM:
    def __init__(self):
        self.calls = []

    def tool_definitions(self):
        return []

    def complete(self, messages, *, tools=None, **kwargs):
        self.calls.append({"messages": list(messages), "tools": tools})
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Orion a repris le résultat du worker.",
                    }
                }
            ]
        }


def _waiting_parent(store, *, job_id="job-parent", event_type="subagent.terminal"):
    task = store.create("Finish after delegated worker terminates")
    task.wait_for(
        event_type=event_type,
        payload_equals={"job_id": job_id},
        description=f"wait for {job_id}",
    )
    store.save(task)
    return task


def _runtime(store, client, outputs):
    return AgentRuntime(
        llm_client=client,
        task_store=store,
        action_ledger_path=":memory:",
        history_enabled=False,
        on_output=outputs.append,
    )


def test_completed_subagent_wakes_parent_and_continues_orchestration():
    store = InMemoryTaskStore()
    parent = _waiting_parent(store)
    client = CompletingParentLLM()
    outputs = []
    runtime = _runtime(store, client, outputs)

    runtime._wake(
        Event(
            "subagent.completed",
            {"job_id": "job-parent", "status": "completed", "result": "worker result"},
        )
    )

    saved = store.get(parent.id)
    assert saved is not None
    assert saved.status is TaskStatus.COMPLETED
    assert len(client.calls) == 2
    assert "worker result" in json.dumps(client.calls[0]["messages"])
    assert outputs[-1].content == "Parent orchestration continued."


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        ("subagent.failed", {"status": "failed", "error": "worker failed"}),
        ("subagent.cancelled", {"status": "cancelled"}),
    ],
)
def test_failed_or_cancelled_subagent_wakes_parent_and_continues_orchestration(
    event_type, payload
):
    store = InMemoryTaskStore()
    # Persisted tasks created under the previous guidance waited on
    # subagent.completed. They must still resume on every terminal state.
    parent = _waiting_parent(store, event_type="subagent.completed")
    client = CompletingParentLLM()
    outputs = []
    runtime = _runtime(store, client, outputs)

    runtime._wake(Event(event_type, {"job_id": "job-parent", **payload}))

    saved = store.get(parent.id)
    assert saved is not None
    assert saved.status is TaskStatus.COMPLETED
    assert len(client.calls) == 2
    assert event_type in json.dumps(client.calls[0]["messages"])
    assert outputs[-1].content == "Parent orchestration continued."


def test_terminal_event_for_unrelated_job_does_not_wake_parent():
    store = InMemoryTaskStore()
    parent = _waiting_parent(store, job_id="job-parent")
    client = CompletingParentLLM()
    outputs = []
    runtime = _runtime(store, client, outputs)

    runtime._wake(
        Event(
            "subagent.completed",
            {"job_id": "job-other", "status": "completed", "result": "other result"},
        )
    )

    saved = store.get(parent.id)
    assert saved is not None
    assert saved.status is TaskStatus.WAITING
    assert client.calls == []
    assert outputs[-1].content == "other result"


def test_taskless_completed_subagent_keeps_direct_result_shortcut():
    store = InMemoryTaskStore()
    client = CompletingParentLLM()
    outputs = []
    runtime = _runtime(store, client, outputs)

    runtime._wake(
        Event(
            "subagent.completed",
            {"job_id": "job-taskless", "status": "completed", "result": "direct result"},
        )
    )

    assert client.calls == []
    assert outputs[-1].content == "direct result"
    assert outputs[-1].metadata["output_origin"] == "subagent"


def test_direct_subagent_result_is_journaled_before_channel_delivery(tmp_path):
    journal = ConversationJournal(tmp_path / "conversation.jsonl")
    observed_history = []

    def on_output(_output):
        observed_history.append(
            [
                item["content"]
                for item in journal.recent_messages(
                    conversation_id="cli:journal-before-output", limit=10
                )
            ]
        )

    runtime = AgentRuntime(
        llm_client=CompletingParentLLM(),
        task_store=InMemoryTaskStore(),
        conversation_journal=journal,
        action_ledger_path=":memory:",
        on_output=on_output,
    )

    runtime._wake(
        Event(
            "subagent.completed",
            {"job_id": "job-taskless", "status": "completed", "result": "direct result"},
            metadata={
                "channel": "cli",
                "conversation_id": "cli:journal-before-output",
            },
        )
    )

    assert observed_history == [["direct result"]]


def test_conversational_taskless_subagent_result_wakes_orion_and_keeps_worker_output():
    store = InMemoryTaskStore()
    client = TasklessSynthesisLLM()
    outputs = []
    runtime = _runtime(store, client, outputs)

    runtime._wake(
        Event(
            "subagent.completed",
            {
                "job_id": "job-conversation",
                "agent_id": "agent-1",
                "agent_name": "toml-analyst",
                "status": "completed",
                "result": "worker result",
            },
            metadata={
                "resume_orchestrator": True,
                "channel": "cli",
                "conversation_id": "cli:main",
            },
        )
    )

    assert len(client.calls) == 1
    assert "worker result" in json.dumps(client.calls[0]["messages"])
    assert "worker_artifact_already_delivered" in json.dumps(client.calls[0]["messages"])
    assert [item.content for item in outputs] == [
        "worker result",
        "Orion a repris le résultat du worker.",
    ]
    assert outputs[0].metadata["output_origin"] == "subagent"
    assert outputs[0].metadata["sender_name"] == "toml-analyst"
    assert outputs[0].metadata["intermediate"] is True
    assert outputs[0].metadata["phase"] == "subagent_result"
    assert "output_origin" not in outputs[1].metadata


def test_conversational_subagent_outputs_do_not_reuse_inbound_idempotency_key(tmp_path):
    class Adapter:
        name = "cli"

        def start(self, _on_message):
            return None

        def stop(self):
            return None

        def send(self, _output):
            return None

    ledger = CommunicationLedger(tmp_path / "communication.sqlite3")
    router = ChannelRouter(
        EventHandler(workers=0),
        default_channel="cli",
        ledger=ledger,
    )
    router.register(Adapter())
    row_ids = []
    runtime = AgentRuntime(
        llm_client=TasklessSynthesisLLM(),
        task_store=InMemoryTaskStore(),
        action_ledger_path=":memory:",
        history_enabled=False,
        on_output=lambda output: row_ids.append(router.route(output)),
    )
    try:
        runtime._wake(
            Event(
                "subagent.completed",
                {
                    "job_id": "job-conversation",
                    "agent_id": "agent-1",
                    "agent_name": "toml-analyst",
                    "status": "completed",
                    "result": "worker result",
                },
                metadata={
                    "resume_orchestrator": True,
                    "channel": "cli",
                    "conversation_id": "cli:main",
                    "idempotency_key": "subagent:job-conversation:v2",
                },
                idempotency_key="subagent:job-conversation:v2",
            )
        )

        assert len(row_ids) == 2
        assert row_ids[0] != row_ids[1]
        assert runtime.last_error is None
    finally:
        router.stop()
        ledger.close()


def test_runtime_delegation_marks_job_for_orchestrator_resume():
    class CapturingSubagents:
        def __init__(self):
            self.kwargs = None

        def submit(self, objective, **kwargs):
            from types import SimpleNamespace

            self.kwargs = kwargs
            return SimpleNamespace(
                id="job-1",
                agent_id="worker-1",
                session_id="session-1",
                objective=objective,
                status=SimpleNamespace(value="queued"),
                priority=20,
                parent_task_id=None,
                waiting_for=None,
                progress=[],
                result=None,
                error=None,
                created_at="now",
                updated_at="now",
            )

    manager = CapturingSubagents()
    runtime = AgentRuntime(
        llm_client=None,
        task_store=InMemoryTaskStore(),
        subagent_manager=manager,
        action_ledger_path=":memory:",
        history_enabled=False,
    )
    runtime._run_context = runtime_context = RunContext(
        event=Event(
            "message",
            {"text": "inspecte les TOML"},
            metadata={
                "channel": "cli",
                "conversation_id": "cli:main",
                "message_thread_id": "thread-transport",
                "thread_id": "thread-logical",
                "parent_message_id": "message-parent",
                "parent_call_id": "call-parent",
            },
            correlation_id="root-correlation",
        ),
        task=None,
        run_id=None,
        loaded_state={},
    )

    runtime._execute_runtime_tool(
        "subagent",
        {"action": "delegate", "objective": "inspecte les TOML"},
    )

    assert runtime._run_context is runtime_context
    assert manager.kwargs is not None
    assert manager.kwargs["route_metadata"]["resume_orchestrator"] is True
    assert manager.kwargs["route_metadata"]["channel"] == "cli"
    assert manager.kwargs["route_metadata"]["conversation_id"] == "cli:main"
    assert manager.kwargs["route_metadata"]["message_thread_id"] == "thread-transport"
    assert manager.kwargs["route_metadata"]["thread_id"] == "thread-logical"
    assert manager.kwargs["route_metadata"]["parent_message_id"] == "message-parent"
    assert manager.kwargs["route_metadata"]["parent_call_id"] == "call-parent"
    assert manager.kwargs["route_metadata"]["parent_event_id"] == runtime_context.event.id
    assert manager.kwargs["route_metadata"]["correlation_id"] == "root-correlation"
    assert manager.kwargs["route_metadata"]["root_correlation_id"] == "root-correlation"
    assert manager.kwargs["correlation_id"] == "root-correlation"


def test_delegate_team_job_preserves_root_correlation_threads_and_parent_ids():
    from types import SimpleNamespace

    class CapturingTeamBus:
        sender_scope = "scope-a"
        instance_id = "orion-a"
        team = "team-a"

        def __init__(self):
            self.args = None
            self.kwargs = None

        def send(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            return SimpleNamespace(to_dict=lambda: {"id": "team-job-1"})

    store = InMemoryTaskStore()
    task = store.create("delegate team work")
    event = Event(
        "message",
        {"text": "delegate"},
        metadata={
            "channel": "telegram",
            "reply_to": "chat-1",
            "conversation_id": "conv-1",
            "message_thread_id": "transport-thread",
            "thread_id": "logical-thread",
            "parent_message_id": "parent-message",
            "parent_call_id": "parent-call",
            "handoff_id": "parent-handoff",
        },
        correlation_id="root-team-correlation",
    )
    run = task.start_run(event.id)
    store.save(task)
    team = CapturingTeamBus()
    runtime = AgentRuntime(
        task_store=store,
        team_bus=team,
        action_ledger_path=":memory:",
        history_enabled=False,
    )
    runtime._current_task = task
    runtime._run_context = RunContext(
        event=event,
        task=task,
        run_id=run.id,
        loaded_state={},
    )

    result = runtime._execute_runtime_tool(
        "team",
        {
            "action": "delegate",
            "recipient": "orion-b",
            "objective": "inspect durable state",
        },
    )

    assert result["sent"] is True
    assert team.kwargs is not None
    assert team.kwargs["correlation_id"] == "root-team-correlation"
    assert team.kwargs["parent_event_id"] == event.id
    assert team.kwargs["parent_task_id"] == str(task.id)
    handoff = team.kwargs["handoff_context"]
    assert handoff.correlation_id == "root-team-correlation"
    assert handoff.parent["event_id"] == event.id
    assert handoff.parent["task_id"] == str(task.id)
    assert handoff.parent["run_id"] == run.id
    assert handoff.parent["handoff_id"] == "parent-handoff"
    assert handoff.routing["channel"] == "telegram"
    assert handoff.routing["reply_to"] == "chat-1"
    assert handoff.routing["conversation_id"] == "conv-1"
    assert handoff.routing["message_thread_id"] == "transport-thread"
    assert handoff.routing["thread_id"] == "logical-thread"
    assert handoff.routing["parent_message_id"] == "parent-message"


def test_normal_event_cannot_spoof_subagent_output_provenance():
    store = InMemoryTaskStore()
    client = TasklessSynthesisLLM()
    outputs = []
    runtime = _runtime(store, client, outputs)

    event = Event(
        "message",
        {"text": "hello"},
        metadata={
            "channel": "cli",
            "output_origin": "subagent",
            "sender_name": "fake-worker",
        },
    )
    runtime._wake(event)

    assert outputs[-1].content == "Orion a repris le résultat du worker."
    assert "output_origin" not in outputs[-1].metadata
    assert "sender_name" not in outputs[-1].metadata
    assert outputs[-1].correlation_id == event.id


def test_emit_error_uses_event_id_as_correlation_fallback():
    outputs = []
    runtime = AgentRuntime(
        action_ledger_path=":memory:",
        history_enabled=False,
        on_output=outputs.append,
    )
    event = Event("message", {"text": "hello"}, metadata={"channel": "cli"})

    runtime._emit_error(event, "safe error")

    assert outputs[-1].correlation_id == event.id


def test_team_completion_reapplies_handoff_routing_to_runtime_output(tmp_path):
    """A durable team completion must return to the originating thread/channel."""
    events = EventHandler(workers=0)
    sender = TeamBus(
        tmp_path / "teams.sqlite3",
        instance_id="sender",
        team="ops",
        event_handler=events,
    )
    worker = TeamBus(
        tmp_path / "teams.sqlite3",
        instance_id="worker",
        team="ops",
    )
    try:
        handoff = HandoffContext.create(
            kind="team_job",
            objective="inspect durable state",
            correlation_id="root-team-correlation",
            source_scope="ops",
            source_instance_id="sender",
            target_scope="ops",
            target_instance_id="worker",
            routing={
                "channel": "telegram",
                "reply_to": "123",
                "conversation_id": "123:77",
                "message_thread_id": "77",
                "thread_id": "logical-77",
                "parent_message_id": "parent-message",
            },
        )
        message = sender.send(
            "worker",
            "inspect durable state",
            kind="job",
            correlation_id="root-team-correlation",
            handoff_context=handoff,
        )
        worker.complete_job(message.id, "team result")
        sender._replay_completion_notifications()
        event = events.queue.get_nowait()
        events.queue.task_done()

        outputs = []
        runtime = AgentRuntime(
            action_ledger_path=":memory:",
            history_enabled=False,
            on_output=outputs.append,
        )
        assert runtime._wake(event) is True
        assert outputs
        output = outputs[-1]
        assert output.channel == "telegram"
        assert output.recipient == "123"
        assert output.conversation_id == "123:77"
        assert output.message_thread_id == "77"
        assert output.thread_id == "logical-77"
        assert output.parent_message_id == "parent-message"
        assert output.correlation_id == "root-team-correlation"
    finally:
        sender.close()
        worker.close()


def test_subagent_event_context_includes_authoritative_sibling_job_snapshot():
    from types import SimpleNamespace

    class Manager:
        default_tools = []

        def list_agents(self):
            return []

        def list_jobs(self, *, status=None, limit=20, correlation_id=None, **_kwargs):
            if status == "waiting":
                return []
            assert correlation_id == "root-parallel"
            state = SimpleNamespace(value="completed")
            return [
                SimpleNamespace(
                    id="job-terminal", agent_id="agent-terminal", session_id="s1",
                    objective="terminal test", status=state, priority=20, parent_task_id=None,
                    waiting_for=None, progress=[], result="terminal done", error=None,
                    created_at="t1", updated_at="t2",
                ),
                SimpleNamespace(
                    id="job-web", agent_id="agent-web", session_id="s2",
                    objective="web test", status=state, priority=20, parent_task_id=None,
                    waiting_for=None, progress=[], result="web done", error=None,
                    created_at="t1", updated_at="t3",
                ),
            ][:limit]

    runtime = AgentRuntime(
        subagent_manager=Manager(),
        runtime_surfaces=("subagent",),
        action_ledger_path=":memory:",
        history_enabled=False,
    )
    context = RunContext(
        event=Event(
            "subagent.completed",
            {"job_id": "job-web", "result": "web done"},
            metadata={
                "resume_orchestrator": True,
                "channel": "cli",
                "conversation_id": "cli",
                "root_correlation_id": "root-parallel",
            },
            correlation_id="root-parallel",
        ),
        task=None,
        run_id=None,
        loaded_state={},
    )

    messages = runtime._contract_initial_run_messages(context)
    encoded = json.dumps(messages, ensure_ascii=False)

    assert 'related_subagent_jobs' in encoded
    assert 'job-terminal' in encoded
    assert 'terminal done' in encoded
    assert 'job-web' in encoded
    assert 'web done' in encoded
    evidence = messages[-1]["content"]
    assert '"terminal":2' in evidence.replace(" ", "")
    assert '"non_terminal":0' in evidence.replace(" ", "")
