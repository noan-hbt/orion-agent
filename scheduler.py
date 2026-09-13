"""Scheduler persistant pour réveiller l'agent à une date donnée.

Le scheduler ne crée ni ne modifie de tâche. Il publie simplement un événement
``schedule`` contenant la référence de la tâche déjà associée au réveil.
"""

from __future__ import annotations

import copy
import heapq
import json
import os
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from event_handler import EventHandler, EventPriority, EventType


class _SingleWriterFileLock:
    """Lifetime, crash-safe single-writer lock for one JSON state file."""

    def __init__(self, target: Path) -> None:
        self.target = target.resolve()
        self.path = self.target.with_name(f".{self.target.name}.writer.lock")
        self._handle: Any | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError(
                f"JsonScheduleStore est déjà ouvert par un autre writer : {self.target}"
            ) from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __del__(self) -> None:
        try:
            self.release()
        except Exception:
            pass


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
    """Requeue the same FAILED durable EventHandler receipt and stable key."""
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


class ScheduleStatus(str, Enum):
    ACTIVE = "active"
    RETRYING = "retrying"
    FIRED = "fired"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("run_at doit contenir un fuseau horaire.")
    return value.astimezone(timezone.utc)


def _datetime_to_string(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse_utc(value: Any) -> datetime | None:
    """Coerce an ISO string or datetime to an aware UTC datetime.

    ``run_at`` is validated by ``__post_init__`` (a naive value raises), but the
    other stamps were stored with a plain ``fromisoformat`` and could come back
    naive.  ``snapshot()`` then compared a naive ``created_at`` against an aware
    ``run_at`` and raised ``TypeError`` into the health surface.  Normalising
    every stamp keeps comparisons total.
    """
    if value is None or value == "":
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass
class Schedule:
    """Réveil planifié et lié à une tâche existante."""

    run_at: datetime
    task_id: int
    payload: dict[str, Any] = field(default_factory=dict)
    priority: int = int(EventPriority.NORMAL)
    id: str = field(default_factory=lambda: f"schedule_{uuid.uuid4().hex[:12]}")
    version: int = 1
    status: ScheduleStatus = ScheduleStatus.ACTIVE
    created_at: datetime = field(default_factory=_now)
    fired_at: datetime | None = None
    publish_attempts: int = 0
    last_attempt_at: datetime | None = None
    last_error: str | None = None
    delivery_recoveries: int = 0
    delivery_failed: bool = False

    def __post_init__(self) -> None:
        self.run_at = _as_utc(self.run_at)
        self.task_id = int(self.task_id)
        self.version = int(self.version)
        self.publish_attempts = int(self.publish_attempts)
        self.delivery_recoveries = int(self.delivery_recoveries)
        if self.version < 1:
            raise ValueError(
                "La version du schedule doit être supérieure ou égale à un."
            )
        if self.publish_attempts < 0:
            raise ValueError("Le nombre de tentatives doit être positif ou nul.")
        if self.delivery_recoveries < 0:
            raise ValueError("Le nombre de reprises de livraison doit être positif ou nul.")
        self.priority = int(self.priority)
        if self.priority < 0:
            raise ValueError("La priorité du schedule doit être positive ou nulle.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "run_at": _datetime_to_string(self.run_at),
            "task_id": self.task_id,
            "payload": self.payload,
            "priority": self.priority,
            "status": self.status.value,
            "created_at": _datetime_to_string(self.created_at),
            "fired_at": _datetime_to_string(self.fired_at),
            "publish_attempts": self.publish_attempts,
            "last_attempt_at": _datetime_to_string(self.last_attempt_at),
            "last_error": self.last_error,
            "delivery_recoveries": self.delivery_recoveries,
            "delivery_failed": self.delivery_failed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Schedule:
        run_at = _parse_utc(data["run_at"])
        return cls(
            id=str(data.get("id") or f"schedule_{uuid.uuid4().hex[:12]}"),
            version=int(data.get("version", 1)),
            run_at=run_at,
            task_id=int(data["task_id"]),
            payload=dict(data.get("payload") or {}),
            priority=int(data.get("priority", EventPriority.NORMAL)),
            status=ScheduleStatus(data.get("status", ScheduleStatus.ACTIVE.value)),
            created_at=_parse_utc(data.get("created_at")) or _now(),
            fired_at=_parse_utc(data.get("fired_at")),
            publish_attempts=int(data.get("publish_attempts", 0)),
            last_attempt_at=_parse_utc(data.get("last_attempt_at")),
            last_error=str(data["last_error"])
            if data.get("last_error") is not None
            else None,
            delivery_recoveries=int(data.get("delivery_recoveries", 0)),
            delivery_failed=bool(data.get("delivery_failed", False)),
        )


class ScheduleStore(Protocol):
    def save(self, schedule: Schedule) -> Schedule: ...

    def get(self, schedule_id: str) -> Schedule | None: ...

    def list(self, *, status: ScheduleStatus | None = None) -> list[Schedule]: ...


class InMemoryScheduleStore:
    """Stockage thread-safe des schedules actifs et déclenchés."""

    def __init__(self) -> None:
        self._schedules: dict[str, Schedule] = {}
        self._lock = threading.RLock()
        self._pending_heap: list[tuple[datetime, str, int]] = []
        self._revisions: dict[str, int] = {}

    @staticmethod
    def _is_pending(schedule: Schedule) -> bool:
        return schedule.status in {ScheduleStatus.ACTIVE, ScheduleStatus.RETRYING}

    def _index_saved_locked(self, schedule: Schedule) -> None:
        revision = self._revisions.get(schedule.id, 0) + 1
        self._revisions[schedule.id] = revision
        if self._is_pending(schedule):
            heapq.heappush(self._pending_heap, (schedule.run_at, schedule.id, revision))

    def _rebuild_pending_index_locked(self) -> None:
        self._pending_heap = []
        self._revisions = {}
        for schedule in self._schedules.values():
            self._index_saved_locked(schedule)

    def _prune_pending_heap_locked(self) -> None:
        while self._pending_heap:
            run_at, schedule_id, revision = self._pending_heap[0]
            schedule = self._schedules.get(schedule_id)
            if (
                schedule is not None
                and self._revisions.get(schedule_id) == revision
                and self._is_pending(schedule)
                and schedule.run_at == run_at
            ):
                return
            heapq.heappop(self._pending_heap)

    def save(self, schedule: Schedule) -> Schedule:
        with self._lock:
            stored = copy.deepcopy(schedule)
            self._schedules[schedule.id] = stored
            self._index_saved_locked(stored)
            return copy.deepcopy(stored)

    def get(self, schedule_id: str) -> Schedule | None:
        with self._lock:
            schedule = self._schedules.get(schedule_id)
            return copy.deepcopy(schedule) if schedule else None

    def list(self, *, status: ScheduleStatus | None = None) -> list[Schedule]:
        with self._lock:
            schedules = list(self._schedules.values())
            if status is not None:
                schedules = [item for item in schedules if item.status == status]
            return copy.deepcopy(sorted(schedules, key=lambda item: item.run_at))

    def _next_pending_at(self) -> datetime | None:
        with self._lock:
            self._prune_pending_heap_locked()
            return self._pending_heap[0][0] if self._pending_heap else None

    def _list_due(self, now: datetime) -> list[Schedule]:
        due: list[Schedule] = []
        retained: list[tuple[datetime, str, int]] = []
        with self._lock:
            while True:
                self._prune_pending_heap_locked()
                if not self._pending_heap or self._pending_heap[0][0] > now:
                    break
                entry = heapq.heappop(self._pending_heap)
                _, schedule_id, _ = entry
                retained.append(entry)
                schedule = self._schedules.get(schedule_id)
                if schedule is not None:
                    due.append(copy.deepcopy(schedule))
            for entry in retained:
                heapq.heappush(self._pending_heap, entry)
        return due


class JsonScheduleStore(InMemoryScheduleStore):
    """Stockage JSON pour conserver les réveils entre redémarrages."""

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path).resolve()
        self._writer_lock = _SingleWriterFileLock(self.path)
        self._writer_lock.acquire()
        self._persist_lock = threading.Lock()
        self._generation = 0
        self._persisted_generation = 0
        try:
            self._load()
        except Exception:
            self._writer_lock.release()
            raise

    def close(self) -> None:
        self._writer_lock.release()

    def _load(self) -> None:
        """Load schedules, tolerant of individual malformed records.

        A single bad row previously quarantined the *entire* file: the whole
        list comprehension ran inside one ``except`` that renamed
        ``schedules.json`` to ``.corrupt`` and continued with an empty store,
        silently destroying every schedule.  Now each record is decoded
        independently, the survivors are kept, and the rejected payloads are
        recorded so they can be reported instead of vanishing.
        """
        self.load_errors: list[str] = []
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            # Unreadable/unparseable file: nothing can be salvaged.
            self.load_errors.append(f"{type(exc).__name__}: {exc}")
            self._quarantine()
            return
        if not isinstance(raw, dict) or not isinstance(raw.get("schedules", []), list):
            self.load_errors.append("format JSON invalide: 'schedules' doit etre une liste")
            self._quarantine()
            return

        schedules: list[Schedule] = []
        rejected: list[Any] = []
        for data in raw.get("schedules", []):
            try:
                schedules.append(Schedule.from_dict(data))
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                # Keep every valid schedule rather than losing the whole file.
                self.load_errors.append(f"{type(exc).__name__}: {exc}")
                rejected.append(data)
        with self._lock:
            self._schedules = {item.id: item for item in schedules}
            self._rebuild_pending_index_locked()
        if rejected and not schedules:
            # Nothing survived, so preserve the original bytes for recovery.
            self._quarantine()

    def _quarantine(self) -> None:
        try:
            os.replace(self.path, self.path.with_name(self.path.name + ".corrupt"))
        except OSError:
            pass

    def rejected_schedule_count(self) -> int:
        """Number of records skipped as malformed during the last load."""
        return len(getattr(self, "load_errors", ()))

    def _snapshot_locked(self) -> dict[str, Any]:
        return {
            "schedules": [
                item.to_dict()
                for item in sorted(
                    self._schedules.values(), key=lambda item: item.run_at
                )
            ]
        }

    def _flush(
        self,
        data: Mapping[str, Any] | None = None,
        *,
        generation: int | None = None,
    ) -> None:
        if data is None or generation is None:
            with self._lock:
                if data is None:
                    data = self._snapshot_locked()
                if generation is None:
                    self._generation += 1
                    generation = self._generation

        assert generation is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._persist_lock:
            # A later save may have reached disk while this caller was waiting
            # to persist an older snapshot. Never let that stale snapshot
            # overwrite the newer durable state.
            if generation <= self._persisted_generation:
                return
            temporary = self.path.with_name(self.path.name + f".tmp-{uuid.uuid4().hex}")
            try:
                temporary.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                with temporary.open("r+b") as stream:
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                self._persisted_generation = generation
            finally:
                try:
                    temporary.unlink()
                except OSError:
                    pass

    def save(self, schedule: Schedule) -> Schedule:
        with self._lock:
            saved = super().save(schedule)
            self._generation += 1
            generation = self._generation
            data = self._snapshot_locked()
        self._flush(data, generation=generation)
        return saved


class Scheduler:
    """Publie des événements quand des schedules arrivent à échéance."""

    _MAX_DOWNSTREAM_RECOVERIES = 3

    def __init__(
        self,
        event_handler: EventHandler,
        *,
        store: ScheduleStore | None = None,
        poll_interval: float = 1.0,
        max_publish_retries: int = 3,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval doit être supérieur à zéro.")
        if max_publish_retries < 0:
            raise ValueError("max_publish_retries doit être positif ou nul.")
        self.event_handler = event_handler
        self.store = store or InMemoryScheduleStore()
        self.poll_interval = poll_interval
        self._stop_requested = threading.Event()
        self._wakeup_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.RLock()
        self._trigger_lock = threading.Lock()
        self.max_publish_retries = int(max_publish_retries)
        self._closed = False

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def snapshot(self) -> dict[str, Any]:
        """Return a compact, JSON-safe operational view without payload data."""
        try:
            schedules = self.store.list()
        except Exception:
            return {
                "component": "scheduler",
                "running": self.running,
                "healthy": False,
                "status_counts": {status.value: 0 for status in ScheduleStatus},
                "pending": 0,
                "retrying": 0,
                "failed": 0,
                "oldest_pending_age_seconds": None,
            }

        counts = {status.value: 0 for status in ScheduleStatus}
        oldest_pending: datetime | None = None
        for schedule in schedules:
            counts[schedule.status.value] = counts.get(schedule.status.value, 0) + 1
            if schedule.status in {ScheduleStatus.ACTIVE, ScheduleStatus.RETRYING}:
                candidate = min(
                    _parse_utc(schedule.created_at) or schedule.created_at,
                    _parse_utc(schedule.run_at) or schedule.run_at,
                )
                oldest_pending = (
                    candidate
                    if oldest_pending is None or candidate < oldest_pending
                    else oldest_pending
                )

        now = _now()
        return {
            "component": "scheduler",
            "running": self.running,
            "healthy": True,
            "status_counts": counts,
            "pending": counts.get(ScheduleStatus.ACTIVE.value, 0)
            + counts.get(ScheduleStatus.RETRYING.value, 0),
            "retrying": counts.get(ScheduleStatus.RETRYING.value, 0),
            "failed": counts.get(ScheduleStatus.FAILED.value, 0),
            "oldest_pending_age_seconds": (
                max(0.0, (now - oldest_pending).total_seconds())
                if oldest_pending is not None
                else None
            ),
        }

    def schedule_at(
        self,
        run_at: datetime,
        *,
        task_id: int,
        payload: Mapping[str, Any] | None = None,
        priority: int | EventPriority = EventPriority.NORMAL,
    ) -> Schedule:
        """Crée un réveil ponctuel pour une tâche déjà existante."""
        schedule = Schedule(
            run_at=run_at,
            task_id=task_id,
            payload=dict(payload or {}),
            priority=int(priority),
        )
        saved = self.store.save(schedule)
        self._wakeup_requested.set()
        return saved

    def schedule_in(
        self,
        delay: float,
        *,
        task_id: int,
        payload: Mapping[str, Any] | None = None,
        priority: int | EventPriority = EventPriority.NORMAL,
    ) -> Schedule:
        """Crée un réveil relatif à maintenant."""
        if delay < 0:
            raise ValueError("delay doit être positif ou nul.")
        return self.schedule_at(
            _now() + timedelta(seconds=delay),
            task_id=task_id,
            payload=payload,
            priority=priority,
        )

    def cancel(self, schedule_id: str) -> Schedule:
        with self._trigger_lock:
            schedule = self.store.get(schedule_id)
            if schedule is None:
                raise KeyError(f"Schedule inconnu : {schedule_id}")
            if schedule.status in {ScheduleStatus.ACTIVE, ScheduleStatus.RETRYING}:
                schedule.status = ScheduleStatus.CANCELLED
                schedule = self.store.save(schedule)
        self._wakeup_requested.set()
        return schedule

    @staticmethod
    def _idempotency_key(schedule: Schedule) -> str:
        return f"scheduler:{schedule.id}:v{schedule.version}"

    @staticmethod
    def _error_text(error: Exception) -> str:
        text = f"{type(error).__name__}: {error}"
        return text[:1000]

    def _recover_failed_deliveries(self) -> int:
        """Recover EventHandler failures without minting a new scheduler key.

        Only receipts whose idempotency key belongs to this scheduler are
        relevant, so the scan is pushed into SQL.  It previously fetched every
        failed receipt in the namespace (up to 10k rows) on every poll and
        filtered them in Python, so an idle 1 Hz scheduler paid an
        O(failed x 10000) cost forever.
        """
        durable_store = getattr(self.event_handler, "_durable_store", None)
        if durable_store is None:
            return 0
        try:
            failed = durable_store.list_events(
                status="failed", limit=10000, idempotency_key_prefix="scheduler:"
            )
        except TypeError:
            # Older store without the prefix filter: fall back to the full scan.
            try:
                failed = durable_store.list_events(status="failed", limit=10000)
            except Exception:
                return 0
        except Exception:
            return 0
        recovered = 0
        prefix = "scheduler:"
        for receipt in failed:
            stable_key = str(getattr(receipt, "idempotency_key", "") or "")
            if not stable_key.startswith(prefix) or ":v" not in stable_key:
                continue
            schedule_id, _, version_text = stable_key[len(prefix) :].rpartition(":v")
            try:
                version = int(version_text)
            except ValueError:
                continue
            schedule = self.store.get(schedule_id)
            if (
                schedule is None
                or schedule.version != version
                or schedule.status not in {ScheduleStatus.RETRYING, ScheduleStatus.FIRED}
            ):
                continue
            if schedule.delivery_recoveries >= self._MAX_DOWNSTREAM_RECOVERIES:
                schedule.delivery_failed = True
                schedule.status = ScheduleStatus.FAILED
                schedule.last_error = str(
                    getattr(receipt, "last_error", "") or "event_handler_delivery_failed"
                )[:1000]
                try:
                    self.store.save(schedule)
                except Exception:
                    pass
                continue
            if _revive_failed_event_receipt(self.event_handler, stable_key):
                schedule.delivery_recoveries += 1
                schedule.delivery_failed = False
                try:
                    self.store.save(schedule)
                except Exception:
                    # The durable receipt is already queued. Keep recovery
                    # bounded by the downstream receipt itself for this process;
                    # a restart may conservatively retry within the same cap.
                    pass
                recovered += 1
        return recovered

    def _save_failure_state(self, schedule: Schedule, error: Exception) -> None:
        schedule.last_error = self._error_text(error)
        if schedule.publish_attempts >= self.max_publish_retries + 1:
            schedule.status = ScheduleStatus.FAILED
        else:
            schedule.status = ScheduleStatus.RETRYING
        try:
            self.store.save(schedule)
        except Exception:
            # A broken store must not kill the scheduler thread. The next poll
            # can retry once persistence is healthy again.
            pass

    def trigger_due(self, *, now: datetime | None = None) -> int:
        """Déclenche immédiatement les schedules échus ; retourne leur nombre."""
        self._recover_failed_deliveries()
        if not self._trigger_lock.acquire(blocking=False):
            return 0
        current_time = _as_utc(now or _now())
        triggered = 0
        try:
            try:
                list_due = getattr(self.store, "_list_due", None)
                schedules = (
                    list_due(current_time) if callable(list_due) else self.store.list()
                )
            except Exception:
                return 0
            for schedule in schedules:
                if schedule.status not in {
                    ScheduleStatus.ACTIVE,
                    ScheduleStatus.RETRYING,
                }:
                    continue
                if schedule.run_at > current_time:
                    continue

                # RETRYING with no last_error means the previous process persisted
                # the intent to publish but may have crashed before or after the
                # volatile handoff. Re-publishing with the same idempotency key is
                # the safest at-least-once recovery available without a durable
                # event queue.
                uncertain_attempt = (
                    schedule.status == ScheduleStatus.RETRYING
                    and schedule.publish_attempts > 0
                    and schedule.last_error is None
                )
                if (
                    schedule.last_error is not None
                    and schedule.publish_attempts >= self.max_publish_retries + 1
                ):
                    schedule.status = ScheduleStatus.FAILED
                    try:
                        self.store.save(schedule)
                    except Exception:
                        pass
                    continue

                if not uncertain_attempt:
                    schedule.publish_attempts += 1
                schedule.status = ScheduleStatus.RETRYING
                schedule.last_attempt_at = current_time
                schedule.last_error = None
                try:
                    # Persist the retryable intent before publishing. A crash from
                    # here onward leaves a schedule that will be retried on restart.
                    self.store.save(schedule)
                except Exception:
                    continue

                try:
                    self.event_handler.publish(
                        EventType.SCHEDULE,
                        schedule.payload,
                        priority=schedule.priority,
                        source="scheduler",
                        metadata={
                            "schedule_id": schedule.id,
                            "schedule_version": schedule.version,
                            "task_id": schedule.task_id,
                        },
                        idempotency_key=self._idempotency_key(schedule),
                    )
                except Exception as exc:
                    self._save_failure_state(schedule, exc)
                    continue
                schedule.status = ScheduleStatus.FIRED
                schedule.fired_at = current_time
                schedule.last_error = None
                try:
                    self.store.save(schedule)
                except Exception:
                    # The already-persisted RETRYING record survives this failure.
                    # A same-process retry is deduplicated by EventHandler; after a
                    # restart the stable key is preserved for downstream dedupe.
                    continue
                triggered += 1
            return triggered
        finally:
            self._trigger_lock.release()

    def start(self) -> Scheduler:
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("Scheduler fermé.")
            if self.running:
                return self
            self._stop_requested.clear()
            self._wakeup_requested.clear()
            self._thread = threading.Thread(
                target=self._run, name="agent-scheduler", daemon=True
            )
            self._thread.start()
        return self

    def stop(self, *, wait: bool = True) -> None:
        with self._lifecycle_lock:
            self._stop_requested.set()
            self._wakeup_requested.set()
            thread = self._thread
        if thread is not None and wait and thread is not threading.current_thread():
            thread.join()
        with self._lifecycle_lock:
            # With wait=False the old thread may still be inside trigger_due().
            # Keep the reference until it really exits so start() cannot clear
            # the shared stop Event and launch an overlapping scheduler loop.
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None

    def close(self) -> None:
        if self._closed:
            return
        if self._thread is not None:
            self.stop(wait=True)
        close = getattr(self.store, "close", None)
        if callable(close):
            close()
        self._closed = True

    def _poll_once(self) -> float:
        """Process one scheduler poll and return the maximum next wait."""
        self._recover_failed_deliveries()
        next_pending_at = getattr(self.store, "_next_pending_at", None)
        if not callable(next_pending_at):
            self.trigger_due()
            return self.poll_interval

        try:
            next_due = next_pending_at()
        except Exception:
            return self.poll_interval
        current_time = _now()
        if next_due is None:
            return self.poll_interval
        if next_due > current_time:
            return min(self.poll_interval, (next_due - current_time).total_seconds())

        self.trigger_due(now=current_time)
        try:
            next_due = next_pending_at()
        except Exception:
            return self.poll_interval
        if next_due is None:
            return self.poll_interval
        remaining = (next_due - _now()).total_seconds()
        if remaining <= 0:
            return self.poll_interval
        return min(self.poll_interval, remaining)

    def _run(self) -> None:
        while not self._stop_requested.is_set():
            self._wakeup_requested.clear()
            try:
                wait_for = self._poll_once()
            except Exception:
                # Scheduler reliability must not depend on one malformed row,
                # transient store failure or publisher exception.
                wait_for = self.poll_interval
            if self._stop_requested.is_set():
                break
            self._wakeup_requested.wait(wait_for)

    def __enter__(self) -> Scheduler:
        return self.start()

    def __exit__(self, *_: Any) -> None:
        self.stop()


__all__ = [
    "InMemoryScheduleStore",
    "JsonScheduleStore",
    "Schedule",
    "ScheduleStatus",
    "ScheduleStore",
    "Scheduler",
]
