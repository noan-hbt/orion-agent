import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from cli_cockpit_backend import CockpitBackend


class Runtime:
    state = "sleep"
    running = False


class Tasks:
    def list(self): return [{"id": 1, "status": "running"}]


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
