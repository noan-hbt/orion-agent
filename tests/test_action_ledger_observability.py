from __future__ import annotations

import json

from action_ledger import ActionLedger
from observability import collect_observability


class _Runtime:
    def __init__(self, ledger: ActionLedger) -> None:
        self.action_ledger = ledger


class _Application:
    def __init__(self, ledger: ActionLedger) -> None:
        self.runtime = _Runtime(ledger)


def _expire(ledger: ActionLedger, key: str, *, lease_until: float) -> None:
    ledger._connection.execute(
        "UPDATE actions SET lease_until=? WHERE action_key=?",
        (lease_until, key),
    )
    ledger._connection.commit()


def test_snapshot_reports_effective_uncertain_without_mutating_or_leaking(tmp_path) -> None:
    ledger = ActionLedger(tmp_path / "actions.sqlite3", owner_id="ops")
    running = ledger.reserve(
        "send_secret",
        {"token": "TOP-SECRET", "body": "PRIVATE-BODY"},
        target="sensitive@example.test",
        lease_seconds=60,
    )
    succeeded = ledger.reserve("done", {"password": "SECRET-PASSWORD"})
    ledger.complete(succeeded.action_key, {"provider_response": "SECRET-RESULT"})
    failed = ledger.reserve("failed", {"api_key": "SECRET-KEY"})
    ledger.fail(failed.action_key, "SECRET-ERROR")

    _expire(ledger, running.action_key, lease_until=90.0)
    snapshot = ledger.snapshot(now=100.0)

    assert snapshot["status_counts"] == {
        "running": 0,
        "uncertain": 1,
        "succeeded": 1,
        "failed": 1,
    }
    assert snapshot["needs_reconciliation"] == 1
    assert snapshot["uncertain_action_keys"] == [running.action_key]
    assert snapshot["oldest_uncertain_age_seconds"] == 10.0

    # Snapshotting is read-only: the durable row is still RUNNING until a
    # normal ledger operation performs stale-lease migration.
    durable_status = ledger._connection.execute(
        "SELECT status FROM actions WHERE action_key=?",
        (running.action_key,),
    ).fetchone()[0]
    assert durable_status == "running"

    encoded = json.dumps(snapshot)
    for secret in (
        "TOP-SECRET",
        "PRIVATE-BODY",
        "sensitive@example.test",
        "SECRET-PASSWORD",
        "SECRET-RESULT",
        "SECRET-KEY",
        "SECRET-ERROR",
    ):
        assert secret not in encoded
    ledger.close()


def test_snapshot_counts_durable_uncertain_and_caps_safe_identifiers(tmp_path) -> None:
    ledger = ActionLedger(tmp_path / "actions.sqlite3", owner_id="ops")
    keys = []
    for index in range(5):
        decision = ledger.reserve(f"op-{index}", {"secret": f"value-{index}"})
        _expire(ledger, decision.action_key, lease_until=1.0)
        ledger.get(decision.action_key)
        keys.append(decision.action_key)

    snapshot = ledger.snapshot(uncertain_limit=2, now=100.0)

    assert snapshot["status_counts"]["uncertain"] == 5
    assert snapshot["needs_reconciliation"] == 5
    assert len(snapshot["uncertain_action_keys"]) == 2
    assert set(snapshot["uncertain_action_keys"]) <= set(keys)
    ledger.close()


def test_collect_observability_includes_runtime_action_ledger_without_secrets(tmp_path) -> None:
    ledger = ActionLedger(tmp_path / "actions.sqlite3", owner_id="ops")
    decision = ledger.reserve("send", {"token": "DO-NOT-LEAK"}, lease_seconds=60)
    _expire(ledger, decision.action_key, lease_until=1.0)

    result = collect_observability(_Application(ledger))

    assert result["action_ledger"]["needs_reconciliation"] == 1
    assert result["action_ledger"]["uncertain_action_keys"] == [decision.action_key]
    assert "DO-NOT-LEAK" not in json.dumps(result)
    ledger.close()


def test_snapshot_and_observability_are_safe_after_close(tmp_path) -> None:
    ledger = ActionLedger(tmp_path / "actions.sqlite3")
    ledger.reserve("noop", {"secret": "hidden"})
    ledger.close()
    ledger.close()

    snapshot = ledger.snapshot()
    observed = collect_observability(_Application(ledger))

    assert snapshot["available"] is False
    assert snapshot["closed"] is True
    assert snapshot["uncertain_action_keys"] == []
    assert observed["action_ledger"] == snapshot
    assert "hidden" not in json.dumps(observed)
