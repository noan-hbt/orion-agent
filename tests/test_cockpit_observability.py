import json
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


class ActionLedgerService:
    def __init__(self, value):
        self.value = value

    def snapshot(self):
        return self.value


class RuntimeWithActions(Runtime):
    def __init__(self, action_ledger):
        self.action_ledger = action_ledger


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
                 "tasks", "events", "memory", "galaxy", "skills", "actions"):
        assert result[name] is None


def test_action_ledger_snapshot_surfaces_uncertain_state_without_secrets():
    action_key = "a" * 64
    runtime = RuntimeWithActions(ActionLedgerService({
        "component": "action_ledger",
        "available": True,
        "closed": False,
        "total": 3,
        "status_counts": {
            "running": 1,
            "uncertain": 1,
            "succeeded": 1,
            "failed": 0,
        },
        "needs_reconciliation": 1,
        "uncertain_action_keys": [action_key],
        "oldest_uncertain_age_seconds": 12.5,
    }))

    result = collect_observability({"runtime": runtime})

    assert result["actions"]["needs_reconciliation"] == 1
    assert result["actions"]["status_counts"]["uncertain"] == 1
    assert result["actions"]["uncertain_action_keys"] == [action_key]
    encoded = json.dumps(result)
    for secret in ("TOP-SECRET", "PRIVATE-BODY", "SECRET-RESULT"):
        assert secret not in encoded


def test_action_ledger_unavailable_or_broken_is_graceful_and_never_falls_back_to_raw_records():
    class UnsafeLedger:
        def snapshot(self):
            raise RuntimeError("closed")

        def list(self):
            return [{"arguments": {"token": "DO-NOT-LEAK"}}]

    result = collect_observability({"runtime": RuntimeWithActions(UnsafeLedger())})

    assert result["actions"] is None
    assert "DO-NOT-LEAK" not in json.dumps(result)


def test_closed_action_ledger_safe_snapshot_is_preserved():
    closed = {
        "component": "action_ledger",
        "available": False,
        "closed": True,
        "total": None,
        "status_counts": {
            "running": None,
            "uncertain": None,
            "succeeded": None,
            "failed": None,
        },
        "needs_reconciliation": None,
        "uncertain_action_keys": [],
        "oldest_uncertain_age_seconds": None,
    }

    result = collect_observability({
        "runtime": RuntimeWithActions(ActionLedgerService(closed))
    })

    assert result["actions"] == closed

