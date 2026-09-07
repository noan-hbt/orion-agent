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

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
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
        self._closed = False

        # Databases created by the first bus version have no status for rows
        # that were already delivered.  Normalize those rows once so they do
        # not look like fresh queued jobs after a migration.
        with self._lock:
            self._connection.execute(
                "UPDATE team_messages SET status=CASE WHEN kind='job' THEN 'in_progress' ELSE 'delivered' END WHERE delivered_at IS NOT NULL AND status='queued'"
            )
            self._connection.commit()

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
    ) -> TeamMessage:
        self._ensure_open()
        recipient = str(recipient).strip()
        body = _clip(body, self.max_message_chars)
        if "*" in recipient:
            raise ValueError("Les messages d'équipe doivent viser une instance précise.")
        if not recipient or not body:
            raise ValueError("recipient et body sont obligatoires.")
        item = TeamMessage(
            id=str(message_id or uuid.uuid4().hex[:16]),
            team=self.team,
            sender=self.instance_id,
            recipient=recipient,
            kind=str(kind or "message"),
            body=body,
            subject=_clip(subject, 300),
            correlation_id=str(correlation_id) if correlation_id else None,
            priority=max(0, min(int(priority), 40)),
            created_at=time.time(),
        )
        with self._lock:
            self._connection.execute(
                "INSERT INTO team_messages(id, team, sender, recipient, kind, body, subject, correlation_id, priority, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (item.id, item.team, item.sender, item.recipient, item.kind, item.body, item.subject, item.correlation_id, item.priority, item.created_at),
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
            changed = self._connection.execute(
                "UPDATE team_messages SET status=?, result=?, error=?, delivered_at=COALESCE(delivered_at, ?), claim_owner=NULL, claim_expires_at=NULL WHERE id=? AND team=? AND recipient=? AND kind='job' AND status IN ('queued', 'in_progress')",
                (status, result, None if success else _clip(result, 2000), time.time(), str(message_id), self.team, self.instance_id),
            )
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
        return item

    def start(self) -> "TeamBus":
        if self._closed:
            raise RuntimeError("TeamBus est fermé.")
        if self.event_handler is None:
            return self
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
                "UPDATE team_messages SET claim_owner=NULL, claim_expires_at=NULL WHERE id=? AND team=? AND recipient=? AND claim_owner=? AND status='delivered'",
                (str(message_id), self.team, self.instance_id, self._claim_owner),
            )
            self._connection.commit()
            return changed.rowcount > 0

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
        return TeamMessage(
            id=str(row["id"]), team=str(row["team"]), sender=str(row["sender"]),
            recipient=str(row["recipient"]), kind=str(row["kind"]), body=str(row["body"]),
            subject=str(row["subject"]), correlation_id=row["correlation_id"],
            priority=int(row["priority"]), created_at=float(row["created_at"]),
            delivered_at=float(row["delivered_at"]) if row["delivered_at"] is not None else None,
            status=str(row["status"] or "queued"), result=row["result"], error=row["error"],
        )


__all__ = ["TeamBus", "TeamMessage"]
