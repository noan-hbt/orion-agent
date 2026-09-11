import json

from action_ledger import ActionLedger
from event_handler import Event, EventHandler
from runtime import AgentRuntime, RunContext
from subagents import SubAgentManager
from tool_policy import ToolPolicy


class _LLM:
    model = "openai/gpt-4o-mini"

    def tool_definitions(self):
        return []

    def get_registered_tool(self, _name):
        return None


def _call(arguments):
    return {
        "id": "create-1",
        "type": "function",
        "function": {
            "name": "subagent",
            "arguments": json.dumps({"action": "create", **arguments}),
        },
    }


def _named_call(name, arguments, *, call_id="call-1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments),
        },
    }


def _runtime(tmp_path):
    llm = _LLM()
    ledger = ActionLedger(":memory:")
    manager = SubAgentManager(
        llm,
        EventHandler(workers=0),
        state_path=tmp_path / "subagents.json",
        default_tools=["web", "files"],
        action_ledger=ledger,
        emit_progress_events=False,
    )
    runtime = AgentRuntime(
        llm_client=llm,
        subagent_manager=manager,
        action_ledger=ledger,
        history_enabled=False,
    )
    runtime._run_context = RunContext(
        Event("message", {"text": "create worker"}),
        None,
        None,
        {},
    )
    return runtime, manager, ledger


def test_create_subagent_schema_exposes_only_callable_ceiling(tmp_path):
    runtime, _manager, _ledger = _runtime(tmp_path)
    definition = next(
        item
        for item in runtime._runtime_tool_definitions()
        if item["function"]["name"] == "subagent"
    )

    parameters = definition["function"]["parameters"]
    assert parameters["required"] == ["action"]
    assert parameters["properties"]["action"]["enum"] == [
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
    allowed = parameters["properties"]["allowed_tools"]
    assert allowed["items"]["enum"] == [
        "web",
        "files",
    ]
    assert "Omit allowed_tools" in allowed["description"]
    exposed = {item["function"]["name"] for item in runtime._runtime_tool_definitions()}
    assert "create_subagent" not in exposed
    assert "delete_subagent" not in exposed
    assert "delegate_to_subagent" not in exposed


def test_invalid_create_subagent_tools_fail_retryably_before_uncertain(tmp_path):
    runtime, manager, ledger = _runtime(tmp_path)
    invalid = {
        "name": "toml-explorer",
        "description": "inspect TOML files",
        "allowed_tools": ["orion.files"],
    }

    result = json.loads(runtime._execute_tool(_call(invalid))["content"])

    assert result["executed"] is False
    assert result["invalid_arguments"] is True
    assert result["retryable"] is True
    assert result["reason"] == "validation_error"
    assert result["ledger_status"] == "failed"
    assert manager.list_agents() == []
    record = ledger.get(result["action_key"])
    assert record is not None and record.status == "failed"

    corrected = {
        "name": "toml-explorer",
        "description": "inspect TOML files",
        "allowed_tools": ["files"],
    }
    created = json.loads(runtime._execute_tool(_call(corrected))["content"])
    assert created["kind"] == "subagent"
    assert created["allowed_tools"] == ["files"]
    assert [agent.name for agent in manager.list_agents()] == ["toml-explorer"]


def test_legacy_uncertain_validation_row_is_healed_on_retry(tmp_path):
    runtime, manager, ledger = _runtime(tmp_path)
    invalid = {
        "name": "toml-explorer",
        "description": "inspect TOML files",
        "allowed_tools": ["orion.files"],
    }
    reservation = ledger.reserve("create_subagent", invalid)
    ledger.mark_uncertain(
        reservation.action_key,
        "Tools interdits pour ce sous-agent : orion.files. Plafond configuré : read_file.",
        owner_id=reservation.owner_id,
        fence_token=reservation.fence_token,
    )

    result = json.loads(runtime._execute_tool(_call(invalid))["content"])

    assert result["reason"] == "validation_error"
    assert result["retryable"] is True
    assert result["ledger_status"] == "failed"
    assert manager.list_agents() == []
    assert ledger.get(reservation.action_key).status == "failed"


def test_legacy_uncertain_delete_is_retried_when_agent_still_exists(tmp_path):
    runtime, manager, ledger = _runtime(tmp_path)
    agent = manager.create_agent("agent-test", "test agent")
    arguments = {"agent_id": agent.id}
    reservation = ledger.reserve(
        "delete_subagent",
        arguments,
        target=agent.id,
    )
    ledger.mark_uncertain(
        reservation.action_key,
        "tool dispatch exception",
        owner_id=reservation.owner_id,
        fence_token=reservation.fence_token,
    )

    call = _named_call(
        "subagent",
        {"action": "delete", **arguments},
        call_id="delete-1",
    )
    result = json.loads(runtime._execute_tool(call)["content"])

    assert result["deleted"] is True
    assert result["agent_id"] == agent.id
    assert manager.get_agent(agent.id) is None
    assert ledger.get(reservation.action_key).status == "succeeded"


def test_legacy_uncertain_delete_is_confirmed_without_redispatch_when_agent_is_gone(tmp_path):
    runtime, manager, ledger = _runtime(tmp_path)
    # Opaque ids are never operator-selected or reused by create_agent, so an
    # absent exact id is sufficient source-of-truth for reconciliation.
    missing_agent_id = "abcdef123456"
    arguments = {"agent_id": missing_agent_id}
    reservation = ledger.reserve(
        "delete_subagent",
        arguments,
        target=missing_agent_id,
    )
    ledger.mark_uncertain(
        reservation.action_key,
        "tool dispatch exception",
        owner_id=reservation.owner_id,
        fence_token=reservation.fence_token,
    )

    call = _named_call(
        "subagent",
        {"action": "delete", **arguments},
        call_id="delete-2",
    )
    result = json.loads(runtime._execute_tool(call)["content"])

    assert result["executed"] is False
    assert result["duplicate"] is True
    assert result["previous_result"]["deleted"] is True
    assert result["previous_result"]["agent_id"] == missing_agent_id
    assert result["previous_result"]["reconciled"] is True
    assert manager.list_agents() == []
    assert ledger.get(reservation.action_key).status == "succeeded"


def test_delete_subagent_accepts_unique_agent_name(tmp_path):
    runtime, manager, _ledger = _runtime(tmp_path)
    agent = manager.create_agent("toml-analyst", "inspect TOML")

    result = json.loads(
        runtime._execute_tool(
            _named_call(
                "subagent",
                {"action": "delete", "agent_id": "toml-analyst"},
                call_id="delete-by-name",
            )
        )["content"]
    )

    assert result["deleted"] is True
    assert result["agent_id"] == agent.id
    assert manager.list_agents() == []


def test_list_subagents_is_fresh_after_delete_and_never_ledger_deduped(tmp_path):
    runtime, manager, ledger = _runtime(tmp_path)
    # Production wiring has a ToolPolicy. Its fail-closed rule for unknown
    # internal operation names used to misclassify list_subagents as a side
    # effect and return ActionLedger's stale cached result here.
    runtime.tool_policy = ToolPolicy(approvals_enabled=False)
    agent = manager.create_agent("cleanup-agent", "temporary cleanup worker")
    list_call = _named_call(
        "subagent",
        {"action": "list"},
        call_id="list-before-delete",
    )

    before = json.loads(runtime._execute_tool(list_call)["content"])
    assert [item["id"] for item in before["subagents"]] == [agent.id]

    deleted = json.loads(
        runtime._execute_tool(
            _named_call(
                "subagent",
                {"action": "delete", "agent_id": agent.id},
                call_id="delete-cleanup-agent",
            )
        )["content"]
    )
    assert deleted["deleted"] is True

    list_call["id"] = "list-after-delete"
    after = json.loads(runtime._execute_tool(list_call)["content"])

    assert after == {"subagents": []}
    assert "duplicate" not in after
    assert "previous_result" not in after
    assert "previous_at" not in after
    assert ledger.recent(operation="list_subagents") == []


def test_delete_unknown_subagent_is_retryable_not_uncertain(tmp_path):
    runtime, manager, ledger = _runtime(tmp_path)

    result = json.loads(
        runtime._execute_tool(
            _named_call(
                "subagent",
                {"action": "delete", "agent_id": "missing-worker"},
                call_id="delete-missing",
            )
        )["content"]
    )

    assert result["executed"] is False
    assert result["invalid_arguments"] is True
    assert result["retryable"] is True
    assert result["reason"] == "validation_error"
    assert result["ledger_status"] == "failed"
    assert manager.list_agents() == []
    assert ledger.get(result["action_key"]).status == "failed"


def test_legacy_subagent_name_remains_executable_but_not_model_visible(tmp_path):
    runtime, manager, _ledger = _runtime(tmp_path)

    created = runtime._execute_runtime_tool(
        "create_subagent",
        {"name": "legacy-worker", "description": "persisted replay"},
    )

    assert created["kind"] == "subagent"
    assert [agent.name for agent in manager.list_agents()] == ["legacy-worker"]
    exposed = {item["function"]["name"] for item in runtime._runtime_tool_definitions()}
    assert "create_subagent" not in exposed
    assert "subagent" in exposed


def test_delegate_subagent_accepts_unique_agent_name_without_uncertain(tmp_path):
    runtime, manager, _ledger = _runtime(tmp_path)
    agent = manager.create_agent("demo-worker", "demo worker")
    call = _named_call(
        "subagent",
        {"action": "delegate", "agent_id": "demo-worker", "objective": "inspect state"},
        call_id="delegate-by-name",
    )

    result = json.loads(runtime._execute_tool(call)["content"])

    assert result.get("uncertain") is not True
    assert result["agent_id"] == agent.id
    jobs = manager.list_jobs()
    assert len(jobs) == 1 and jobs[0].agent_id == agent.id
