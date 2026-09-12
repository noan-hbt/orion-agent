import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from cli_cockpit_backend import CockpitBackend


class Runtime:
    state = "sleep"
    running = False


class Tasks:
    def list(self): return [{"id": 1, "status": "running"}]


class Tools:
    def installed(self): return [({"id": "web", "version": "1.0"}, Path("tools/web"))]


class Memory:
    def search(self, query, *, limit):
        assert query == ""
        assert limit == 20
        return [{"id": "m1", "content": "remembered"}]


class Context:
    def snapshot(self): return {"threads": [{"id": "t1"}]}


class Usage:
    def snapshot(self): return {"known_cost_usd": Decimal("0.25"), "total_tokens": 42}


def test_snapshot_uses_real_services():
    result = CockpitBackend({"runtime": Runtime(), "task_store": Tasks(), "workspace": "repo"}).snapshot()
    assert result["runtime"]["state"] == "sleep"
    assert result["tasks"] == [{"id": 1, "status": "running"}]


def test_unavailable_and_unknown_are_explicit():
    backend = CockpitBackend({})
    assert backend.execute("/events")["error"]
    assert backend.execute("/wat") ["error"]


def test_command_contract():
    result = CockpitBackend({"workspace": "x"}).execute("/workspace")
    assert set(result) >= {"title", "data"}
    assert result["data"] == "x"


def test_help_is_descriptive_and_supports_command_specific_lookup():
    backend = CockpitBackend({"runtime": Runtime()})

    overview = backend.execute("/help")
    assert overview.get("error") is None
    commands = overview["data"]["commands"]
    status = next(item for item in commands if item["command"] == "status")
    assert status["usage"] == "/status"
    assert status["description"]
    assert status["available"] is True

    targeted = backend.execute("/help status")
    assert targeted.get("error") is None
    assert targeted["data"]["command"] == "status"
    assert targeted["data"]["usage"] == "/status"
    assert targeted["data"]["description"] == status["description"]
    assert targeted["data"]["available"] is True


def test_snapshot_projects_pending_approvals_without_raw_secrets_or_objects():
    secret = "approval-secret-must-not-leak"

    class Approvals:
        def pending(self):
            return [
                {
                    "id": "approval-1",
                    "status": "pending",
                    "requester": "runtime",
                    "scope": "tool:terminal",
                    "created_at": "2026-09-10T12:00:00+00:00",
                    "expires_at": None,
                    "payload": {
                        "tool_id": "terminal",
                        "args_hash": "a" * 64,
                        "preview": {"kind": "terminal", "command": "echo reviewed"},
                        "password": secret,
                        "internal": {"secret": secret},
                    },
                }
            ]

    class RawSecretObject:
        def __repr__(self):
            return f"RawSecretObject({secret})"

    snapshot = CockpitBackend(
        {
            "approval_store": Approvals(),
            "memory": RawSecretObject(),
            "galaxy": RawSecretObject(),
        }
    ).snapshot()

    assert len(snapshot["approvals"]) == 1
    approval = snapshot["approvals"][0]
    assert approval["id"] == "approval-1"
    assert approval["tool_id"] == "terminal"
    assert approval["preview"] == {"kind": "terminal", "command": "echo reviewed"}
    encoded = json.dumps(snapshot, sort_keys=True, default=str)
    assert secret not in encoded
    assert "RawSecretObject" not in encoded


def test_commands_resolve_application_service_facade():
    backend = CockpitBackend({
        "task_store": Tasks(),
        "tool_manager": Tools(),
        "retrieval_store": Memory(),
        "context_registry": Context(),
        "usage_ledger": Usage(),
    })

    tasks_result = backend.execute("/tasks")
    assert tasks_result["data"] == [{"id": 1, "status": "running"}]
    assert "#1 [running]" in tasks_result["display"]
    tools_result = backend.execute("/tools")
    tools = tools_result["data"]
    assert len(tools) == 1
    assert tools[0]["id"] == "web"
    assert tools[0]["version"] == "1.0"
    assert tools[0]["installed"] is True
    assert tools[0]["enabled"] is False
    assert tools[0]["status"] == "installed_not_enabled"
    assert "tools/web" not in str(tools).replace("\\", "/")
    assert "web [installed_not_enabled]" in tools_result["display"]
    memory_result = backend.execute("/memory")
    assert memory_result["data"] == [{"id": "m1", "content": "remembered"}]
    assert "remembered" in memory_result["display"]
    context_result = backend.execute("/context")
    assert context_result["data"] == {"threads": 1}
    assert context_result["display"] == "threads: 1"
    assert backend.execute("/cost")["data"] == {"known_cost_usd": "0.25", "total_tokens": 42}


def test_tools_projection_requires_explicit_package_enable_after_install():
    class ConfiguredTools(Tools):
        config = {"enabled": ["web"], "disabled": []}

        def loaded_guidance(self):
            return {"web": object()}

    rows = CockpitBackend({"tool_manager": ConfiguredTools()}).execute("/tools")["data"]

    assert rows[0]["id"] == "web"
    assert rows[0]["enabled"] is True
    assert rows[0]["loaded"] is True
    assert rows[0]["status"] == "loaded"


def test_command_display_contract_is_compact_human_text_while_data_stays_structured():
    class Agents:
        def list_agents(self):
            return [
                {
                    "id": "agent-1",
                    "name": "reviewer",
                    "status": "active",
                    "model": "provider/model",
                    "allowed_tools": ["web"],
                    "capabilities": ["audit"],
                }
            ]

        def list_jobs(self):
            return [
                {
                    "id": "job-1",
                    "agent_id": "agent-1",
                    "status": "running",
                    "objective": "inspect the repository",
                    "result": {"must": "not render"},
                }
            ]

    class Events:
        running = True
        workers = 2
        queue = None
        dead_letters = []
        callback_errors = []

    backend = CockpitBackend(
        {
            "runtime": Runtime(),
            "task_store": Tasks(),
            "subagents": Agents(),
            "events": Events(),
            "context_registry": Context(),
        }
    )

    agents = backend.execute("/agents")
    assert isinstance(agents["data"], list)
    assert "reviewer [active]" in agents["display"]
    assert "model=provider/model" in agents["display"]

    jobs = backend.execute("/jobs")
    assert jobs["data"] == [
        {
            "id": "job-1",
            "status": "running",
            "agent_id": "agent-1",
            "objective": "inspect the repository",
        }
    ]
    assert "job-1 [running] inspect the repository" in jobs["display"]
    assert "must" not in jobs["display"]

    events = backend.execute("/events")
    assert isinstance(events["data"], dict)
    assert "state: running=yes" in events["display"]
    assert "queue: queued=-" in events["display"]
    assert "{" not in events["display"]

    status = backend.execute("/status")
    assert isinstance(status["data"], dict)
    assert "RUNTIME" in status["display"]
    assert "TASKS" in status["display"]
    assert "AGENTS" in status["display"]
    assert "EVENTS" in status["display"]


def test_empty_collection_commands_have_explicit_empty_states():
    class EmptyTasks:
        def list(self):
            return []

    class EmptyAgents:
        def list_agents(self):
            return []

        def list_jobs(self):
            return []

    backend = CockpitBackend({"task_store": EmptyTasks(), "subagents": EmptyAgents()})

    assert backend.execute("/tasks")["display"] == "Aucune tâche."
    assert backend.execute("/agents")["display"] == "Aucun sous-agent."
    assert backend.execute("/jobs")["display"] == "Aucun travail délégué."


def test_empty_context_is_available_and_not_reported_as_missing_provider():
    class EmptyContext:
        def snapshot(self, limit=100):
            assert limit == 100
            return {"threads": [], "bindings": []}

    result = CockpitBackend({"context_registry": EmptyContext()}).execute("/context")

    assert result.get("error") is None
    assert result["data"] == {"threads": 0, "bindings": 0}
    assert "threads: 0" in result["display"]


def test_help_display_groups_available_and_unavailable_commands():
    result = CockpitBackend({"runtime": Runtime()}).execute("/help")

    assert result.get("error") is None
    assert "AVAILABLE" in result["display"]
    assert "/status" in result["display"]
    assert "UNAVAILABLE" in result["display"]
    assert "/tasks" in result["display"]
    assert "ALIASES" in result["display"]


def test_snapshot_falls_back_to_exposed_context_memory_and_usage_services():
    result = CockpitBackend({
        "runtime": Runtime(),
        "retrieval_store": Memory(),
        "context_registry": Context(),
        "usage_ledger": Usage(),
    }).snapshot()

    # Status/dashboard keeps only structural Context OS metadata and does not
    # dump memory contents. Explicit /memory remains available separately.
    assert "memory" not in result
    assert result["context"] == {"threads": 1}
    assert result["usage"]["total_tokens"] == 42
    assert result["cost"]["known_cost_usd"] == "0.25"


def test_tasks_agents_context_and_workspace_use_public_snapshot_projections():
    secret = "snapshot-secret-must-not-leak"

    class TaskRows:
        def list(self):
            return [
                {
                    "id": 7,
                    "objective": "safe objective",
                    "status": "running",
                    "current_state": {"token": secret},
                    "history": [{"error": secret}],
                    "actions": [{"result": secret}],
                }
            ]

    class AgentRows:
        def list_agents(self):
            return [
                {
                    "id": "agent-1",
                    "name": "worker",
                    "model": "provider/model",
                    "status": "active",
                    "system_prompt": secret,
                    "allowed_tools": ["web_search"],
                    "capabilities": ["research"],
                }
            ]

    class ContextRows:
        def snapshot(self, limit=100):
            assert limit == 100
            return {
                "threads": [{"id": "thread-1", "data": {"token": secret}}],
                "bindings": [{"external_id": secret, "data": {"secret": secret}}],
            }

    class WorkspaceSecret:
        def __repr__(self):
            return f"WorkspaceSecret({secret})"

    snapshot = CockpitBackend(
        {
            "task_store": TaskRows(),
            "subagents": AgentRows(),
            "context_registry": ContextRows(),
            "workspace": WorkspaceSecret(),
        }
    ).snapshot()

    assert snapshot["tasks"] == [{"id": 7, "objective": "safe objective", "status": "running"}]
    assert snapshot["agents"] == [
        {
            "id": "agent-1",
            "name": "worker",
            "model": "provider/model",
            "status": "active",
            "capability_count": 1,
            "tool_count": 1,
        }
    ]
    assert snapshot["context"] == {"threads": 1, "bindings": 1}
    assert snapshot["workspace"] is None
    assert secret not in json.dumps(snapshot, sort_keys=True, default=str)
