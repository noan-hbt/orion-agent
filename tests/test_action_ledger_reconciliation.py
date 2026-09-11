import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from action_ledger import ActionLedger, action_key


def _expire_reservation(ledger: ActionLedger, key: str) -> None:
    ledger._connection.execute(
        "UPDATE actions SET lease_until=? WHERE action_key=?",
        (time.time() - 1.0, key),
    )
    ledger._connection.commit()


def test_stale_lease_becomes_uncertain_and_requires_reconciliation(tmp_path):
    path = tmp_path / "actions.sqlite3"
    ledger = ActionLedger(path, owner_id="worker-a")
    first = ledger.reserve("send_email", {"to": "a@example.test"})
    assert first.allowed is True
    assert first.fence_token == 1

    _expire_reservation(ledger, first.action_key)
    ledger.close()

    ledger = ActionLedger(path, owner_id="worker-b")

    blocked = ledger.reserve("send_email", {"to": "a@example.test"})
    assert blocked.allowed is False
    assert blocked.reason == "needs_reconciliation"
    assert blocked.existing is not None
    assert blocked.existing.status == "uncertain"
    assert blocked.existing.needs_reconciliation is True
    assert [item.action_key for item in ledger.needs_reconciliation()] == [first.action_key]

    reconciled = ledger.reconcile(
        first.action_key,
        outcome="failed",
        error="provider confirms the request was not applied",
    )
    assert reconciled.status == "failed"

    retry = ledger.reserve("send_email", {"to": "a@example.test"})
    assert retry.allowed is True
    assert retry.fence_token == 2
    ledger.close()


def test_concurrent_owners_are_blocked_and_old_fence_cannot_finalize_retry(tmp_path):
    path = tmp_path / "actions.sqlite3"
    first_owner = ActionLedger(path, owner_id="worker-a")
    second_owner = ActionLedger(path, owner_id="worker-b")

    barrier = threading.Barrier(2)

    def reserve(ledger):
        barrier.wait()
        return ledger.reserve("charge", {"amount": 10}, target="account:1")

    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(reserve, (first_owner, second_owner)))

    allowed = [decision for decision in decisions if decision.allowed]
    blocked = [decision for decision in decisions if not decision.allowed]
    assert len(allowed) == 1
    assert len(blocked) == 1
    first = allowed[0]
    concurrent = blocked[0]
    assert concurrent.reason == "already_running"
    assert concurrent.existing is not None
    assert concurrent.existing.owner_id == first.owner_id

    winner = first_owner if first.owner_id == "worker-a" else second_owner
    loser = second_owner if winner is first_owner else first_owner
    loser_id = "worker-b" if loser is second_owner else "worker-a"

    _expire_reservation(winner, first.action_key)
    stale = loser.reserve("charge", {"amount": 10}, target="account:1")
    assert stale.reason == "needs_reconciliation"
    loser.reconcile(first.action_key, outcome="failed", error="not applied")

    retry = loser.reserve("charge", {"amount": 10}, target="account:1")
    assert retry.allowed is True
    assert retry.owner_id == loser_id
    assert retry.fence_token == 2

    stale_completion = winner.complete(
        first.action_key,
        {"receipt": "old"},
        owner_id=first.owner_id,
        fence_token=first.fence_token,
    )
    assert stale_completion is not None
    assert stale_completion.status == "running"
    assert stale_completion.owner_id == loser_id
    assert stale_completion.fence_token == 2

    completed = loser.complete(
        retry.action_key,
        {"receipt": "new"},
        owner_id=loser_id,
        fence_token=retry.fence_token,
    )
    assert completed is not None
    assert completed.status == "succeeded"
    assert completed.result == {"receipt": "new"}

    first_owner.close()
    second_owner.close()


def test_legacy_database_migrates_running_rows_to_uncertain(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    key = action_key("deploy", {"version": 1}, target="prod")
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE actions (
            action_key TEXT PRIMARY KEY,
            operation TEXT NOT NULL,
            target TEXT,
            arguments_json TEXT NOT NULL,
            normalized_json TEXT NOT NULL,
            status TEXT NOT NULL,
            result_json TEXT,
            error TEXT,
            created_at REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    connection.execute(
        "INSERT INTO actions VALUES (?, ?, ?, ?, ?, 'running', NULL, NULL, ?, 1)",
        (key, "deploy", "prod", '{"version": 1}', '{"version": 1}', 10.0),
    )
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()

    ledger = ActionLedger(path, owner_id="new-process")
    columns = {
        row[1]
        for row in ledger._connection.execute("PRAGMA table_info(actions)").fetchall()
    }
    assert {"owner_id", "lease_until", "updated_at", "fence_token"} <= columns
    assert ledger._connection.execute("PRAGMA user_version").fetchone()[0] == 2

    migrated = ledger.get(key)
    assert migrated is not None
    assert migrated.status == "uncertain"
    assert migrated.needs_reconciliation is True
    assert migrated.owner_id is None
    assert migrated.lease_until is None
    assert migrated.updated_at >= migrated.created_at

    blocked = ledger.reserve("deploy", {"version": 1}, target="prod")
    assert blocked.allowed is False
    assert blocked.reason == "needs_reconciliation"
    ledger.close()


def test_completed_duplicate_behavior_is_preserved(tmp_path):
    ledger = ActionLedger(tmp_path / "actions.sqlite3", owner_id="worker-a")
    first = ledger.reserve("publish", {"body": "hello"}, target="channel:1")
    completed = ledger.complete(first.action_key, {"message_id": "42"})
    assert completed is not None
    assert completed.status == "succeeded"

    duplicate = ledger.reserve("publish", {"body": "hello"}, target="channel:1")
    assert duplicate.allowed is False
    assert duplicate.reason == "already_succeeded"
    assert duplicate.existing is not None
    assert duplicate.existing.result == {"message_id": "42"}

    completed_again = ledger.complete(first.action_key, {"message_id": "different"})
    assert completed_again is not None
    assert completed_again.status == "succeeded"
    assert completed_again.result == {"message_id": "42"}
    ledger.close()


def test_mark_uncertain_is_immediate_fenced_and_blocks_retry(tmp_path):
    path = tmp_path / "actions.sqlite3"
    first = ActionLedger(path, owner_id="worker-a")
    second = ActionLedger(path, owner_id="worker-b")
    try:
        reservation = first.reserve("publish", {"body": "once"}, target="channel:1")
        uncertain = first.mark_uncertain(
            reservation.action_key,
            "transport failed after dispatch",
            owner_id=reservation.owner_id,
            fence_token=reservation.fence_token,
        )
        assert uncertain is not None
        assert uncertain.status == "uncertain"
        assert uncertain.needs_reconciliation is True

        blocked = second.reserve("publish", {"body": "once"}, target="channel:1")
        assert blocked.allowed is False
        assert blocked.reason == "needs_reconciliation"

        second.reconcile(reservation.action_key, outcome="failed", error="provider confirms absent")
        retry = second.reserve("publish", {"body": "once"}, target="channel:1")
        assert retry.allowed is True and retry.fence_token == reservation.fence_token + 1

        stale = first.mark_uncertain(
            reservation.action_key,
            "late stale failure",
            owner_id=reservation.owner_id,
            fence_token=reservation.fence_token,
        )
        assert stale is not None
        assert stale.status == "running"
        assert stale.owner_id == "worker-b"
        assert stale.fence_token == retry.fence_token
    finally:
        first.close()
        second.close()
