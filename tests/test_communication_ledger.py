from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from communication_ledger import CommunicationLedger, IdempotencyConflict


def test_same_external_id_is_isolated_by_channel(tmp_path):
    ledger = CommunicationLedger(tmp_path / "communication.sqlite3")
    try:
        telegram_id, telegram_new = ledger.record_inbound(
            channel="telegram",
            payload={"text": "from telegram"},
            message_id="42",
        )
        webhook_id, webhook_new = ledger.record_inbound(
            channel="webhook",
            payload={"text": "from webhook"},
            message_id="42",
        )

        assert telegram_new is True
        assert webhook_new is True
        assert telegram_id != webhook_id
        assert ledger.get(telegram_id)["channel"] == "telegram"
        assert ledger.get(webhook_id)["channel"] == "webhook"
    finally:
        ledger.close()


def test_same_channel_same_external_id_is_duplicate(tmp_path):
    ledger = CommunicationLedger(tmp_path / "communication.sqlite3")
    try:
        first_id, first_new = ledger.record_inbound(
            channel="telegram", payload={"text": "same"}, message_id="42"
        )
        second_id, second_new = ledger.record_inbound(
            channel="telegram", payload={"text": "same"}, message_id="42"
        )

        assert first_new is True
        assert second_new is False
        assert second_id == first_id
    finally:
        ledger.close()


def test_same_channel_same_external_id_with_changed_payload_conflicts(tmp_path):
    ledger = CommunicationLedger(tmp_path / "communication.sqlite3")
    try:
        ledger.record_inbound(
            channel="telegram", payload={"text": "first"}, message_id="42"
        )

        with pytest.raises(IdempotencyConflict):
            ledger.record_inbound(
                channel="telegram", payload={"text": "changed"}, message_id="42"
            )
    finally:
        ledger.close()


def test_explicit_idempotency_key_remains_global(tmp_path):
    ledger = CommunicationLedger(tmp_path / "communication.sqlite3")
    try:
        first_id, first_new = ledger.record_inbound(
            channel="telegram",
            payload={"text": "same logical request"},
            message_id="telegram-42",
            idempotency_key="global-request-1",
        )
        duplicate_id, duplicate_new = ledger.record_inbound(
            channel="telegram",
            payload={"text": "same logical request"},
            message_id="telegram-43",
            idempotency_key="global-request-1",
        )

        assert first_new is True
        assert duplicate_new is False
        assert duplicate_id == first_id

        with pytest.raises(IdempotencyConflict):
            ledger.record_inbound(
                channel="webhook",
                payload={"text": "same logical request"},
                message_id="webhook-42",
                idempotency_key="global-request-1",
            )
    finally:
        ledger.close()


def test_get_by_event_id_keeps_raw_lookup_when_unambiguous(tmp_path):
    ledger = CommunicationLedger(tmp_path / "communication.sqlite3")
    try:
        row_id, _ = ledger.record_inbound(
            channel="telegram",
            payload={"text": "lookup"},
            event_id="external-event-7",
            message_id="telegram-message-7",
        )

        assert ledger.get_by_event_id("external-event-7")["id"] == row_id
        assert ledger.get_by_event_id(
            "external-event-7", channel="telegram", kind="inbound"
        )["id"] == row_id
    finally:
        ledger.close()


def test_legacy_raw_event_id_dedupes_only_within_its_original_scope(tmp_path):
    path = tmp_path / "communication.sqlite3"
    ledger = CommunicationLedger(path)
    ledger.close()

    now = time.time()
    connection = sqlite3.connect(path)
    try:
        payload = {"text": "legacy"}
        fingerprint = CommunicationLedger._fingerprint(
            kind="inbound",
            channel="telegram",
            payload=payload,
            correlation_id=None,
            reply_to=None,
        )
        connection.execute(
            """INSERT INTO communication_events
            (id,event_id,kind,channel,idempotency_key,dedupe_key,fingerprint,payload,
             correlation_id,reply_to,status,max_attempts,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?, 'delivered',?,?,?)""",
            (
                "42",
                "42",
                "inbound",
                "telegram",
                None,
                "event:42",
                fingerprint,
                CommunicationLedger._json(payload),
                None,
                None,
                3,
                now,
                now,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    ledger = CommunicationLedger(path)
    try:
        same_id, same_new = ledger.record_inbound(
            channel="telegram", payload={"text": "legacy"}, message_id="42"
        )
        other_id, other_new = ledger.record_inbound(
            channel="webhook", payload={"text": "legacy"}, message_id="42"
        )

        assert same_new is False
        assert same_id == "42"
        assert ledger.get("42")["status"] == "delivered"
        assert other_new is True
        assert other_id != "42"
        assert ledger.get(other_id)["status"] == "queued"
    finally:
        ledger.close()


def test_legacy_schema_migrates_before_retry_indexes_are_created(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """CREATE TABLE communication_events (
            id TEXT PRIMARY KEY,
            event_id TEXT UNIQUE,
            kind TEXT NOT NULL,
            channel TEXT NOT NULL,
            idempotency_key TEXT,
            dedupe_key TEXT NOT NULL UNIQUE,
            fingerprint TEXT NOT NULL,
            payload TEXT NOT NULL,
            correlation_id TEXT,
            reply_to TEXT,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
            )"""
        )
        connection.commit()
    finally:
        connection.close()

    ledger = CommunicationLedger(path)
    try:
        columns = {
            row["name"]
            for row in ledger._connection.execute(
                "PRAGMA table_info(communication_events)"
            ).fetchall()
        }
        assert {
            "max_attempts",
            "lease_owner",
            "lease_until",
            "next_attempt_at",
            "last_error",
        } <= columns
        indexes = {
            row["name"]
            for row in ledger._connection.execute(
                "PRAGMA index_list(communication_events)"
            ).fetchall()
        }
        assert "communication_events_ready" in indexes
        assert ledger._connection.execute("PRAGMA user_version").fetchone()[0] >= 2
    finally:
        ledger.close()


def test_stale_fail_cannot_clear_newer_worker_claim(tmp_path):
    path = tmp_path / "communication.sqlite3"
    old = CommunicationLedger(path)
    new = CommunicationLedger(path)
    entered_update = threading.Event()
    release_update = threading.Event()

    class BlockingConnection:
        def __init__(self, connection):
            self._connection = connection

        def execute(self, sql, args=()):
            if sql.startswith("UPDATE communication_events SET status=?"):
                entered_update.set()
                assert release_update.wait(timeout=2.0)
            return self._connection.execute(sql, args)

        def __getattr__(self, name):
            return getattr(self._connection, name)

    try:
        row_id, _ = old.record_inbound(
            channel="web",
            payload={"text": "race"},
            message_id="race-1",
            max_attempts=5,
        )
        assert old.claim_by_id(
            row_id,
            worker_id="old-worker",
            lease_seconds=0.05,
        ) is not None
        old._connection = BlockingConnection(old._connection)
        result: list[str | None] = []
        thread = threading.Thread(
            target=lambda: result.append(
                old.fail(row_id, "old failure", worker_id="old-worker")
            )
        )
        thread.start()
        assert entered_update.wait(timeout=2.0)
        time.sleep(0.07)
        claimed = new.claim_by_id(
            row_id,
            worker_id="new-worker",
            lease_seconds=1.0,
        )
        assert claimed is not None
        assert claimed["attempts"] == 2

        release_update.set()
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        assert result == [None]
        row = new.get(row_id)
        assert row["status"] == "claimed"
        assert row["lease_owner"] == "new-worker"
        assert row["attempts"] == 2
    finally:
        release_update.set()
        old.close()
        new.close()
