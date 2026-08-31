"""Scheduler persistant pour réveiller l'agent à une date donnée.

Le scheduler ne crée ni ne modifie de tâche. Il publie simplement un événement
``schedule`` contenant la référence de la tâche déjà associée au réveil.
"""

from __future__ import annotations

import copy
import json
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


class ScheduleStatus(str, Enum):
    ACTIVE = "active"
    FIRED = "fired"
    CANCELLED = "cancelled"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("run_at doit contenir un fuseau horaire.")
    return value.astimezone(timezone.utc)


def _datetime_to_string(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


@dataclass
class Schedule:
    """Réveil planifié et lié à une tâche existante."""

    run_at: datetime
    task_id: int
    payload: dict[str, Any] = field(default_factory=dict)
    priority: int = int(EventPriority.NORMAL)
    id: str = field(default_factory=lambda: f"schedule_{uuid.uuid4().hex[:12]}")
    status: ScheduleStatus = ScheduleStatus.ACTIVE
    created_at: datetime = field(default_factory=_now)
    fired_at: datetime | None = None

    def __post_init__(self) -> None:
        self.run_at = _as_utc(self.run_at)
        self.task_id = int(self.task_id)
        self.priority = int(self.priority)
        if self.priority < 0:
            raise ValueError("La priorité du schedule doit être positive ou nulle.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_at": _datetime_to_string(self.run_at),
            "task_id": self.task_id,
            "payload": self.payload,
            "priority": self.priority,
            "status": self.status.value,
            "created_at": _datetime_to_string(self.created_at),
            "fired_at": _datetime_to_string(self.fired_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Schedule:
        run_at = datetime.fromisoformat(data["run_at"])
        return cls(
            id=str(data.get("id") or f"schedule_{uuid.uuid4().hex[:12]}"),
            run_at=run_at,
            task_id=int(data["task_id"]),
            payload=dict(data.get("payload") or {}),
            priority=int(data.get("priority", EventPriority.NORMAL)),
            status=ScheduleStatus(data.get("status", ScheduleStatus.ACTIVE.value)),
            created_at=datetime.fromisoformat(data["created_at"])
            if data.get("created_at")
            else _now(),
            fired_at=datetime.fromisoformat(data["fired_at"])
            if data.get("fired_at")
            else None,
        )


class ScheduleStore(Protocol):
    def save(self, schedule: Schedule) -> Schedule:
        ...

    def get(self, schedule_id: str) -> Schedule | None:
        ...

    def list(self, *, status: ScheduleStatus | None = None) -> list[Schedule]:
        ...


class InMemoryScheduleStore:
    """Stockage thread-safe des schedules actifs et déclenchés."""

    def __init__(self) -> None:
        self._schedules: dict[str, Schedule] = {}
        self._lock = threading.RLock()

    def save(self, schedule: Schedule) -> Schedule:
        with self._lock:
            self._schedules[schedule.id] = copy.deepcopy(schedule)
            return copy.deepcopy(schedule)

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


class JsonScheduleStore(InMemoryScheduleStore):
    """Stockage JSON pour conserver les réveils entre redémarrages."""

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        with self._lock:
            self._schedules = {
                item.id: item
                for item in (Schedule.from_dict(data) for data in raw.get("schedules", []))
            }

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"schedules": [item.to_dict() for item in self.list()]}
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def save(self, schedule: Schedule) -> Schedule:
        saved = super().save(schedule)
        self._flush()
        return saved


class Scheduler:
    """Publie des événements quand des schedules arrivent à échéance."""

    def __init__(
        self,
        event_handler: EventHandler,
        *,
        store: ScheduleStore | None = None,
        poll_interval: float = 1.0,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval doit être supérieur à zéro.")
        self.event_handler = event_handler
        self.store = store or InMemoryScheduleStore()
        self.poll_interval = poll_interval
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

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
        return self.store.save(schedule)

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
        schedule = self.store.get(schedule_id)
        if schedule is None:
            raise KeyError(f"Schedule inconnu : {schedule_id}")
        if schedule.status == ScheduleStatus.ACTIVE:
            schedule.status = ScheduleStatus.CANCELLED
            self.store.save(schedule)
        return schedule

    def trigger_due(self, *, now: datetime | None = None) -> int:
        """Déclenche immédiatement les schedules échus ; retourne leur nombre."""
        current_time = _as_utc(now or _now())
        triggered = 0
        for schedule in self.store.list(status=ScheduleStatus.ACTIVE):
            if schedule.run_at > current_time:
                continue
            self.event_handler.publish(
                EventType.SCHEDULE,
                schedule.payload,
                priority=schedule.priority,
                source="scheduler",
                metadata={
                    "schedule_id": schedule.id,
                    "task_id": schedule.task_id,
                },
            )
            schedule.status = ScheduleStatus.FIRED
            schedule.fired_at = current_time
            self.store.save(schedule)
            triggered += 1
        return triggered

    def start(self) -> Scheduler:
        if self.running:
            return self
        self._stop_requested.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="agent-scheduler",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self, *, wait: bool = True) -> None:
        self._stop_requested.set()
        thread = self._thread
        if thread is not None and wait:
            thread.join()
        self._thread = None

    def _run(self) -> None:
        while not self._stop_requested.is_set():
            self.trigger_due()
            self._stop_requested.wait(self.poll_interval)

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
