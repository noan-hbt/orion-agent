from types import SimpleNamespace

from cli_cockpit_actions import execute_action


class Service:
    def __init__(self):
        self.calls = []

    def create_agent(self, **kw):
        self.calls.append(("spawn", kw))
        return {"id": "a1"}

    def delete_agent(self, agent_id):
        self.calls.append(("kill", agent_id))
        return {"id": agent_id}

    def submit(self, objective, *, agent_id=None, **kw):
        self.calls.append(("delegate", agent_id, objective, kw))
        return {"id": "j1"}

    def send_message(self, job_id, message, *, caller_scope=None):
        self.calls.append(("send", job_id, message))
        return {"id": job_id}


class TeamService:
    def __init__(self):
        self.calls = []

    def send(self, **kwargs):
        self.calls.append(kwargs)
        return {"id": "message-1"}


def test_subagent_actions_are_explicit_and_serialized():
    service = Service()
    app = SimpleNamespace(subagent_manager=service)
    assert execute_action(app, "/spawn", {"name": "worker"})["ok"]
    assert service.calls[0][1]["allowed_tools"] is None
    assert execute_action(app, "kill", {"agent_id": "a1"})["data"] == {"id": "a1"}
    assert execute_action(app, "delegate", {"agent_id": "a1", "objective": "inspect"})[
        "ok"
    ]
    assert execute_action(app, "send", {"job_id": "j1", "message": "done"})["ok"]


def test_spawn_preserves_explicit_empty_tool_restriction():
    service = Service()
    result = execute_action(
        SimpleNamespace(subagent_manager=service),
        "/spawn",
        {"name": "isolated", "allowed_tools": []},
    )
    assert result["ok"]
    assert service.calls[0][1]["allowed_tools"] == []


def test_approve_never_infers_or_auto_approves():
    result = execute_action(SimpleNamespace(), "approve", {"approval_id": "missing"})
    assert not result["ok"]
    assert "indisponible" in result["error"]


def test_unknown_and_unsupported_actions_fail_cleanly():
    assert not execute_action({}, "spawn", {})["ok"]
    assert not execute_action({}, "autonomy", {"autonomy": 3})["ok"]
    assert not execute_action({}, "nope", {})["ok"]


def test_positional_spawn_does_not_reuse_name_as_optional_fields():
    service = Service()

    result = execute_action(SimpleNamespace(subagent_manager=service), "spawn", ["worker"])

    assert result["ok"] is True
    kwargs = service.calls[-1][1]
    assert kwargs["name"] == "worker"
    assert kwargs["allowed_tools"] is None
    assert kwargs["capabilities"] == []
    assert kwargs["max_turns"] == 8


def test_positional_delegate_supports_objective_only_and_agent_plus_objective():
    service = Service()
    app = SimpleNamespace(subagent_manager=service)

    assert execute_action(app, "delegate", ["inspect", "logs"])["ok"]
    assert service.calls[-1][1:3] == ("inspect", "logs")

    assert execute_action(app, "delegate", ["summarize"])["ok"]
    assert service.calls[-1][1:3] == (None, "summarize")


def test_positional_team_send_keeps_recipient_body_and_default_priority_distinct():
    teams = TeamService()

    result = execute_action(
        SimpleNamespace(team_bus=teams),
        "send",
        ["worker-7", "analyse", "repo"],
    )

    assert result["ok"] is True
    assert teams.calls == [
        {
            "recipient": "worker-7",
            "body": "analyse repo",
            "subject": "",
            "priority": 20,
        }
    ]


def test_required_positional_arguments_return_usage_errors():
    service = Service()
    app = SimpleNamespace(subagent_manager=service)

    assert execute_action(app, "spawn", [])["error"] == "Usage: /spawn <name>"
    assert execute_action(app, "kill", [])["error"] == "Usage: /kill <agent_id>"
    assert execute_action(app, "delegate", [])["error"] == "Usage: /delegate [agent_id] <objective>"
