import json

from action_ledger import ActionLedger
from event_handler import Event
from runtime import AgentRuntime, RunContext
from tool_policy import ToolPolicy


class _LLM:
    model = "openai/gpt-4o-mini"

    def tool_definitions(self):
        return []

    def get_registered_tool(self, _name):
        return None


class _LLMWithLegacyDefinition(_LLM):
    def tool_definitions(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": "create_task",
                    "description": "must stay hidden",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "external_read",
                    "description": "external",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]


class _LLMWithWeb(_LLM):
    def tool_definitions(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": "web",
                    "description": "web search",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]


class _Subagents:
    default_tools = ["read_file", "search_files"]


class _TeamBus:
    pass


def _names(runtime):
    return [item["function"]["name"] for item in runtime._runtime_tool_definitions()]


def _definition(runtime, name):
    return next(
        item for item in runtime._runtime_tool_definitions() if item["function"]["name"] == name
    )


def test_runtime_definitions_expose_only_consolidated_active_surfaces():
    runtime = AgentRuntime(
        action_ledger_path=":memory:",
        history_enabled=False,
        subagent_manager=_Subagents(),
        team_bus=_TeamBus(),
        runtime_surfaces=("task", "event", "subagent", "team"),
    )

    assert _names(runtime) == ["task", "event", "subagent", "team"]
    legacy_names = {
        item["function"]["name"] for item in runtime._legacy_runtime_tool_definitions()
    }
    assert "create_task" in legacy_names
    assert "create_subagent" in legacy_names
    assert "delegate_team_job" in legacy_names
    assert legacy_names.isdisjoint(_names(runtime))


def test_external_registry_cannot_reexpose_hidden_legacy_runtime_names():
    runtime = AgentRuntime(
        llm_client=_LLMWithLegacyDefinition(),
        action_ledger_path=":memory:",
        history_enabled=False,
        runtime_surfaces=("task",),
    )

    names = [item["function"]["name"] for item in runtime._tool_definitions()]

    assert "task" in names
    assert "external_read" in names
    assert "create_task" not in names


def test_action_schemas_are_consolidated_and_schedule_is_capability_gated():
    runtime = AgentRuntime(
        action_ledger_path=":memory:",
        history_enabled=False,
        subagent_manager=_Subagents(),
        team_bus=_TeamBus(),
        runtime_surfaces=("task", "subagent", "team"),
    )

    task_actions = _definition(runtime, "task")["function"]["parameters"]["properties"]["action"]["enum"]
    assert task_actions == [
        "create",
        "get",
        "list",
        "bind",
        "set_plan",
        "update_plan_step",
        "update_state",
        "wait",
        "complete",
    ]
    subagent_actions = _definition(runtime, "subagent")["function"]["parameters"]["properties"]["action"]["enum"]
    assert subagent_actions == [
        "create",
        "update",
        "delete",
        "get",
        "list",
        "delegate",
        "get_job",
        "get_session",
        "list_jobs",
        "cancel_job",
        "send",
        "pause_job",
        "resume_job",
    ]
    team_actions = _definition(runtime, "team")["function"]["parameters"]["properties"]["action"]["enum"]
    assert team_actions == ["list_messages", "send", "delegate", "get_job", "complete_job"]

    runtime.scheduler = object()
    task_actions = _definition(runtime, "task")["function"]["parameters"]["properties"]["action"]["enum"]
    assert task_actions[-1] == "schedule"


def test_runtime_controls_only_describe_active_consolidated_surfaces():
    empty = AgentRuntime(
        action_ledger_path=":memory:",
        history_enabled=False,
        runtime_surfaces=(),
        subagent_manager=_Subagents(),
        team_bus=_TeamBus(),
    )
    assert _names(empty) == []
    empty_controls = empty._runtime_control_instructions()
    assert "task(action=" not in empty_controls
    assert "subagent(action=" not in empty_controls
    assert "team(action=" not in empty_controls
    assert "sous-agent" not in empty_controls.lower()
    assert "recherche web" not in empty._system_instructions().lower()

    subagent_only = AgentRuntime(
        action_ledger_path=":memory:",
        history_enabled=False,
        runtime_surfaces=("subagent",),
        subagent_manager=_Subagents(),
        team_bus=_TeamBus(),
    )
    controls = subagent_only._runtime_control_instructions()
    assert "subagent(action=" in controls
    assert "traite ce retour comme un delta" in controls
    assert "Ne répète pas les statuts" in controls
    assert "task(action=" not in controls
    assert "team(action=" not in controls

    with_web = AgentRuntime(
        llm_client=_LLMWithWeb(),
        action_ledger_path=":memory:",
        history_enabled=False,
        runtime_surfaces=(),
    )
    assert "recherche web" in with_web._system_instructions().lower()


def test_consolidated_task_side_effect_keeps_legacy_action_ledger_identity():
    llm = _LLM()
    ledger = ActionLedger(":memory:")
    runtime = AgentRuntime(
        llm_client=llm,
        action_ledger=ledger,
        history_enabled=False,
        runtime_surfaces=("task",),
    )
    runtime._run_context = RunContext(
        Event("message", {"text": "remember this"}), None, None, {}
    )
    call = {
        "id": "task-create-1",
        "type": "function",
        "function": {
            "name": "task",
            "arguments": json.dumps({"action": "create", "objective": "durable objective"}),
        },
    }

    result = json.loads(runtime._execute_tool(call)["content"])

    assert result["objective"] == "durable objective"
    records = ledger.recent(operation="create_task")
    assert len(records) == 1
    assert records[0].status == "succeeded"
    assert ledger.recent(operation="task") == []


def test_runtime_state_reads_never_inherit_fail_closed_policy_side_effect_classification():
    runtime = AgentRuntime(
        llm_client=_LLM(),
        action_ledger_path=":memory:",
        tool_policy=ToolPolicy(approvals_enabled=False),
        history_enabled=False,
        subagent_manager=_Subagents(),
        team_bus=_TeamBus(),
        runtime_surfaces=("task", "event", "subagent", "team"),
    )
    runtime_names = runtime._runtime_tool_names()
    reads = {
        "get_task",
        "list_tasks",
        "get_subagent",
        "list_subagents",
        "get_subagent_job",
        "get_subagent_session",
        "list_subagent_jobs",
        "list_team_messages",
        "get_team_job",
    }

    assert reads == runtime._READ_ONLY_RUNTIME_TOOLS
    for operation in reads:
        assert operation in runtime_names
        assert runtime._tool_is_side_effect(operation, runtime_names)[0] is False

    for operation in {
        "create_task",
        "bind_task",
        "delete_subagent",
        "delegate_to_subagent",
        "send_team_message",
        "acknowledge_pending_event",
    }:
        assert operation in runtime_names
        assert runtime._tool_is_side_effect(operation, runtime_names)[0] is True


def test_invalid_consolidated_action_is_rejected_before_action_ledger():
    class _NoReserve:
        def reserve(self, *args, **kwargs):
            raise AssertionError("invalid action must not reach ActionLedger")

    runtime = AgentRuntime(
        llm_client=_LLM(),
        action_ledger=_NoReserve(),
        history_enabled=False,
        runtime_surfaces=("task",),
    )
    runtime._run_context = RunContext(Event("message", {"text": "x"}), None, None, {})
    call = {
        "id": "bad-action",
        "type": "function",
        "function": {
            "name": "task",
            "arguments": json.dumps({"action": "create", "priority": 20}),
        },
    }

    result = json.loads(runtime._execute_tool(call)["content"])

    assert result["invalid_arguments"] is True
    assert result["retryable"] is True
    assert result["executed"] is False


def test_taskless_wait_is_recoverable_before_action_ledger_and_without_user_warning():
    class _NoReserve:
        def reserve(self, *args, **kwargs):
            raise AssertionError("taskless task precondition must not reach ActionLedger")

    outputs = []
    runtime = AgentRuntime(
        llm_client=_LLM(),
        action_ledger=_NoReserve(),
        history_enabled=False,
        runtime_surfaces=("task", "subagent"),
        subagent_manager=_Subagents(),
        on_output=outputs.append,
    )
    runtime._run_context = RunContext(
        Event("message", {"text": "delegate this"}), None, None, {}
    )
    call = {
        "id": "taskless-wait",
        "type": "function",
        "function": {
            "name": "task",
            "arguments": json.dumps(
                {
                    "action": "wait",
                    "event_type": "subagent.terminal",
                    "payload_equals": {"job_id": "job-1"},
                }
            ),
        },
    }

    result = json.loads(runtime._execute_tool(call)["content"])

    assert result == {
        "executed": False,
        "precondition_failed": True,
        "retryable": True,
        "reason": "task_not_bound",
        "guidance": (
            "Cette action nécessite une tâche durable liée au RUN. "
            "Pour une délégation conversationnelle taskless, termine le tour : "
            "le retour du sous-agent réveillera Orion automatiquement."
        ),
    }
    assert outputs == []


def test_action_schemas_expose_machine_readable_required_fields_per_action():
    runtime = AgentRuntime(
        action_ledger_path=":memory:",
        history_enabled=False,
        subagent_manager=_Subagents(),
        team_bus=_TeamBus(),
        runtime_surfaces=("task", "event", "subagent", "team"),
    )

    subagent = _definition(runtime, "subagent")["function"]["parameters"]
    create_rule = next(
        item for item in subagent["allOf"]
        if item["if"]["properties"]["action"]["const"] == "create"
    )
    send_rule = next(
        item for item in subagent["allOf"]
        if item["if"]["properties"]["action"]["const"] == "send"
    )
    assert create_rule["then"]["required"] == ["action", "description", "name"]
    assert send_rule["then"]["required"] == ["action", "job_id", "message"]

    task = _definition(runtime, "task")["function"]["parameters"]
    task_create = next(
        item for item in task["allOf"]
        if item["if"]["properties"]["action"]["const"] == "create"
    )
    assert task_create["then"]["required"] == ["action", "objective"]

    event = _definition(runtime, "event")["function"]["parameters"]
    assert event["allOf"][0]["then"]["required"] == ["action", "event_id"]
