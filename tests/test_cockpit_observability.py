import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from cli_cockpit_observability import collect_observability


class Service:
    def __init__(self, value):
        self.value = value

    def snapshot(self):
        return self.value


class Runtime:
    state = "running"
    running = True


def test_collects_real_services_and_preserves_statuses():
    app = {
        "runtime": Runtime(),
        "model": Service({"name": "gpt-test", "provider": "openrouter"}),
        "usage": Service({"input_tokens": 12, "output_tokens": 8}),
        "cost": Service({"total": 0.03, "currency": "USD"}),
        "context": Service({"used": 20, "limit": 100}),
        "subagent_store": Service([
            {"id": "a1", "status": "running"},
            {"id": "a2", "status": "completed"},
        ]),
        "task_store": Service([
            {"id": 1, "status": "running"},
            {"id": 2, "status": "done"},
        ]),
    }
    result = collect_observability(app)
    assert result["runtime"] == {"state": "running", "running": True}
    assert result["model"]["name"] == "gpt-test"
    assert result["usage"]["input_tokens"] == 12
    assert result["cost"]["total"] == 0.03
    assert [x["status"] for x in result["agents"]] == ["running", "completed"]
    assert [x["status"] for x in result["tasks"]] == ["running", "done"]


def test_absent_services_are_explicitly_unavailable():
    result = collect_observability({})
    for name in ("runtime", "model", "usage", "cost", "context", "agents",
                 "tasks", "events", "memory", "galaxy", "skills"):
        assert result[name] is None

