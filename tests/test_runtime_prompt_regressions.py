from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from event_handler import Event
from prompt_context import ConversationJournal, PromptContextStore, SQLiteConversationJournal
from runtime import AgentRuntime, RunContext
from tool_manager import ToolGuidance


def _context(text: str = "bonjour", *, conversation_id: str = "cli:main") -> RunContext:
    return RunContext(
        event=Event(
            "message",
            {"text": text},
            metadata={"channel": "cli", "conversation_id": conversation_id},
        ),
        task=None,
        run_id=None,
        loaded_state={},
    )


def _evidence(messages):
    content = next(
        str(item["content"])
        for item in messages
        if str(item.get("content", "")).startswith("BEGIN_ORION_EVIDENCE")
    )
    return json.loads(
        content.removeprefix("BEGIN_ORION_EVIDENCE\n").removesuffix(
            "\nEND_ORION_EVIDENCE"
        )
    )


def test_contract_and_legacy_system_prompt_include_all_configured_layers():
    store = PromptContextStore(
        ":memory:",
        core="CORE_MARKER",
        personality="PERSONALITY_MARKER",
        methodology="METHODOLOGY_MARKER",
        additional="ADDITIONAL_MARKER",
    )
    for mode in ("contract", "legacy"):
        runtime = AgentRuntime(
            prompt_store=store,
            context_mode=mode,
            runtime_surfaces=(),
            action_ledger_path=":memory:",
        )
        system = runtime._system_instructions()
        assert system.index("CORE_MARKER") < system.index("PERSONALITY_MARKER")
        assert system.index("PERSONALITY_MARKER") < system.index("METHODOLOGY_MARKER")
        assert system.index("METHODOLOGY_MARKER") < system.index("ADDITIONAL_MARKER")
        assert "## RUNTIME CONTROLS" in system


def test_system_prompt_override_replaces_core_only_not_personality():
    store = PromptContextStore(
        ":memory:", core="ORIGINAL_CORE", personality="KEEP_PERSONALITY"
    )
    runtime = AgentRuntime(
        prompt_store=store,
        system_prompt="OVERRIDE_CORE",
        context_mode="legacy",
        runtime_surfaces=(),
        action_ledger_path=":memory:",
    )
    system = runtime._system_instructions()
    assert "OVERRIDE_CORE" in system
    assert "ORIGINAL_CORE" not in system
    assert "KEEP_PERSONALITY" in system


def test_policy_budget_keeps_all_section_headings_under_oversized_content():
    store = PromptContextStore(
        ":memory:",
        core="C" * 20_000,
        personality="P" * 5_000,
        methodology="M" * 5_000,
        additional="A" * 5_000,
    )
    runtime = AgentRuntime(
        prompt_store=store,
        runtime_surfaces=(),
        action_ledger_path=":memory:",
        tool_guidance={"global": ToolGuidance(instructions="G" * 20_000)},
    )
    system = runtime._system_instructions()
    for heading in (
        "## CORE POLICY",
        "## PERSONALITY",
        "## METHODOLOGY",
        "## ADDITIONAL INSTRUCTIONS",
        "## RUNTIME CONTROLS",
        "## TOOL GUIDANCE",
    ):
        assert heading in system


def test_concise_guidance_is_utf8_and_distinguishes_soft_target_from_hard_limit():
    runtime = AgentRuntime(
        runtime_surfaces=(),
        response_concise=True,
        response_max_chars=1800,
        response_max_sentences=5,
        action_ledger_path=":memory:",
    )
    system = runtime._system_instructions()
    assert "Style par défaut : écris" in system
    assert "très court" in system
    assert "préférence souple" in system
    assert "livraison est limitée à 1800 caractères" in system
    assert "d?faut" not in system
    assert "r?ponse" not in system


def test_response_concise_false_removes_runtime_terse_directive():
    store = PromptContextStore(":memory:", core="CORE_NEUTRAL")
    runtime = AgentRuntime(
        prompt_store=store,
        response_concise=False,
        runtime_surfaces=(),
        action_ledger_path=":memory:",
    )
    assert "Style par défaut" not in runtime._system_instructions()


def test_contract_request_text_appears_once_and_not_inside_evidence_event_payload():
    marker = "UNIQUE_REQUEST_MARKER"
    runtime = AgentRuntime(
        context_mode="contract", runtime_surfaces=(), action_ledger_path=":memory:"
    )
    messages = runtime._contract_initial_run_messages(_context(marker))
    encoded = json.dumps(messages, ensure_ascii=False)
    assert encoded.count(marker) == 1
    evidence = _evidence(messages)["data"]
    assert "request" not in evidence
    assert "text" not in evidence["event"]["payload"]


def test_legacy_request_appears_once_and_persistent_context_is_retained():
    marker = "LEGACY_UNIQUE_REQUEST"
    store = PromptContextStore(":memory:")
    store.apply_extraction(
        {
            "user_profile": {"role": "PROFILE_MARKER"},
            "preferences": ["PREFERENCE_MARKER"],
            "memories": ["MEMORY_MARKER"],
        }
    )
    runtime = AgentRuntime(
        prompt_store=store,
        context_mode="legacy",
        runtime_surfaces=(),
        history_enabled=False,
        action_ledger_path=":memory:",
    )
    messages = runtime._initial_run_messages(_context(marker))
    encoded = json.dumps(messages, ensure_ascii=False)
    assert encoded.count(marker) == 1
    assert "PROFILE_MARKER" in encoded
    assert "PREFERENCE_MARKER" in encoded
    assert "MEMORY_MARKER" in encoded


def test_handoff_event_is_internal_in_contract_and_legacy_contexts():
    event = Event(
        "handoff.completed",
        {"result": "worker result"},
        metadata={"conversation_id": "cli:main", "internal_event": True},
    )
    context = RunContext(event=event, task=None, run_id=None, loaded_state={})
    contract = AgentRuntime(
        context_mode="contract", runtime_surfaces=(), action_ledger_path=":memory:"
    )._contract_initial_run_messages(context)
    assert "ORION_REQUEST_V1" not in contract[1]["content"]
    assert '"kind":"internal"' in contract[1]["content"].replace(" ", "")

    legacy = AgentRuntime(
        context_mode="legacy", runtime_surfaces=(), action_ledger_path=":memory:"
    )._initial_run_messages(context)
    event_message = next(item for item in legacy if "Événement reçu" in str(item.get("content")))
    assert event_message["role"] == "system"


def test_taskless_team_delegation_marks_handoff_for_orchestrator_resume():
    class Team:
        sender_scope = "scope-a"
        instance_id = "orion-a"
        team = "team-a"

        def __init__(self):
            self.handoff_context = None

        def send(self, _recipient, _body, **kwargs):
            self.handoff_context = kwargs["handoff_context"]
            return SimpleNamespace(to_dict=lambda: {"id": "team-job"})

    team = Team()
    runtime = AgentRuntime(
        team_bus=team,
        runtime_surfaces=("team",),
        action_ledger_path=":memory:",
    )
    context = _context("delegate")
    runtime._run_context = context

    runtime._execute_runtime_tool(
        "team",
        {"action": "delegate", "recipient": "orion-b", "objective": "inspect"},
    )

    assert team.handoff_context is not None
    serialized = team.handoff_context.to_dict()
    assert serialized["routing"]["intent"] == "resume_orchestrator"
    completed = Event(
        "handoff.completed",
        {"handoff_context": serialized, "result": "done"},
        metadata={"internal_event": True},
    )
    assert runtime._handoff_resumes_orchestrator(completed) is True


def test_task_context_asks_for_task_max_tokens_not_event_budget(monkeypatch):
    runtime = AgentRuntime(context_mode="contract", action_ledger_path=":memory:")
    seen = []
    original = runtime._context_limit

    def capture(name, fallback):
        seen.append(name)
        return original(name, fallback)

    monkeypatch.setattr(runtime, "_context_limit", capture)
    runtime._contract_initial_run_messages(_context())
    assert "task_max_tokens" in seen


def _job(job_id, *, conversation_id=None, correlation_id=None, status="waiting"):
    route = {}
    if conversation_id is not None:
        route["conversation_id"] = conversation_id
    if correlation_id is not None:
        route["root_correlation_id"] = correlation_id
    return SimpleNamespace(
        id=job_id,
        agent_id="agent",
        session_id=f"session-{job_id}",
        objective="work",
        status=SimpleNamespace(value=status),
        priority=20,
        parent_task_id=None,
        waiting_for="need input" if status == "waiting" else None,
        progress=[],
        result=None,
        error=None,
        created_at="t1",
        updated_at="t2",
        route_metadata=route,
        handoff_context=None,
    )


def test_waiting_subagents_are_filtered_to_current_conversation():
    class Manager:
        default_tools = []

        def list_agents(self):
            return []

        def list_jobs(self, **_kwargs):
            return [
                _job("same", conversation_id="cli:a"),
                _job("other", conversation_id="cli:b"),
            ]

    runtime = AgentRuntime(
        subagent_manager=Manager(),
        runtime_surfaces=("subagent",),
        action_ledger_path=":memory:",
    )
    context = _context("answer worker", conversation_id="cli:a")
    assert [item["id"] for item in runtime._waiting_subagent_jobs_for_context(context)] == [
        "same"
    ]


def test_related_subagent_snapshot_declares_when_it_is_truncated():
    class Manager:
        default_tools = []

        def list_agents(self):
            return []

        def list_jobs(self, *, limit=20, **_kwargs):
            return [
                _job(f"job-{index}", correlation_id="root", status="completed")
                for index in range(25)
            ][:limit]

    runtime = AgentRuntime(
        subagent_manager=Manager(),
        runtime_surfaces=("subagent",),
        action_ledger_path=":memory:",
    )
    event = Event(
        "subagent.completed",
        {"job_id": "job-1", "result": "done"},
        correlation_id="root",
    )
    snapshot = runtime._related_subagent_jobs_snapshot(event, limit=20)
    assert snapshot is not None
    assert snapshot["shown"] == 20
    assert snapshot["truncated"] is True
    assert snapshot["exhaustive"] is False


def test_related_subagent_snapshot_excludes_current_event_job_but_stays_authoritative():
    class Manager:
        default_tools = []

        def list_agents(self):
            return []

        def list_jobs(self, *, limit=20, **_kwargs):
            return [_job("current", correlation_id="root", status="completed")][:limit]

    runtime = AgentRuntime(
        subagent_manager=Manager(),
        runtime_surfaces=("subagent",),
        action_ledger_path=":memory:",
    )
    event = Event(
        "subagent.completed",
        {"job_id": "current", "result": "already present in event"},
        correlation_id="root",
    )

    snapshot = runtime._related_subagent_jobs_snapshot(event, limit=20)

    assert snapshot is not None
    assert snapshot["jobs"] == []
    assert snapshot["shown"] == 0
    assert snapshot["exhaustive"] is True


def test_current_subagent_status_is_explicitly_registry_state_not_job_execution():
    class Manager:
        default_tools = []

        def list_agents(self):
            return [
                SimpleNamespace(
                    id="agent-1",
                    name="researcher",
                    model="provider/model",
                    status=SimpleNamespace(value="active"),
                )
            ]

    runtime = AgentRuntime(
        subagent_manager=Manager(),
        runtime_surfaces=("subagent",),
        action_ledger_path=":memory:",
    )

    snapshot = runtime._current_subagents_snapshot()

    assert snapshot["status_semantics"] == "agent_registry_not_job_execution"
    assert snapshot["agents"][0]["registry_status"] == "active"
    assert snapshot["agents"][0]["enabled"] is True


def test_deferred_notification_is_marked_only_after_provider_payload_contains_it():
    runtime = AgentRuntime(action_ledger_path=":memory:", runtime_surfaces=("event",))
    context = _context()
    deferred = Event(
        "message",
        {"text": "later"},
        id="deferred-1",
        metadata={"channel": "cli", "conversation_id": "cli:main"},
    )
    runtime._deferred_event_index[deferred.id] = deferred

    runtime._append_pending_event_notifications(context)
    assert deferred.id not in context.notified_event_ids
    runtime._mark_pending_event_notifications_sent(context, [])
    assert deferred.id not in context.notified_event_ids
    runtime._mark_pending_event_notifications_sent(context, context.messages)
    assert deferred.id in context.notified_event_ids


def test_deferred_notifications_do_not_cross_conversations():
    runtime = AgentRuntime(action_ledger_path=":memory:", runtime_surfaces=("event",))
    context = _context(conversation_id="cli:a")
    same = Event(
        "message",
        {"text": "same-conversation"},
        id="deferred-same",
        metadata={"channel": "cli", "conversation_id": "cli:a"},
    )
    other = Event(
        "message",
        {"text": "other-conversation"},
        id="deferred-other",
        metadata={"channel": "cli", "conversation_id": "cli:b"},
    )
    runtime._deferred_event_index[same.id] = same
    runtime._deferred_event_index[other.id] = other

    runtime._append_pending_event_notifications(context)

    encoded = json.dumps(context.messages, ensure_ascii=False)
    assert same.id in encoded
    assert other.id not in encoded


def test_memory_query_uses_content_and_request_payloads():
    runtime = AgentRuntime(
        retrieval_store=object(), runtime_surfaces=(), action_ledger_path=":memory:"
    )
    for key in ("content", "request"):
        context = RunContext(
            event=Event("message", {key: f"query-from-{key}"}),
            task=None,
            run_id=None,
            loaded_state={},
        )
        components = runtime._optional_context_components(context)
        memory_query = next(item for item in components if item.name == "memory_query")
        assert memory_query.value == f"query-from-{key}"


def test_journal_keeps_plain_user_and_final_assistant_without_contract_wrappers(tmp_path):
    journal = ConversationJournal(tmp_path / "conversation.jsonl")
    runtime = AgentRuntime(
        conversation_journal=journal,
        context_mode="contract",
        action_ledger_path=":memory:",
    )
    context = _context("question canonique")
    context.messages = runtime._contract_initial_run_messages(context)
    context.answer = "réponse finale"

    runtime._journal_context(context)

    history = journal.recent_messages(conversation_id="cli:main", limit=20)
    assert [item["content"] for item in history] == ["question canonique", "réponse finale"]
    raw = (tmp_path / "conversation.jsonl").read_text(encoding="utf-8")
    assert "ORION_REQUEST_V1" not in raw
    assert "BEGIN_ORION_EVIDENCE" not in raw


def test_retry_same_event_can_complete_previously_journaled_error(tmp_path):
    journal = ConversationJournal(tmp_path / "conversation.jsonl")
    runtime = AgentRuntime(conversation_journal=journal, action_ledger_path=":memory:")
    event = Event(
        "message",
        {"text": "continue me"},
        metadata={"channel": "cli", "conversation_id": "cli:retry"},
        id="same-event",
    )
    failed = RunContext(event=event, task=None, run_id=None, loaded_state={})
    runtime._journal_context(failed, error=RuntimeError("boom"))
    succeeded = RunContext(event=event, task=None, run_id=None, loaded_state={})
    succeeded.answer = "done now"
    runtime._journal_context(succeeded)

    history = journal.recent_messages(conversation_id="cli:retry", limit=20)
    contents = [item["content"] for item in history]
    assert contents.count("continue me") == 1
    assert any("RUN a été interrompu" in item for item in contents)
    assert contents[-1] == "done now"


def test_sqlite_retry_same_event_can_complete_previously_journaled_error(tmp_path):
    journal = SQLiteConversationJournal(tmp_path / "conversation.sqlite3")
    try:
        runtime = AgentRuntime(conversation_journal=journal, action_ledger_path=":memory:")
        event = Event(
            "message",
            {"text": "continue sqlite"},
            metadata={"channel": "cli", "conversation_id": "cli:sqlite-retry"},
            id="same-sqlite-event",
        )
        failed = RunContext(event=event, task=None, run_id=None, loaded_state={})
        runtime._journal_context(failed, error=RuntimeError("boom"))
        succeeded = RunContext(event=event, task=None, run_id=None, loaded_state={})
        succeeded.answer = "sqlite done"
        runtime._journal_context(succeeded)

        contents = [
            item["content"]
            for item in journal.recent_messages(
                conversation_id="cli:sqlite-retry", limit=20
            )
        ]
        assert contents.count("continue sqlite") == 1
        assert any("RUN a été interrompu" in item for item in contents)
        assert contents[-1] == "sqlite done"
    finally:
        journal.close()


@pytest.mark.parametrize(
    ("event_type", "answer"),
    [
        ("subagent.failed", None),
        ("subagent.cancelled", "La délégation a été annulée."),
        ("subagent.waiting", "Le worker attend une précision."),
    ],
)
def test_taskless_conversational_terminal_or_waiting_states_are_journaled(
    tmp_path, event_type, answer
):
    journal = ConversationJournal(tmp_path / f"{event_type}.jsonl")
    runtime = AgentRuntime(conversation_journal=journal, action_ledger_path=":memory:")
    payload = {"job_id": "job-1"}
    if event_type.endswith("failed"):
        payload["error"] = "worker failed"
    if event_type.endswith("waiting"):
        payload["result"] = "need input"
    event = Event(
        event_type,
        payload,
        metadata={
            "conversation_id": "cli:delegated",
            "channel": "cli",
            "resume_orchestrator": True,
        },
    )
    context = RunContext(event=event, task=None, run_id=None, loaded_state={})
    context.answer = answer
    runtime._journal_context(context)
    messages = journal.recent_messages(conversation_id="cli:delegated", limit=10)
    assert messages
    assert messages[-1]["role"] == "assistant"


def test_action_specific_schemas_restrict_fields_and_status_vocabularies():
    class Subagents:
        default_tools = []

    class Team:
        pass

    runtime = AgentRuntime(
        subagent_manager=Subagents(),
        team_bus=Team(),
        runtime_surfaces=("task", "event", "subagent", "team"),
        action_ledger_path=":memory:",
    )
    definitions = {
        item["function"]["name"]: item["function"]["parameters"]
        for item in runtime._runtime_tool_definitions()
    }
    task = definitions["task"]
    create = next(item for item in task["oneOf"] if item["properties"]["action"]["const"] == "create")
    assert set(create["properties"]) == {"action", "objective", "priority"}
    task_list = next(item for item in task["oneOf"] if item["properties"]["action"]["const"] == "list")
    assert task_list["properties"]["status"]["enum"] == [
        "pending",
        "running",
        "waiting",
        "paused",
        "completed",
        "failed",
        "cancelled",
    ]
    plan_step = next(
        item for item in task["oneOf"] if item["properties"]["action"]["const"] == "update_plan_step"
    )
    assert plan_step["properties"]["status"]["enum"] == [
        "pending",
        "in_progress",
        "completed",
        "blocked",
        "skipped",
    ]
    assert definitions["subagent"]["properties"]["allowed_tools"]["maxItems"] == 0
    complete_job = next(
        item
        for item in definitions["team"]["oneOf"]
        if item["properties"]["action"]["const"] == "complete_job"
    )
    assert "false publishes a failed completion" in complete_job["properties"]["success"]["description"]


def test_bare_wait_is_rejected_and_wait_any_is_explicitly_accepted():
    runtime = AgentRuntime(action_ledger_path=":memory:")
    with pytest.raises(ValueError, match="wait_any=true"):
        runtime._normalize_runtime_tool_call("task", {"action": "wait"})
    operation, arguments = runtime._normalize_runtime_tool_call(
        "task", {"action": "wait", "wait_any": True}
    )
    assert operation == "wait_for_event"
    assert arguments["wait_any"] is True
    with pytest.raises(ValueError, match="doit valoir true"):
        runtime._normalize_runtime_tool_call(
            "task", {"action": "wait", "event_type": "message", "wait_any": False}
        )


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("task", {"action": "list", "status": "in_progress"}),
        ("task", {"action": "update_plan_step", "step_id": "s1", "status": "running"}),
        ("subagent", {"action": "update", "agent_id": "a", "status": "running"}),
        ("subagent", {"action": "list_jobs", "status": "active"}),
        ("team", {"action": "complete_job", "job_id": "j", "result": "x", "success": "false"}),
    ],
)
def test_action_specific_vocabularies_are_also_validated_at_runtime(tool, arguments):
    runtime = AgentRuntime(action_ledger_path=":memory:")
    with pytest.raises(ValueError):
        runtime._normalize_runtime_tool_call(tool, arguments)


def test_runtime_guidance_does_not_claim_task_list_is_history_and_ack_is_after_success():
    runtime = AgentRuntime(
        runtime_surfaces=("task", "event"), action_ledger_path=":memory:"
    )
    controls = runtime._runtime_control_instructions()
    assert "l'état actuel des tâches" in controls
    assert "ne prouve pas" in controls
    assert "acquitte-la ensuite" in controls
    assert "jamais avant le traitement réussi" in controls
