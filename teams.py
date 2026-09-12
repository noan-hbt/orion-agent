"""Communication locale durable entre instances Orion.

Le bus est volontairement petit : une base SQLite partagée suffit pour faire
circuler des messages et des demandes entre processus sans introduire de
service. Les messages sont bornés, adressés à une instance et restent
consultables après un redémarrage.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from event_handler import EventHandler, EventPriority
from handoff_context import HandoffContext


def _durable_event_receipt(event_handler: Any, idempotency_key: str) -> Any | None:
    store = getattr(event_handler, "_durable_store", None)
    if store is None:
        return None
    try:
        receipts = store.list_events(limit=10000)
    except Exception:
        return None
    return next(
        (
            receipt
            for receipt in receipts
            if str(getattr(receipt, "idempotency_key", "") or "") == idempotency_key
        ),
        None,
    )


def _revive_failed_event_receipt(event_handler: Any, idempotency_key: str) -> bool:
    """Requeue one failed durable EventHandler receipt under the same key."""
    receipt = _durable_event_receipt(event_handler, idempotency_key)
    if receipt is None or str(getattr(receipt, "status", "")) != "failed":
        return False
    store = getattr(event_handler, "_durable_store", None)
    lock = getattr(store, "_lock", None)
    db = getattr(store, "_db", None)
    namespace = getattr(store, "namespace", None)
    if lock is None or db is None or namespace is None:
        return False
    try:
        with lock:
            db.execute("BEGIN IMMEDIATE")
            try:
                changed = db.execute(
                    "UPDATE durable_events SET status='queued', owner_id=NULL, "
                    "lease_until=NULL, last_error=NULL, updated_at=? "
                    "WHERE receipt_id=? AND namespace=? AND status='failed'",
                    (time.time(), str(receipt.receipt_id), str(namespace)),
                )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
    except Exception:
        return False
    if not changed.rowcount:
        return False
    hydrate = getattr(event_handler, "_hydrate_durable_queue", None)
    if callable(hydrate):
        try:
            hydrate()
        except Exception:
            pass
    return True


def _clip(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"


@dataclass(frozen=True)
class TeamMessage:
    id: str
    team: str
    sender: str
    recipient: str
    kind: str
    body: str
    subject: str
    correlation_id: str | None
    priority: int
    created_at: float
    delivered_at: float | None = None
    status: str = "queued"
    result: str | None = None
    error: str | None = None
    handoff_context: HandoffContext | None = None
    state_version: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "team": self.team,
            "sender": self.sender,
            "recipient": self.recipient,
            "kind": self.kind,
            "body": self.body,
            "subject": self.subject,
            "correlation_id": self.correlation_id,
            "priority": self.priority,
            "created_at": self.created_at,
            "delivered_at": self.delivered_at,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "handoff_context": self.handoff_context.to_dict() if self.handoff_context else None,
            "handoff_id": self.handoff_context.handoff_id if self.handoff_context else None,
            "state_version": self.state_version,
            # Keep the stable message id last for legacy consumers that
            # extract an id from a serialized handoff body.
            "id": self.id,
        }


class TeamBus:
    """SQLite mailbox and poller for one Orion instance."""

    _MAX_DOWNSTREAM_RECOVERIES = 3

    def __init__(
        self,
        path: str | Path = "data/teams.sqlite3",
        *,
        instance_id: str = "orion",
        team: str = "default",
        poll_interval: float = 1.0,
        max_message_chars: int = 12000,
        claim_timeout: float = 5.0,
        event_handler: EventHandler | None = None,
        sender_scope: str | None = None,
    ) -> None:
        if not str(instance_id).strip() or not str(team).strip():
            raise ValueError("instance_id et team doivent être renseignés.")
        if poll_interval <= 0 or max_message_chars < 1 or claim_timeout <= 0:
            raise ValueError("poll_interval, max_message_chars et claim_timeout sont invalides.")
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.instance_id = str(instance_id).strip()
        self.team = str(team).strip()
        self.poll_interval = float(poll_interval)
        self.max_message_chars = int(max_message_chars)
        self.claim_timeout = float(claim_timeout)
        self.event_handler = event_handler
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS team_messages (
                id TEXT PRIMARY KEY, team TEXT NOT NULL, sender TEXT NOT NULL,
                recipient TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL,
                subject TEXT NOT NULL DEFAULT '', correlation_id TEXT,
                priority INTEGER NOT NULL DEFAULT 20, created_at REAL NOT NULL,
            delivered_at REAL
            )"""
        )
        columns = {row[1] for row in self._connection.execute("PRAGMA table_info(team_messages)")}
        for name, definition in {
            "status": "TEXT NOT NULL DEFAULT 'queued'",
            "result": "TEXT",
            "error": "TEXT",
            # A delivery claim remains held while the local runtime processes
            # the published event.  The poller heartbeats it until the runtime
            # acknowledges; a crashed process stops renewing and can then be
            # recovered safely by another bus incarnation.
            "claim_owner": "TEXT",
            "claim_expires_at": "REAL",
            "claim_fence": "INTEGER NOT NULL DEFAULT 0",
            "handoff_context": "TEXT",
            "state_version": "INTEGER NOT NULL DEFAULT 0",
            "delivery_recoveries": "INTEGER NOT NULL DEFAULT 0",
            "delivery_failed": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in columns:
                self._connection.execute(f"ALTER TABLE team_messages ADD COLUMN {name} {definition}")
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_team_inbox ON team_messages(team, recipient, delivered_at, created_at)"
        )
        self._connection.commit()
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._claim_owner = uuid.uuid4().hex
        # Keep a claimed delivery alive while the local runtime is processing
        # the published event.  ``claim_owner`` is a per-TeamBus incarnation
        # token, so another process (or a restarted process) cannot renew or
        # acknowledge this lease accidentally.
        self._claim_heartbeat_interval = min(
            self.poll_interval,
            self.claim_timeout / 3.0,
        )
        self._published_ids: set[str] = set()
        self._claim_fences: dict[str, int] = {}
        self._published_completion_keys: set[str] = set()
        self._completion_claim_fences: dict[str, int] = {}
        self.sender_scope = str(sender_scope or self.team).strip() or self.team
        self._closed = False
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS completion_notifications (
                key TEXT PRIMARY KEY, team TEXT NOT NULL, sender TEXT NOT NULL,
                recipient TEXT NOT NULL, message_id TEXT NOT NULL, state_version INTEGER NOT NULL,
                payload TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, claim_owner TEXT, claim_expires_at REAL,
                claim_fence INTEGER NOT NULL DEFAULT 0,
                delivery_recoveries INTEGER NOT NULL DEFAULT 0,
                delivery_failed INTEGER NOT NULL DEFAULT 0
            )"""
        )
        completion_columns = {
            row[1]
            for row in self._connection.execute(
                "PRAGMA table_info(completion_notifications)"
            )
        }
        for name, definition in {
            "claim_owner": "TEXT",
            "claim_expires_at": "REAL",
            "claim_fence": "INTEGER NOT NULL DEFAULT 0",
            "delivery_recoveries": "INTEGER NOT NULL DEFAULT 0",
            "delivery_failed": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in completion_columns:
                self._connection.execute(
                    f"ALTER TABLE completion_notifications ADD COLUMN {name} {definition}"
                )
        self._connection.commit()

        # Databases created by the first bus version have no status for rows
        # that were already delivered.  Normalize those rows once so they do
        # not look like fresh queued jobs after a migration.
        with self._lock:
            self._connection.execute(
                "UPDATE team_messages SET status=CASE WHEN kind='job' THEN 'in_progress' ELSE 'delivered' END WHERE delivered_at IS NOT NULL AND status='queued'"
            )
            self._connection.commit()
        self._replay_completion_notifications()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def snapshot(self) -> dict[str, Any]:
        """Return bounded aggregate state for cockpit/observability consumers."""
        heartbeat_running = (
            self._heartbeat_thread is not None and self._heartbeat_thread.is_alive()
        )
        if self._closed:
            return {
                "component": "team_bus",
                "running": False,
                "closed": True,
                "heartbeat_running": heartbeat_running,
                "status_counts": {},
                "pending": 0,
                "inflight": 0,
                "failed": 0,
                "retry_pending": 0,
                "oldest_pending_age_seconds": None,
            }

        now = time.time()
        with self._lock:
            rows = self._connection.execute(
                """SELECT status, COUNT(*) AS count
                     FROM team_messages
                    WHERE team=? AND recipient=?
                    GROUP BY status""",
                (self.team, self.instance_id),
            ).fetchall()
            oldest = self._connection.execute(
                """SELECT MIN(created_at) AS oldest
                     FROM team_messages
                    WHERE team=? AND recipient=?
                      AND status IN ('queued', 'delivered', 'in_progress')
                      AND result IS NULL""",
                (self.team, self.instance_id),
            ).fetchone()
            completion_row = self._connection.execute(
                """SELECT COUNT(*) AS count
                     FROM completion_notifications
                    WHERE team=? AND sender=? AND acknowledged=0""",
                (self.team, self.instance_id),
            ).fetchone()
            owned_claims = self._connection.execute(
                """SELECT COUNT(*) AS count
                     FROM team_messages
                    WHERE team=? AND recipient=? AND claim_owner=?
                      AND kind='job' AND status='in_progress'""",
                (self.team, self.instance_id, self._claim_owner),
            ).fetchone()

        counts = {str(row["status"]): int(row["count"]) for row in rows}
        oldest_value = oldest["oldest"] if oldest is not None else None
        return {
            "component": "team_bus",
            "running": self.running,
            "closed": False,
            "heartbeat_running": heartbeat_running,
            "status_counts": counts,
            "pending": counts.get("queued", 0),
            # ``in_progress`` is the durable job lifecycle state.  A runtime
            # delivery claim is only transport ownership and may be released
            # before the worker explicitly calls complete_job().  Reporting
            # only owned claims made an unfinished job disappear from cockpit
            # snapshots after delivery acknowledgement.
            "inflight": counts.get("in_progress", 0),
            "claimed_inflight": (
                int(owned_claims["count"]) if owned_claims is not None else 0
            ),
            "failed": counts.get("failed", 0),
            "retry_pending": int(completion_row["count"])
            if completion_row is not None
            else 0,
            "oldest_pending_age_seconds": (
                max(0.0, now - float(oldest_value))
                if oldest_value is not None
                else None
            ),
        }

    def close(self) -> None:
        self.stop()
        self._heartbeat_stop.set()
        heartbeat = self._heartbeat_thread
        if heartbeat is not None and heartbeat is not threading.current_thread():
            heartbeat.join()
        with self._lock:
            if not self._closed:
                self._release_owned_claims()
                self._connection.close()
                self._closed = True
        self._heartbeat_thread = None

    def send(
        self,
        recipient: str,
        body: str,
        *,
        kind: str = "message",
        subject: str = "",
        correlation_id: str | None = None,
        priority: int = int(EventPriority.NORMAL),
        message_id: str | None = None,
        handoff_context: HandoffContext | dict[str, Any] | None = None,
        parent_event_id: str | None = None,
        parent_task_id: str | None = None,
        source_scope: str | None = None,
        target_scope: str | None = None,
    ) -> TeamMessage:
        self._ensure_open()
        recipient = str(recipient).strip()
        body = _clip(body, self.max_message_chars)
        if "*" in recipient:
            raise ValueError("Les messages d'équipe doivent viser une instance précise.")
        if not recipient or not body:
            raise ValueError("recipient et body sont obligatoires.")
        durable_message_id = str(message_id or uuid.uuid4().hex[:16])
        if handoff_context is None:
            handoff_context = HandoffContext.create(
                kind="team_job" if kind == "job" else "team_job",
                objective=body,
                # Sans corrélation parent, le job est la racine durable du
                # handoff. Cela évite de créer un second identifiant opaque
                # qui pourrait être confondu avec le job côté consommateur.
                correlation_id=correlation_id or durable_message_id,
                source_scope=source_scope or self.sender_scope,
                source_instance_id=self.instance_id,
                target_scope=target_scope or self.team,
                target_instance_id=recipient,
                parent_event_id=parent_event_id,
                parent_task_id=parent_task_id,
                phase="team_transport",
            )
        elif not isinstance(handoff_context, HandoffContext):
            raw_target = handoff_context.get("target", {}) if isinstance(handoff_context, dict) else {}
            if isinstance(raw_target, dict) and raw_target.get("instance_id") not in {None, recipient}:
                raise PermissionError("Handoff recipient mismatch")
            raw_scope = handoff_context.get("target", {}).get("scope") if isinstance(handoff_context, dict) and isinstance(handoff_context.get("target"), dict) else None
            if raw_scope not in {None, self.team}:
                raise PermissionError("Handoff team scope mismatch")
            handoff_context = HandoffContext.from_dict(handoff_context)
        # Apply the same scope check to already-instantiated envelopes. A
        # typed object must not bypass the mapping validation above.
        source_scope_value = handoff_context.source.get("scope")
        target_scope_value = handoff_context.target.get("scope")
        if source_scope_value not in {None, self.sender_scope} or target_scope_value not in {None, self.team}:
            raise PermissionError("Handoff team scope mismatch")
        if handoff_context.target.get("instance_id") not in {None, recipient}:
            raise PermissionError("Handoff recipient mismatch")
        # The durable row and its envelope must identify the same trace.  A
        # mismatch makes completion events impossible to correlate reliably
        # (and used to let an accidental caller silently fork a trace).
        context_correlation = str(handoff_context.correlation_id).strip()
        if correlation_id is not None and str(correlation_id).strip() and str(correlation_id).strip() != context_correlation:
            raise ValueError("correlation_id ne correspond pas au handoff")
        item = TeamMessage(
            id=durable_message_id,
            team=self.team,
            sender=self.instance_id,
            recipient=recipient,
            kind=str(kind or "message"),
            body=body,
            subject=_clip(subject, 300),
            correlation_id=context_correlation,
            priority=max(0, min(int(priority), 40)),
            created_at=time.time(),
            handoff_context=handoff_context,
        )
        with self._lock:
            self._connection.execute(
                "INSERT INTO team_messages(id, team, sender, recipient, kind, body, subject, correlation_id, priority, created_at, handoff_context, state_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (item.id, item.team, item.sender, item.recipient, item.kind, item.body, item.subject, item.correlation_id, item.priority, item.created_at, item.handoff_context.to_json(), item.state_version),
            )
            self._connection.commit()
        return item

    def inbox(self, *, limit: int = 20, unread_only: bool = True) -> list[TeamMessage]:
        self._ensure_open()
        limit = max(1, min(int(limit), 100))
        with self._lock:
            self._recover_expired_claims()
            # Never let a wildcard row be consumed by an arbitrary instance.
            where = "team=? AND recipient=?"
            args: list[Any] = [self.team, self.instance_id]
            if unread_only:
                where += " AND delivered_at IS NULL"
            rows = self._connection.execute(
                f"SELECT * FROM team_messages WHERE {where} ORDER BY priority DESC, created_at ASC LIMIT ?",
                (*args, limit),
            ).fetchall()
        return [self._row(row) for row in rows]

    def mark_delivered(self, message_id: str) -> bool:
        self._ensure_open()
        with self._lock:
            result = self._connection.execute(
                "UPDATE team_messages SET delivered_at=?, status=CASE WHEN kind='job' THEN 'in_progress' ELSE 'delivered' END, claim_owner=NULL, claim_expires_at=NULL WHERE id=? AND team=? AND recipient=? AND delivered_at IS NULL AND status='queued'",
                (time.time(), str(message_id), self.team, self.instance_id),
            )
            self._connection.commit()
            return result.rowcount > 0

    def get(self, message_id: str) -> TeamMessage | None:
        self._ensure_open()
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM team_messages WHERE id=? AND team=? AND (recipient=? OR sender=?)",
                (str(message_id), self.team, self.instance_id, self.instance_id),
            ).fetchone()
        return self._row(row) if row else None

    def complete_job(
        self,
        message_id: str,
        result: str,
        *,
        success: bool = True,
        claim_owner: str | None = None,
        claim_fence: int | None = None,
    ) -> TeamMessage:
        self._ensure_open()
        result = _clip(result, self.max_message_chars)
        if not result.strip():
            raise ValueError("Le résultat d'une délégation ne peut pas être vide.")
        status = "completed" if success else "failed"
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM team_messages WHERE id=? AND team=? AND recipient=? AND kind='job'",
                (str(message_id), self.team, self.instance_id),
            ).fetchone()
            if row is None:
                # A sender may read its own job, but cannot complete it.
                raise KeyError(f"Délégation inconnue : {message_id}")
            existing = self._row(row)
            if existing.status in {"completed", "failed"}:
                return existing

            row_claim_owner = row["claim_owner"] if "claim_owner" in row.keys() else None
            row_claim_expires = row["claim_expires_at"] if "claim_expires_at" in row.keys() else None
            row_claim_fence = int(row["claim_fence"] or 0) if "claim_fence" in row.keys() else 0
            cached_fence = self._claim_fences.get(str(message_id))
            effective_owner = claim_owner
            effective_fence = claim_fence
            if effective_owner is None and cached_fence is not None:
                effective_owner = self._claim_owner
            if effective_fence is None and cached_fence is not None:
                effective_fence = cached_fence

            now = time.time()
            claimed = row_claim_owner is not None or row_claim_expires is not None
            if claimed:
                if (
                    effective_owner is None
                    or effective_fence is None
                    or str(row_claim_owner) != str(effective_owner)
                    or row_claim_fence != int(effective_fence)
                    or row_claim_expires is None
                    or float(row_claim_expires) <= now
                ):
                    raise PermissionError("Team job completion claim is stale or missing")
            next_version = existing.state_version + 1
            context = existing.handoff_context
            if context is not None:
                context = context.with_state(status, result=result, error=None if success else result)
            if claimed:
                transition_now = time.time()
                changed = self._connection.execute(
                    "UPDATE team_messages SET status=?, result=?, error=?, delivered_at=COALESCE(delivered_at, ?), claim_owner=NULL, claim_expires_at=NULL, handoff_context=?, state_version=? WHERE id=? AND team=? AND recipient=? AND kind='job' AND status='in_progress' AND claim_owner=? AND claim_fence=? AND claim_expires_at>?",
                    (
                        status,
                        result,
                        None if success else _clip(result, 2000),
                        transition_now,
                        context.to_json() if context else None,
                        next_version,
                        str(message_id),
                        self.team,
                        self.instance_id,
                        str(effective_owner),
                        int(effective_fence),
                        transition_now,
                    ),
                )
            else:
                # Backwards compatibility for legacy/manual unclaimed jobs.
                changed = self._connection.execute(
                    "UPDATE team_messages SET status=?, result=?, error=?, delivered_at=COALESCE(delivered_at, ?), handoff_context=?, state_version=? WHERE id=? AND team=? AND recipient=? AND kind='job' AND status IN ('queued', 'in_progress') AND claim_owner IS NULL AND claim_expires_at IS NULL",
                    (
                        status,
                        result,
                        None if success else _clip(result, 2000),
                        now,
                        context.to_json() if context else None,
                        next_version,
                        str(message_id),
                        self.team,
                        self.instance_id,
                    ),
                )
            if changed.rowcount:
                item = self._row(self._connection.execute("SELECT * FROM team_messages WHERE id=?", (str(message_id),)).fetchone())
                if context is not None:
                    key = f"{context.handoff_id}:{next_version}"
                    payload = {
                        "event_type": "handoff.completed" if success else "handoff.failed",
                        "message": item.to_dict(),
                        "handoff_context": context.to_dict(),
                        "handoff_id": context.handoff_id,
                        "correlation_id": context.correlation_id,
                        "parent_event_id": context.parent.get("event_id"),
                        "state_version": next_version,
                        "completion_key": key,
                        "internal_event": True,
                        "provenance": {
                            "kind": "team_job",
                            "job_id": item.id,
                            "delegated_by_instance_id": item.sender,
                            "producer_instance_id": item.recipient,
                            "handoff_id": context.handoff_id,
                            "correlation_id": context.correlation_id,
                        },
                        "outcome": {
                            "status": item.status,
                            "terminal": item.status in {"completed", "failed"},
                            "result": item.result,
                            "error": item.error,
                        },
                    }
                    self._connection.execute(
                        "INSERT OR IGNORE INTO completion_notifications(key, team, sender, recipient, message_id, state_version, payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (key, self.team, item.sender, self.instance_id, item.id, next_version, __import__("json").dumps(payload, ensure_ascii=False), time.time()),
                    )
                self._connection.commit()
            else:
                self._connection.commit()
            if changed.rowcount == 0:
                # Only the recipient is allowed to acknowledge a job.  A
                # sender can observe the terminal row through get(), but
                # cannot use completion as an implicit permission check.
                item = self._get_for_recipient(message_id)
                if item is None or item.kind != "job" or item.status not in {"completed", "failed"}:
                    raise KeyError(f"Délégation inconnue : {message_id}")
                # Runtime retries may repeat a completion after a crash.
                return item
            self._claim_fences.pop(str(message_id), None)
            self._published_ids.discard(str(message_id))
        item = self.get(message_id)
        assert item is not None
        self._replay_completion_notifications()
        return item

    def start(self) -> "TeamBus":
        if self._closed:
            raise RuntimeError("TeamBus est fermé.")
        if self.event_handler is None:
            return self
        self._replay_completion_notifications()
        with self._lifecycle_lock:
            if self.running:
                return self
            self._stop.clear()
            self._heartbeat_stop.clear()
            if self._heartbeat_thread is None or not self._heartbeat_thread.is_alive():
                self._heartbeat_thread = threading.Thread(
                    target=self._heartbeat,
                    name=f"orion-team-heartbeat-{self.instance_id}",
                    daemon=True,
                )
                self._heartbeat_thread.start()
            self._thread = threading.Thread(target=self._poll, name=f"orion-team-{self.instance_id}", daemon=True)
            self._thread.start()
        return self

    def stop(self, *, wait: bool = True) -> None:
        with self._lifecycle_lock:
            self._stop.set()
            thread = self._thread
        if wait and thread is not None:
            # The poller uses bounded queue waits, but SQLite may briefly wait
            # for another process. Never close the connection while it can
            # still be using it.
            thread.join()
        with self._lifecycle_lock:
            if thread is self._thread and (thread is None or not thread.is_alive()):
                self._thread = None

    def _poll(self) -> None:
        while not self._stop.is_set():
            self._recover_failed_published_deliveries()
            self._replay_completion_notifications()
            for message in self.inbox(limit=20):
                if message.id in self._published_ids:
                    continue
                # Claim before publish. A worker may run the callback as soon
                # as publish returns, and a second poller must not duplicate it.
                if not self._claim_for_poll(message.id):
                    continue
                claimed = self.get(message.id)
                if claimed is None:
                    self._release_delivery(message.id)
                    continue
                try:
                    event_payload = claimed.to_dict()
                    # Transport claim timestamps/fences are deliberately not
                    # part of the durable event identity. A replay after a
                    # crash must be byte-equivalent for EventHandler dedupe.
                    event_payload["delivered_at"] = None
                    self.event_handler.publish(
                        f"team.{message.kind}",
                        event_payload,
                        priority=claimed.priority,
                        source=f"team:{claimed.sender}",
                        metadata={
                            "team_message_id": message.id,
                            "team": message.team,
                            "internal_event": True,
                        },
                        max_attempts=1,
                        idempotency_key=self._delivery_idempotency_key(claimed),
                        # Do not let a bounded event queue hold the claim longer
                        # than its lease before the dedicated heartbeat can help.
                        timeout=min(self.poll_interval, self.claim_timeout / 2.0, 0.25),
                    )
                except Exception:
                    self._release_delivery(message.id)
                    continue
                self._published_ids.add(message.id)
                # Refresh immediately after enqueue; the dedicated heartbeat
                # maintains the lease until acknowledge_delivery().
                self._renew_delivery_claim(message.id)
            self._stop.wait(self.poll_interval)

    def _heartbeat(self) -> None:
        """Renew active delivery claims independently from mailbox polling.

        ``stop()`` quiesces new mailbox intake but deliberately leaves this
        heartbeat alive while a runtime-owned delivery is still in flight.
        That matches OrionApplication shutdown ordering: accepted team events
        can finish and acknowledge without their lease being stolen mid-drain.
        ``close()`` is the hard lifecycle boundary and always stops this thread.
        """
        while not self._heartbeat_stop.is_set():
            owned = self._renew_owned_claims()
            if self._stop.is_set() and owned == 0:
                return
            self._heartbeat_stop.wait(self._claim_heartbeat_interval)

    def _release_delivery(self, message_id: str) -> None:
        """Return a claimed, not-yet-completed row to the durable inbox."""
        with self._lock:
            self._connection.execute(
                "UPDATE team_messages SET delivered_at=NULL, status='queued', claim_owner=NULL, claim_expires_at=NULL WHERE id=? AND team=? AND recipient=? AND result IS NULL AND claim_owner=? AND status IN ('delivered', 'in_progress')",
                (str(message_id), self.team, self.instance_id, self._claim_owner),
            )
            self._connection.commit()
            self._published_ids.discard(str(message_id))
            self._claim_fences.pop(str(message_id), None)

    def _claim_for_poll(self, message_id: str) -> bool:
        """Atomically claim a queued row for the local publish handoff."""
        now = time.time()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                result = self._connection.execute(
                    """UPDATE team_messages
                       SET delivered_at=?,
                           status=CASE WHEN kind='job' THEN 'in_progress' ELSE 'delivered' END,
                           claim_owner=?, claim_expires_at=?,
                           claim_fence=COALESCE(claim_fence, 0) + 1
                     WHERE id=? AND team=? AND recipient=? AND delivered_at IS NULL
                       AND status='queued'""",
                    (
                        now,
                        self._claim_owner,
                        now + self.claim_timeout,
                        str(message_id),
                        self.team,
                        self.instance_id,
                    ),
                )
                if result.rowcount > 0:
                    row = self._connection.execute(
                        "SELECT claim_fence FROM team_messages WHERE id=?",
                        (str(message_id),),
                    ).fetchone()
                    if row is not None:
                        self._claim_fences[str(message_id)] = int(row["claim_fence"] or 0)
                self._connection.commit()
                return result.rowcount > 0
            except Exception:
                self._connection.rollback()
                raise

    def acknowledge_delivery(self, message_id: str) -> bool:
        """Acknowledge an event after its local runtime processed it.

        Enqueue acceptance alone keeps the lease until this point, so a
        process crash after enqueue can recover the row after expiration.

        A team job is different from a plain message: consuming its runtime
        event is not the same thing as completing the delegated work.  If the
        runtime returns without ``complete_job()``, put that job back in the
        durable queue under a new state version so it cannot silently remain
        ``in_progress`` forever.
        """
        self._ensure_open()
        with self._lock:
            row = self._connection.execute(
                "SELECT kind, status, result FROM team_messages WHERE id=? AND team=? AND recipient=? AND claim_owner=?",
                (str(message_id), self.team, self.instance_id, self._claim_owner),
            ).fetchone()
            if (
                row is not None
                and str(row["kind"]) == "job"
                and str(row["status"]) == "in_progress"
                and row["result"] is None
            ):
                changed = self._connection.execute(
                    """UPDATE team_messages
                          SET delivered_at=NULL, status='queued',
                              claim_owner=NULL, claim_expires_at=NULL,
                              state_version=state_version+1
                        WHERE id=? AND team=? AND recipient=? AND claim_owner=?
                          AND kind='job' AND status='in_progress' AND result IS NULL""",
                    (str(message_id), self.team, self.instance_id, self._claim_owner),
                )
            else:
                changed = self._connection.execute(
                    "UPDATE team_messages SET claim_owner=NULL, claim_expires_at=NULL WHERE id=? AND team=? AND recipient=? AND claim_owner=? AND status IN ('delivered', 'in_progress')",
                    (str(message_id), self.team, self.instance_id, self._claim_owner),
                )
            self._connection.commit()
            acknowledged = changed.rowcount > 0
            if acknowledged:
                self._published_ids.discard(str(message_id))
                self._claim_fences.pop(str(message_id), None)
            return acknowledged

    @staticmethod
    def _delivery_idempotency_key(message: TeamMessage) -> str:
        return f"team:delivery:{message.id}:v{message.state_version}"

    @staticmethod
    def _completion_idempotency_key(key: str) -> str:
        return f"team:completion:{key}"

    def _renew_delivery_claim(self, message_id: str) -> bool:
        """Renew one delivery only if this TeamBus incarnation still owns it."""
        now = time.time()
        with self._lock:
            changed = self._connection.execute(
                """UPDATE team_messages
                      SET claim_expires_at=?
                    WHERE id=? AND team=? AND recipient=? AND claim_owner=?
                      AND claim_expires_at IS NOT NULL
                      AND claim_expires_at > ?
                      AND status IN ('delivered', 'in_progress')""",
                (
                    now + self.claim_timeout,
                    str(message_id),
                    self.team,
                    self.instance_id,
                    self._claim_owner,
                    now,
                ),
            )
            self._connection.commit()
            return changed.rowcount > 0

    def _renew_owned_claims(self) -> int:
        """Heartbeat all non-terminal claims held by this bus incarnation."""
        now = time.time()
        with self._lock:
            changed_messages = self._connection.execute(
                """UPDATE team_messages
                      SET claim_expires_at=?
                    WHERE team=? AND recipient=? AND result IS NULL
                      AND claim_owner=? AND claim_expires_at IS NOT NULL
                      AND claim_expires_at > ?
                      AND status IN ('delivered', 'in_progress')""",
                (
                    now + self.claim_timeout,
                    self.team,
                    self.instance_id,
                    self._claim_owner,
                    now,
                ),
            )
            changed_completions = self._connection.execute(
                """UPDATE completion_notifications
                      SET claim_expires_at=?
                    WHERE team=? AND sender=? AND acknowledged=0
                      AND delivery_failed=0
                      AND claim_owner=? AND claim_expires_at IS NOT NULL
                      AND claim_expires_at > ?""",
                (
                    now + self.claim_timeout,
                    self.team,
                    self.instance_id,
                    self._claim_owner,
                    now,
                ),
            )
            self._connection.commit()
            return int(changed_messages.rowcount) + int(changed_completions.rowcount)

    def _recover_failed_published_deliveries(self) -> int:
        """Bounded recovery for team deliveries rejected by durable EventHandler."""
        if self.event_handler is None:
            return 0
        with self._lock:
            message_ids = list(self._published_ids)
        recovered = 0
        for message_id in message_ids:
            message = self.get(message_id)
            if message is None:
                with self._lock:
                    self._published_ids.discard(message_id)
                    self._claim_fences.pop(message_id, None)
                continue
            with self._lock:
                row = self._connection.execute(
                    "SELECT claim_owner, delivery_recoveries, delivery_failed "
                    "FROM team_messages WHERE id=? AND team=? AND recipient=?",
                    (message_id, self.team, self.instance_id),
                ).fetchone()
            if row is None:
                continue
            if row["claim_owner"] != self._claim_owner:
                with self._lock:
                    self._published_ids.discard(message_id)
                    self._claim_fences.pop(message_id, None)
                continue
            stable_key = self._delivery_idempotency_key(message)
            receipt = _durable_event_receipt(self.event_handler, stable_key)
            if receipt is None or str(getattr(receipt, "status", "")) != "failed":
                continue
            attempts = int(row["delivery_recoveries"] or 0)
            if bool(row["delivery_failed"]) or attempts >= self._MAX_DOWNSTREAM_RECOVERIES:
                error = "Team event delivery failed after durable retries."
                if message.kind == "job":
                    try:
                        self.complete_job(message_id, error, success=False)
                    except (KeyError, PermissionError, RuntimeError):
                        pass
                else:
                    with self._lock:
                        self._connection.execute(
                            """UPDATE team_messages
                                  SET status='failed', error=?, delivery_failed=1,
                                      claim_owner=NULL, claim_expires_at=NULL
                                WHERE id=? AND team=? AND recipient=? AND claim_owner=?""",
                            (error, message_id, self.team, self.instance_id, self._claim_owner),
                        )
                        self._connection.commit()
                        self._published_ids.discard(message_id)
                        self._claim_fences.pop(message_id, None)
                continue
            if _revive_failed_event_receipt(self.event_handler, stable_key):
                with self._lock:
                    self._connection.execute(
                        "UPDATE team_messages SET delivery_recoveries=delivery_recoveries+1 "
                        "WHERE id=? AND team=? AND recipient=? AND claim_owner=?",
                        (message_id, self.team, self.instance_id, self._claim_owner),
                    )
                    self._connection.commit()
                recovered += 1
        return recovered

    def _claim_completion(self, key: str) -> bool:
        """Claim one sender-addressed completion with a fenced durable lease."""
        now = time.time()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                changed = self._connection.execute(
                    """UPDATE completion_notifications
                          SET claim_owner=?, claim_expires_at=?,
                              claim_fence=COALESCE(claim_fence, 0) + 1
                        WHERE key=? AND team=? AND sender=? AND acknowledged=0
                          AND delivery_failed=0
                          AND (claim_owner IS NULL OR claim_expires_at IS NULL OR claim_expires_at<=?)""",
                    (
                        self._claim_owner,
                        now + self.claim_timeout,
                        str(key),
                        self.team,
                        self.instance_id,
                        now,
                    ),
                )
                if changed.rowcount:
                    row = self._connection.execute(
                        "SELECT claim_fence FROM completion_notifications WHERE key=?",
                        (str(key),),
                    ).fetchone()
                    if row is not None:
                        self._completion_claim_fences[str(key)] = int(
                            row["claim_fence"] or 0
                        )
                self._connection.commit()
                return changed.rowcount > 0
            except Exception:
                self._connection.rollback()
                raise

    def _release_completion(self, key: str) -> None:
        with self._lock:
            fence = self._completion_claim_fences.get(str(key))
            if fence is not None:
                self._connection.execute(
                    """UPDATE completion_notifications
                          SET claim_owner=NULL, claim_expires_at=NULL
                        WHERE key=? AND team=? AND sender=? AND acknowledged=0
                          AND claim_owner=? AND claim_fence=?""",
                    (
                        str(key),
                        self.team,
                        self.instance_id,
                        self._claim_owner,
                        int(fence),
                    ),
                )
                self._connection.commit()
            self._completion_claim_fences.pop(str(key), None)
            self._published_completion_keys.discard(str(key))

    def _recover_failed_completion(self, key: str) -> bool:
        stable_key = self._completion_idempotency_key(key)
        receipt = _durable_event_receipt(self.event_handler, stable_key)
        if receipt is None or str(getattr(receipt, "status", "")) != "failed":
            return False
        with self._lock:
            row = self._connection.execute(
                "SELECT delivery_recoveries, delivery_failed, claim_owner "
                "FROM completion_notifications WHERE key=? AND team=? AND sender=?",
                (str(key), self.team, self.instance_id),
            ).fetchone()
            if row is None or row["claim_owner"] != self._claim_owner:
                return False
            attempts = int(row["delivery_recoveries"] or 0)
            if bool(row["delivery_failed"]) or attempts >= self._MAX_DOWNSTREAM_RECOVERIES:
                self._connection.execute(
                    """UPDATE completion_notifications
                          SET delivery_failed=1, claim_owner=NULL, claim_expires_at=NULL
                        WHERE key=? AND team=? AND sender=? AND claim_owner=?""",
                    (str(key), self.team, self.instance_id, self._claim_owner),
                )
                self._connection.commit()
                self._published_completion_keys.discard(str(key))
                self._completion_claim_fences.pop(str(key), None)
                return False
        if not _revive_failed_event_receipt(self.event_handler, stable_key):
            return False
        with self._lock:
            self._connection.execute(
                "UPDATE completion_notifications SET delivery_recoveries=delivery_recoveries+1 "
                "WHERE key=? AND team=? AND sender=? AND claim_owner=?",
                (str(key), self.team, self.instance_id, self._claim_owner),
            )
            self._connection.commit()
        return True

    def acknowledge_completion(self, key: str) -> bool:
        """Acknowledge a sender-addressed terminal notification by key."""
        self._ensure_open()
        with self._lock:
            fence = self._completion_claim_fences.get(str(key))
            if fence is not None:
                changed = self._connection.execute(
                    """UPDATE completion_notifications
                          SET acknowledged=1, claim_owner=NULL, claim_expires_at=NULL
                        WHERE key=? AND team=? AND sender=? AND acknowledged=0
                          AND claim_owner=? AND claim_fence=? AND claim_expires_at>?""",
                    (
                        str(key),
                        self.team,
                        self.instance_id,
                        self._claim_owner,
                        int(fence),
                        time.time(),
                    ),
                )
            else:
                # Legacy/manual notifications that were never claimed remain
                # acknowledgeable; an active claim owned by another incarnation
                # is never bypassed by this compatibility path.
                changed = self._connection.execute(
                    """UPDATE completion_notifications
                          SET acknowledged=1
                        WHERE key=? AND team=? AND sender=? AND acknowledged=0
                          AND claim_owner IS NULL""",
                    (str(key), self.team, self.instance_id),
                )
            self._connection.commit()
            if changed.rowcount:
                self._published_completion_keys.discard(str(key))
                self._completion_claim_fences.pop(str(key), None)
            return changed.rowcount > 0

    def pending_completions(self, *, limit: int = 20) -> list[dict[str, Any]]:
        self._ensure_open()
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM completion_notifications WHERE team=? AND sender=? AND acknowledged=0 ORDER BY created_at LIMIT ?",
                (self.team, self.instance_id, max(1, min(int(limit), 100))),
            ).fetchall()
        import json
        return [json.loads(str(row["payload"])) for row in rows]

    def _replay_completion_notifications(self) -> None:
        if self.event_handler is None:
            return
        import json
        with self._lock:
            self._recover_expired_claims()
            rows = self._connection.execute(
                "SELECT * FROM completion_notifications WHERE team=? AND sender=? AND acknowledged=0 AND delivery_failed=0 ORDER BY created_at LIMIT 100",
                (self.team, self.instance_id),
            ).fetchall()
        for row in rows:
            key = str(row["key"])
            if key in self._published_completion_keys:
                if row["claim_owner"] == self._claim_owner:
                    self._recover_failed_completion(key)
                    continue
                with self._lock:
                    self._published_completion_keys.discard(key)
                    self._completion_claim_fences.pop(key, None)
            if not self._claim_completion(key):
                continue
            try:
                payload = json.loads(str(row["payload"]))
                self.event_handler.publish(
                    payload.get("event_type", "handoff.completed"), payload,
                    priority=EventPriority.NORMAL,
                    source=f"team:{row['recipient']}",
                    metadata={"handoff_id": payload.get("handoff_id"), "correlation_id": payload.get("correlation_id"), "parent_event_id": payload.get("parent_event_id"), "state_version": int(row["state_version"]), "completion_key": key, "internal_event": True},
                    max_attempts=1,
                    idempotency_key=self._completion_idempotency_key(key),
                )
            except Exception:
                self._release_completion(key)
                continue
            self._published_completion_keys.add(key)

    def _recover_expired_claims(self) -> None:
        now = time.time()
        self._connection.execute(
            """UPDATE team_messages
                  SET delivered_at=NULL, status='queued', claim_owner=NULL,
                      claim_expires_at=NULL
                WHERE team=? AND recipient=? AND result IS NULL
                  AND claim_expires_at IS NOT NULL AND claim_expires_at <= ?
                  AND status IN ('delivered', 'in_progress')""",
            (self.team, self.instance_id, now),
        )
        self._connection.execute(
            """UPDATE completion_notifications
                  SET claim_owner=NULL, claim_expires_at=NULL
                WHERE team=? AND sender=? AND acknowledged=0
                  AND delivery_failed=0
                  AND claim_expires_at IS NOT NULL AND claim_expires_at<=?""",
            (self.team, self.instance_id, now),
        )
        self._connection.commit()

    def _release_owned_claims(self) -> None:
        self._connection.execute(
            """UPDATE team_messages
                  SET delivered_at=NULL, status='queued', claim_owner=NULL,
                      claim_expires_at=NULL
                WHERE team=? AND recipient=? AND result IS NULL
                  AND claim_owner=? AND status IN ('delivered', 'in_progress')""",
            (self.team, self.instance_id, self._claim_owner),
        )
        self._connection.execute(
            """UPDATE completion_notifications
                  SET claim_owner=NULL, claim_expires_at=NULL
                WHERE team=? AND sender=? AND acknowledged=0 AND claim_owner=?""",
            (self.team, self.instance_id, self._claim_owner),
        )
        self._connection.commit()
        self._completion_claim_fences.clear()
        self._published_completion_keys.clear()

    def _get_for_recipient(self, message_id: str) -> TeamMessage | None:
        row = self._connection.execute(
            "SELECT * FROM team_messages WHERE id=? AND team=? AND recipient=?",
            (str(message_id), self.team, self.instance_id),
        ).fetchone()
        return self._row(row) if row else None

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("TeamBus est fermé.")

    @staticmethod
    def _row(row: sqlite3.Row) -> TeamMessage:
        import json
        raw_context = row["handoff_context"] if "handoff_context" in row.keys() else None
        status = str(row["status"] or "queued")
        handoff_context = HandoffContext.from_dict(json.loads(raw_context)) if raw_context else None
        # The SQL row is authoritative for transport/job lifecycle.  Older
        # rows (and the claim fast path) can still contain a handoff envelope
        # whose state says ``queued`` while the durable row is already
        # ``in_progress``.  Normalize the model-facing envelope on read so one
        # payload never presents contradictory states.
        if handoff_context is not None and str(row["kind"]) == "job":
            mapped_status = {
                "queued": "queued",
                "in_progress": "running",
                "completed": "completed",
                "failed": "failed",
            }.get(status)
            if mapped_status is not None and handoff_context.state.get("status") != mapped_status:
                try:
                    normalized = handoff_context.with_state(
                        mapped_status,
                        result=row["result"] if mapped_status == "completed" else None,
                        error=row["error"] if mapped_status == "failed" else None,
                    )
                    # Read-time normalization must be pure: changing updated_at
                    # here would make an identical durable delivery serialize
                    # differently on replay and defeat stable event identity.
                    handoff_context = HandoffContext(
                        **{
                            **normalized.__dict__,
                            "updated_at": handoff_context.updated_at,
                        }
                    )
                except ValueError:
                    # A malformed legacy terminal envelope must not make the
                    # otherwise readable durable message unavailable.
                    pass
        return TeamMessage(
            id=str(row["id"]), team=str(row["team"]), sender=str(row["sender"]),
            recipient=str(row["recipient"]), kind=str(row["kind"]), body=str(row["body"]),
            subject=str(row["subject"]), correlation_id=row["correlation_id"],
            priority=int(row["priority"]), created_at=float(row["created_at"]),
            delivered_at=float(row["delivered_at"]) if row["delivered_at"] is not None else None,
            status=status, result=row["result"], error=row["error"],
            handoff_context=handoff_context,
            state_version=int(row["state_version"] or 0) if "state_version" in row.keys() else 0,
        )


__all__ = ["TeamBus", "TeamMessage"]
