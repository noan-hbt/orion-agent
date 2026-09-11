from __future__ import annotations

import sqlite3
import threading

import pytest

from durable_events import (
    DurableEventConflict,
    DurableEventStore,
    DurableEventValidationError,
)


def _event(
    event_id: str,
    *,
    fingerprint: str | None = None,
    message_id: str | None = None,
    idempotency_key: str | None = None,
    value: str = "payload",
):
    event = {
        "event_id": event_id,
        "fingerprint": fingerprint or f"fp:{event_id}:{value}",
        "payload": {"value": value},
    }
    if message_id is not None:
        event["message_id"] = message_id
    if idempotency_key is not None:
        event["idempotency_key"] = idempotency_key
    return event


def test_duplicate_returns_same_receipt_and_conflict_is_rejected(tmp_path):
    store = DurableEventStore(tmp_path / "events.sqlite3", namespace="runtime")
    try:
        first = store.accept(
            _event("event-1", message_id="msg-1", idempotency_key="request-1")
        )
        duplicate = store.accept(
            _event("event-1", message_id="msg-1", idempotency_key="request-1")
        )
        assert duplicate.receipt_id == first.receipt_id

        with pytest.raises(DurableEventConflict):
            store.accept(
                _event(
                    "event-1",
                    fingerprint="different-fingerprint",
                    message_id="msg-1",
                    idempotency_key="request-1",
                )
            )
    finally:
        store.close()


def test_namespace_isolates_event_and_idempotency_keys(tmp_path):
    path = tmp_path / "events.sqlite3"
    first_store = DurableEventStore(path, namespace="event_handler")
    second_store = DurableEventStore(path, namespace="runtime")
    try:
        first = first_store.accept(_event("same", idempotency_key="same-key"))
        second = second_store.accept(_event("same", idempotency_key="same-key"))
        assert first.receipt_id != second.receipt_id
        assert first.namespace == "event_handler"
        assert second.namespace == "runtime"
    finally:
        first_store.close()
        second_store.close()


def test_multi_connection_claim_has_single_owner_and_fence(tmp_path):
    path = tmp_path / "events.sqlite3"
    producer = DurableEventStore(path, namespace="runtime")
    receipt = producer.accept(_event("event-1"))
    producer.close()

    first = DurableEventStore(path, namespace="runtime")
    second = DurableEventStore(path, namespace="runtime")
    barrier = threading.Barrier(2)
    results: list[tuple[str, list]] = []

    def claim(store, owner):
        barrier.wait()
        results.append((owner, store.claim(owner_id=owner, lease_seconds=30)))

    one = threading.Thread(target=claim, args=(first, "owner-a"))
    two = threading.Thread(target=claim, args=(second, "owner-b"))
    one.start()
    two.start()
    one.join()
    two.join()
    try:
        claimed = [(owner, values[0]) for owner, values in results if values]
        assert len(claimed) == 1
        owner, item = claimed[0]
        assert item.receipt_id == receipt.receipt_id
        assert item.owner_id == owner
        assert item.fence_token == 1
        assert item.attempts == 1
    finally:
        first.close()
        second.close()


def test_multi_connection_concurrent_accept_returns_one_receipt(tmp_path):
    path = tmp_path / "events.sqlite3"
    first = DurableEventStore(path, namespace="event_handler")
    second = DurableEventStore(path, namespace="event_handler")
    barrier = threading.Barrier(2)
    receipts = []

    def accept(store):
        barrier.wait()
        receipts.append(
            store.accept(_event("event-1", idempotency_key="request-1")).receipt_id
        )

    one = threading.Thread(target=accept, args=(first,))
    two = threading.Thread(target=accept, args=(second,))
    one.start()
    two.start()
    one.join()
    two.join()
    try:
        assert len(receipts) == 2
        assert receipts[0] == receipts[1]
        assert len(first.list_events()) == 1
    finally:
        first.close()
        second.close()


def test_concurrent_fresh_store_initialization_is_lock_safe(tmp_path):
    path = tmp_path / "events.sqlite3"
    barrier = threading.Barrier(12)
    errors = []
    stores = []
    lock = threading.Lock()

    def open_store(index):
        try:
            barrier.wait()
            store = DurableEventStore(path, namespace=f"worker-{index}")
            with lock:
                stores.append(store)
        except Exception as exc:  # pragma: no cover - asserted below
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=open_store, args=(index,)) for index in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)
    try:
        assert all(not thread.is_alive() for thread in threads)
        assert errors == []
        assert len(stores) == 12
    finally:
        for store in stores:
            store.close()


def test_concurrent_legacy_migration_is_idempotent(tmp_path):
    path = tmp_path / "events.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            """
            CREATE TABLE durable_events (
                receipt_id TEXT PRIMARY KEY,
                namespace TEXT NOT NULL,
                event_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        db.execute(
            "INSERT INTO durable_events(receipt_id,namespace,event_id,fingerprint,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                "legacy-receipt",
                "runtime",
                "legacy-event",
                "legacy-fp",
                '{"event_id":"legacy-event","fingerprint":"legacy-fp"}',
                1.0,
            ),
        )

    barrier = threading.Barrier(8)
    errors = []
    stores = []
    lock = threading.Lock()

    def migrate(index):
        try:
            barrier.wait()
            store = DurableEventStore(path, namespace=f"migration-{index}")
            with lock:
                stores.append(store)
        except Exception as exc:  # pragma: no cover - asserted below
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=migrate, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)
    try:
        assert all(not thread.is_alive() for thread in threads)
        assert errors == []
        with sqlite3.connect(path) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(durable_events)")}
        assert {"last_error", "updated_at", "fence_token", "lease_until"} <= columns
    finally:
        for store in stores:
            store.close()


def test_crash_restart_lists_and_explicitly_recovers_stale_processing(tmp_path):
    path = tmp_path / "events.sqlite3"
    now = [100.0]
    first = DurableEventStore(path, namespace="runtime", clock=lambda: now[0])
    queued = first.accept(_event("queued"))
    now[0] = 101.0
    processing = first.accept(_event("processing"))
    claim = first.claim(owner_id="crashed-worker", lease_seconds=5, limit=1)[0]
    assert claim.receipt_id == queued.receipt_id
    first.close()

    now[0] = 106.0
    restarted = DurableEventStore(path, namespace="runtime", clock=lambda: now[0])
    try:
        recoverable = restarted.list_recoverable()
        assert {item.receipt_id for item in recoverable} == {
            queued.receipt_id,
            processing.receipt_id,
        }
        stale = [item for item in recoverable if item.status == "processing"]
        assert len(stale) == 1 and stale[0].is_stale(now[0])

        recovered = restarted.recover_stale()
        assert [item.receipt_id for item in recovered] == [queued.receipt_id]
        assert restarted.get(queued.receipt_id).status == "queued"

        reclaimed = restarted.claim(owner_id="new-worker", lease_seconds=10, limit=2)
        assert {item.receipt_id for item in reclaimed} == {
            queued.receipt_id,
            processing.receipt_id,
        }
        by_id = {item.receipt_id: item for item in reclaimed}
        assert by_id[queued.receipt_id].fence_token == 2
    finally:
        restarted.close()


def test_ack_and_fail_require_live_owner_lease_and_fence(tmp_path):
    path = tmp_path / "events.sqlite3"
    now = [50.0]
    store = DurableEventStore(path, namespace="runtime", clock=lambda: now[0])
    try:
        receipt = store.accept(_event("event-1"))
        claim = store.claim(owner_id="owner-a", lease_seconds=5)[0]

        assert store.ack(
            receipt.receipt_id, owner_id="owner-b", fence_token=claim.fence_token
        ) is False
        assert store.ack(
            receipt.receipt_id, owner_id="owner-a", fence_token=claim.fence_token + 1
        ) is False

        now[0] = 56.0
        assert store.fail(
            receipt.receipt_id,
            "late failure",
            owner_id="owner-a",
            fence_token=claim.fence_token,
        ) is False

        assert store.recover_stale()[0].receipt_id == receipt.receipt_id
        next_claim = store.claim(owner_id="owner-b", lease_seconds=10)[0]
        assert next_claim.fence_token == claim.fence_token + 1
        assert store.ack(
            receipt.receipt_id,
            owner_id="owner-a",
            fence_token=claim.fence_token,
        ) is False
        assert store.ack(
            receipt.receipt_id,
            owner_id="owner-b",
            fence_token=next_claim.fence_token,
        ) is True
        assert store.get(receipt.receipt_id).status == "acked"
    finally:
        store.close()


def test_renew_claim_extends_only_the_live_exact_fence(tmp_path):
    now = [10.0]
    store = DurableEventStore(
        tmp_path / "events.sqlite3", namespace="runtime", clock=lambda: now[0]
    )
    try:
        receipt = store.accept(_event("heartbeat"))
        claim = store.claim(owner_id="owner-a", lease_seconds=5)[0]
        assert claim.lease_until == 15.0

        now[0] = 14.0
        assert store.renew_claim(
            receipt.receipt_id,
            owner_id="owner-a",
            fence_token=claim.fence_token,
            lease_seconds=5,
        ) is True
        assert store.get(receipt.receipt_id).lease_until == 19.0
        assert store.renew_claim(
            receipt.receipt_id,
            owner_id="owner-b",
            fence_token=claim.fence_token,
            lease_seconds=5,
        ) is False
        assert store.renew_claim(
            receipt.receipt_id,
            owner_id="owner-a",
            fence_token=claim.fence_token + 1,
            lease_seconds=5,
        ) is False

        now[0] = 16.0
        assert store.recover_stale() == []
        now[0] = 20.0
        assert [item.receipt_id for item in store.recover_stale()] == [receipt.receipt_id]
        assert store.renew_claim(
            receipt.receipt_id,
            owner_id="owner-a",
            fence_token=claim.fence_token,
            lease_seconds=5,
        ) is False
    finally:
        store.close()


def test_claim_receipt_claims_exact_row_for_external_priority_queue(tmp_path):
    store = DurableEventStore(tmp_path / "events.sqlite3", namespace="event_handler")
    try:
        first = store.accept(_event("first"))
        second = store.accept(_event("second"))

        claimed = store.claim_receipt(
            second.receipt_id, owner_id="priority-worker", lease_seconds=10
        )

        assert claimed is not None
        assert claimed.receipt_id == second.receipt_id
        assert claimed.fence_token == 1
        assert store.get(first.receipt_id).status == "queued"
    finally:
        store.close()


def test_fail_can_terminally_fail_or_explicitly_requeue(tmp_path):
    store = DurableEventStore(tmp_path / "events.sqlite3", namespace="runtime")
    try:
        terminal = store.accept(_event("terminal"))
        claim = store.claim(owner_id="worker")[0]
        assert store.fail(
            terminal.receipt_id,
            "bad payload",
            owner_id="worker",
            fence_token=claim.fence_token,
        )
        assert store.get(terminal.receipt_id).status == "failed"

        retried = store.accept(_event("retry"))
        retry_claim = store.claim(owner_id="worker")[0]
        assert store.fail(
            retried.receipt_id,
            "temporary",
            owner_id="worker",
            fence_token=retry_claim.fence_token,
            retry=True,
        )
        assert store.get(retried.receipt_id).status == "queued"
    finally:
        store.close()


def test_payload_is_strict_json_and_bounded(tmp_path):
    store = DurableEventStore(
        tmp_path / "events.sqlite3", namespace="runtime", max_payload_bytes=120
    )
    try:
        with pytest.raises(DurableEventValidationError, match="valid JSON"):
            store.accept(
                {
                    "event_id": "bad-json",
                    "fingerprint": "fp",
                    "payload": {"not_json": object()},
                }
            )
        with pytest.raises(DurableEventValidationError, match="exceeds"):
            store.accept(
                {
                    "event_id": "too-large",
                    "fingerprint": "fp-large",
                    "payload": {"text": "x" * 500},
                }
            )
    finally:
        store.close()


def test_additive_legacy_migration_preserves_queued_event(tmp_path):
    path = tmp_path / "events.sqlite3"
    db = sqlite3.connect(path)
    try:
        db.execute(
            """
            CREATE TABLE durable_events (
                receipt_id TEXT PRIMARY KEY,
                namespace TEXT NOT NULL,
                event_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        db.execute(
            "INSERT INTO durable_events(receipt_id,namespace,event_id,fingerprint,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                "legacy-receipt",
                "runtime",
                "legacy-event",
                "legacy-fp",
                '{"event_id":"legacy-event","fingerprint":"legacy-fp"}',
                1.0,
            ),
        )
        db.commit()
    finally:
        db.close()

    store = DurableEventStore(path, namespace="runtime")
    try:
        migrated = store.get("legacy-receipt")
        assert migrated is not None
        assert migrated.status == "queued"
        assert migrated.updated_at == 1.0
        claimed = store.claim(owner_id="worker")[0]
        assert claimed.receipt_id == "legacy-receipt"
        assert claimed.fence_token == 1
    finally:
        store.close()
