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
            # A claim is only held while handing an item to the local event
            # queue.  It is cleared after publish, so a crash in that small
            # window can be recovered without redelivering acknowledged rows.
            "claim_owner": "TEXT",
            "claim_expires_at": "REAL",
            "handoff_context": "TEXT",
            "state_version": "INTEGER NOT NULL DEFAULT 0",
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
        self._claim_owner = uuid.uuid4().hex
        self._published_ids: set[str] = set()
        self._published_completion_keys: set[str] = set()
        self.sender_scope = str(sender_scope or self.team).strip() or self.team
        self._closed = False
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS completion_notifications (
                key TEXT PRIMARY KEY, team TEXT NOT NULL, sender TEXT NOT NULL,
                recipient TEXT NOT NULL, message_id TEXT NOT NULL, state_version INTEGER NOT NULL,
                payload TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            )"""
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

    def close(self) -> None:
        self.stop()
        with self._lock:
            if not self._closed:
                self._release_owned_claims()
                self._connection.close()
                self._closed = True

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

    def complete_job(self, message_id: str, result: str, *, success: bool = True) -> TeamMessage:
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
            next_version = existing.state_version + 1
            context = existing.handoff_context
            if context is not None:
                context = context.with_state(status, result=result, error=None if success else result)
            changed = self._connection.execute(
                "UPDATE team_messages SET status=?, result=?, error=?, delivered_at=COALESCE(delivered_at, ?), claim_owner=NULL, claim_expires_at=NULL, handoff_context=?, state_version=? WHERE id=? AND team=? AND recipient=? AND kind='job' AND status IN ('queued', 'in_progress')",
                (status, result, None if success else _clip(result, 2000), time.time(), context.to_json() if context else None, next_version, str(message_id), self.team, self.instance_id),
            )
            if changed.rowcount:
                item = self._row(self._connection.execute("SELECT * FROM team_messages WHERE id=?", (str(message_id),)).fetchone())
                if context is not None:
                    key = f"{context.handoff_id}:{next_version}"
                    payload = {"event_type": "handoff.completed" if success else "handoff.failed", "message": item.to_dict(), "handoff_context": context.to_dict(), "handoff_id": context.handoff_id, "correlation_id": context.correlation_id, "parent_event_id": context.parent.get("event_id"), "state_version": next_version, "completion_key": key, "internal_event": True}
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
                    self.event_handler.publish(
                        f"team.{message.kind}",
                        claimed.to_dict(),
                        priority=claimed.priority,
                        source=f"team:{claimed.sender}",
                        metadata={"team_message_id": message.id, "team": message.team, "internal_event": True},
                        max_attempts=1,
                        # Do not let a bounded event queue prevent stop().
                        timeout=min(self.poll_interval, 0.25),
                    )
                except Exception:
                    # Enqueue failure is recoverable. A crash between this claim
                    # and publish can lose a handoff; this is not exactly-once.
                    self._release_delivery(message.id)
                    continue
                self._published_ids.add(message.id)
            self._stop.wait(self.poll_interval)

    def _release_delivery(self, message_id: str) -> None:
        """Return a claimed, not-yet-completed row to the durable inbox."""
        with self._lock:
            self._connection.execute(
                "UPDATE team_messages SET delivered_at=NULL, status='queued', claim_owner=NULL, claim_expires_at=NULL WHERE id=? AND team=? AND recipient=? AND result IS NULL AND claim_owner=? AND status IN ('delivered', 'in_progress')",
                (str(message_id), self.team, self.instance_id, self._claim_owner),
            )
            self._connection.commit()
            self._published_ids.discard(str(message_id))

    def _claim_for_poll(self, message_id: str) -> bool:
        """Atomically claim a queued row for the local publish handoff."""
        now = time.time()
        with self._lock:
            result = self._connection.execute(
                """UPDATE team_messages
                   SET delivered_at=?,
                       status=CASE WHEN kind='job' THEN 'in_progress' ELSE 'delivered' END,
                       claim_owner=?, claim_expires_at=?
                 WHERE id=? AND team=? AND recipient=? AND delivered_at IS NULL
                   AND status='queued'""",
                (now, self._claim_owner, now + self.claim_timeout, str(message_id), self.team, self.instance_id),
            )
            self._connection.commit()
            return result.rowcount > 0

    def acknowledge_delivery(self, message_id: str) -> bool:
        """Acknowledge an event after its local runtime processed it.

        Enqueue acceptance alone keeps the lease until this point, so a
        process crash after enqueue can recover the row after expiration.
        """
        self._ensure_open()
        with self._lock:
            changed = self._connection.execute(
                "UPDATE team_messages SET claim_owner=NULL, claim_expires_at=NULL WHERE id=? AND team=? AND recipient=? AND claim_owner=? AND status IN ('delivered', 'in_progress')",
                (str(message_id), self.team, self.instance_id, self._claim_owner),
            )
            self._connection.commit()
            return changed.rowcount > 0

    def acknowledge_completion(self, key: str) -> bool:
        """Acknowledge a sender-addressed terminal notification by key."""
        self._ensure_open()
        with self._lock:
            changed = self._connection.execute(
                "UPDATE completion_notifications SET acknowledged=1 WHERE key=? AND team=? AND sender=?",
                (str(key), self.team, self.instance_id),
            )
            self._connection.commit()
            self._published_completion_keys.discard(str(key))
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
            rows = self._connection.execute(
                "SELECT * FROM completion_notifications WHERE team=? AND sender=? AND acknowledged=0 ORDER BY created_at LIMIT 100",
                (self.team, self.instance_id),
            ).fetchall()
        for row in rows:
            key = str(row["key"])
            if key in self._published_completion_keys:
                continue
            try:
                payload = json.loads(str(row["payload"]))
                self.event_handler.publish(
                    payload.get("event_type", "handoff.completed"), payload,
                    priority=EventPriority.NORMAL,
                    source=f"team:{row['recipient']}",
                    metadata={"handoff_id": payload.get("handoff_id"), "correlation_id": payload.get("correlation_id"), "parent_event_id": payload.get("parent_event_id"), "state_version": int(row["state_version"]), "completion_key": key, "internal_event": True},
                    max_attempts=1,
                )
            except Exception:
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
        self._connection.commit()

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
        return TeamMessage(
            id=str(row["id"]), team=str(row["team"]), sender=str(row["sender"]),
            recipient=str(row["recipient"]), kind=str(row["kind"]), body=str(row["body"]),
            subject=str(row["subject"]), correlation_id=row["correlation_id"],
            priority=int(row["priority"]), created_at=float(row["created_at"]),
            delivered_at=float(row["delivered_at"]) if row["delivered_at"] is not None else None,
            status=str(row["status"] or "queued"), result=row["result"], error=row["error"],
            handoff_context=HandoffContext.from_dict(json.loads(raw_context)) if raw_context else None,
            state_version=int(row["state_version"] or 0) if "state_version" in row.keys() else 0,
        )


__all__ = ["TeamBus", "TeamMessage"]
