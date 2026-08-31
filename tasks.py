"""Modèle et stockage des tâches durables de l'agent."""

from __future__ import annotations

import copy
import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any, Protocol


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskPriority(IntEnum):
    LOW = 10
    NORMAL = 20
    HIGH = 30
    CRITICAL = 40


def _bounded_value(value: Any, *, max_chars: int = 4000) -> Any:
    """Evite de persister des snapshots récursifs ou disproportionnés."""
    if isinstance(value, str):
        if len(value) <= max_chars:
            return value
        return {"truncated": True, "preview": value[:max_chars]}
    try:
        encoded = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)[:max_chars]
    if len(encoded) <= max_chars:
        return value
    return {"truncated": True, "preview": encoded[:max_chars]}


class RunStatus(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ActionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class PlanStepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _datetime_to_string(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _string_to_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


@dataclass
class WaitCondition:
    """Condition déclarée par l'agent pour reprendre une tâche plus tard."""

    event_type: str | None = None
    source: str | None = None
    payload_equals: dict[str, Any] = field(default_factory=dict)
    metadata_equals: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    id: str = field(default_factory=lambda: f"wait_{uuid.uuid4().hex[:12]}")
    created_at: datetime = field(default_factory=_now)

    def matches(self, event: Any) -> bool:
        """Indique si un événement satisfait cette condition simple."""
        expected_type = (
            self.event_type.value
            if isinstance(self.event_type, Enum)
            else self.event_type
        )
        if expected_type is not None and event.type != expected_type:
            return False
        if self.source is not None and event.source != self.source:
            return False
        if any(event.payload.get(key) != value for key, value in self.payload_equals.items()):
            return False
        if any(event.metadata.get(key) != value for key, value in self.metadata_equals.items()):
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "event_type": self.event_type,
            "source": self.source,
            "payload_equals": self.payload_equals,
            "metadata_equals": self.metadata_equals,
            "description": self.description,
            "created_at": _datetime_to_string(self.created_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WaitCondition:
        return cls(
            id=str(data.get("id") or f"wait_{uuid.uuid4().hex[:12]}"),
            event_type=data.get("event_type"),
            source=data.get("source"),
            payload_equals=dict(data.get("payload_equals") or {}),
            metadata_equals=dict(data.get("metadata_equals") or {}),
            description=str(data.get("description", "")),
            created_at=_string_to_datetime(data.get("created_at")) or _now(),
        )


@dataclass
class TaskRun:
    id: str = field(default_factory=lambda: f"run_{uuid.uuid4().hex[:12]}")
    event_id: str | None = None
    status: RunStatus = RunStatus.RUNNING
    phase: str = "reflection"
    started_at: datetime = field(default_factory=_now)
    finished_at: datetime | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "event_id": self.event_id,
            "status": self.status.value,
            "phase": self.phase,
            "started_at": _datetime_to_string(self.started_at),
            "finished_at": _datetime_to_string(self.finished_at),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskRun:
        return cls(
            id=str(data.get("id") or f"run_{uuid.uuid4().hex[:12]}"),
            event_id=data.get("event_id"),
            status=RunStatus(data.get("status", RunStatus.RUNNING.value)),
            phase=str(data.get("phase", "reflection")),
            started_at=_string_to_datetime(data.get("started_at")) or _now(),
            finished_at=_string_to_datetime(data.get("finished_at")),
            error=data.get("error"),
        )


@dataclass
class PlanStep:
    """Étape mutable d'un plan construit par l'agent."""

    id: str = field(default_factory=lambda: f"step_{uuid.uuid4().hex[:12]}")
    position: int = 1
    title: str = ""
    description: str = ""
    status: PlanStepStatus = PlanStepStatus.PENDING
    result: Any = None
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("Une étape de plan doit avoir un titre.")
        self.position = int(self.position)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "position": self.position,
            "title": self.title,
            "description": self.description,
            "status": self.status.value,
            "result": self.result,
            "created_at": _datetime_to_string(self.created_at),
            "updated_at": _datetime_to_string(self.updated_at),
        }

    @classmethod
    def from_dict(cls, data: Any, position: int = 1) -> PlanStep:
        if isinstance(data, str):
            return cls(position=position, title=data)
        if not isinstance(data, dict):
            raise ValueError("Une étape de plan doit être une chaîne ou un objet.")
        return cls(
            id=str(data.get("id") or f"step_{uuid.uuid4().hex[:12]}"),
            position=int(data.get("position", position)),
            title=str(data.get("title", data.get("name", ""))),
            description=str(data.get("description", "")),
            status=PlanStepStatus(data.get("status", PlanStepStatus.PENDING.value)),
            result=_bounded_value(data.get("result")),
            created_at=_string_to_datetime(data.get("created_at")) or _now(),
            updated_at=_string_to_datetime(data.get("updated_at")) or _now(),
        )


@dataclass
class TaskAction:
    id: str = field(default_factory=lambda: f"action_{uuid.uuid4().hex[:12]}")
    name: str = ""
    description: str = ""
    action_key: str | None = None
    status: ActionStatus = ActionStatus.PENDING
    result: Any = None
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "action_key": self.action_key,
            "status": self.status.value,
            "result": _bounded_value(self.result),
            "created_at": _datetime_to_string(self.created_at),
            "updated_at": _datetime_to_string(self.updated_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskAction:
        return cls(
            id=str(data.get("id") or f"action_{uuid.uuid4().hex[:12]}"),
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            action_key=data.get("action_key"),
            status=ActionStatus(data.get("status", ActionStatus.PENDING.value)),
            result=_bounded_value(data.get("result")),
            created_at=_string_to_datetime(data.get("created_at")) or _now(),
            updated_at=_string_to_datetime(data.get("updated_at")) or _now(),
        )


@dataclass
class Task:
    """Objectif durable poursuivi par l'agent."""

    id: int
    objective: str
    status: TaskStatus = TaskStatus.PENDING
    priority: int = int(TaskPriority.NORMAL)
    current_state: dict[str, Any] = field(default_factory=dict)
    plan: list[PlanStep] = field(default_factory=list)
    waiting_for: list[WaitCondition] = field(default_factory=list)
    runs: list[TaskRun] = field(default_factory=list)
    actions: list[TaskAction] = field(default_factory=list)
    artifacts: list[Any] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    def __post_init__(self) -> None:
        if not self.objective.strip():
            raise ValueError("Une tâche doit avoir un objectif.")
        self.priority = int(self.priority)
        if self.priority < 0:
            raise ValueError("La priorité d'une tâche doit être positive ou nulle.")

    def add_history(self, event: str, **data: Any) -> None:
        self.history.append({"at": _datetime_to_string(_now()), "event": event, **data})
        self.updated_at = _now()

    def wait_for(
        self,
        *,
        event_type: str | None = None,
        source: str | None = None,
        payload_equals: dict[str, Any] | None = None,
        metadata_equals: dict[str, Any] | None = None,
        description: str = "",
    ) -> WaitCondition:
        """Déclare ce que l'agent attend avant de poursuivre la tâche."""
        condition = WaitCondition(
            event_type=event_type,
            source=source,
            payload_equals=dict(payload_equals or {}),
            metadata_equals=dict(metadata_equals or {}),
            description=description,
        )
        self.waiting_for.append(condition)
        self.status = TaskStatus.WAITING
        self.add_history(
            "task_waiting",
            condition_id=condition.id,
            description=description,
        )
        return condition

    def resume_from_wait(self, event_id: str | None = None) -> None:
        """Repasse la tâche en cours lorsqu'une condition est satisfaite."""
        self.waiting_for.clear()
        self.status = TaskStatus.RUNNING
        self.add_history("task_resumed", event_id=event_id)

    @property
    def current_plan_step(self) -> PlanStep | None:
        """Étape en cours, ou première étape encore à traiter."""
        ordered = sorted(self.plan, key=lambda step: step.position)
        return next(
            (step for step in ordered if step.status == PlanStepStatus.IN_PROGRESS),
            next(
                (step for step in ordered if step.status == PlanStepStatus.PENDING),
                None,
            ),
        )

    def set_plan(self, steps: list[Any], *, reason: str | None = None) -> None:
        """Remplace le plan ; l'agent peut donc le réviser à tout moment."""
        normalized: list[PlanStep] = []
        for position, step in enumerate(steps, start=1):
            if isinstance(step, PlanStep):
                item = step
                item.position = position
            else:
                item = PlanStep.from_dict(step, position=position)
            normalized.append(item)
        self.plan = normalized
        self.add_history("plan_updated", reason=reason, step_count=len(normalized))

    def add_plan_step(
        self,
        title: str,
        *,
        description: str = "",
        position: int | None = None,
    ) -> PlanStep:
        """Ajoute une étape sans imposer de plan rigide."""
        next_position = position or (max((step.position for step in self.plan), default=0) + 1)
        step = PlanStep(position=next_position, title=title, description=description)
        self.plan.append(step)
        self.plan.sort(key=lambda item: item.position)
        self.add_history("plan_step_added", step_id=step.id, title=title)
        return step

    def update_plan_step(self, step_id: str, **changes: Any) -> PlanStep:
        """Modifie une étape existante (statut, contenu ou résultat)."""
        for step in self.plan:
            if step.id == step_id:
                for key in ("title", "description", "result"):
                    if key in changes:
                        setattr(step, key, changes[key])
                if "status" in changes:
                    step.status = PlanStepStatus(changes["status"])
                step.updated_at = _now()
                self.add_history("plan_step_updated", step_id=step_id, changes=changes)
                return step
        raise KeyError(f"Étape de plan inconnue : {step_id}")

    def complete_plan_step(self, step_id: str, *, result: Any = None) -> PlanStep:
        """Marque une étape comme terminée sans modifier les suivantes."""
        return self.update_plan_step(
            step_id,
            status=PlanStepStatus.COMPLETED,
            result=result,
        )

    def start_run(self, event_id: str | None = None) -> TaskRun:
        self.status = TaskStatus.RUNNING
        run = TaskRun(event_id=event_id)
        self.runs.append(run)
        self.add_history("run_started", run_id=run.id, event_id=event_id)
        return run

    def pause(
        self,
        *,
        run_id: str | None = None,
        reason: str = "",
        interrupted_by: str | None = None,
    ) -> None:
        """Met la tâche et son run courant en pause par préemption."""
        self.status = TaskStatus.PAUSED
        if run_id is not None:
            for run in self.runs:
                if run.id == run_id:
                    run.status = RunStatus.PAUSED
                    break
        self.add_history(
            "task_paused",
            run_id=run_id,
            reason=reason,
            interrupted_by=interrupted_by,
        )

    def resume(self, *, run_id: str | None = None, event_id: str | None = None) -> None:
        """Reprend un run précédemment préempté."""
        self.status = TaskStatus.RUNNING
        if run_id is not None:
            for run in self.runs:
                if run.id == run_id:
                    run.status = RunStatus.RUNNING
                    break
        self.add_history("task_resumed", run_id=run_id, event_id=event_id)

    def finish_run(
        self,
        run_id: str,
        *,
        status: RunStatus = RunStatus.COMPLETED,
        error: str | None = None,
    ) -> None:
        for run in self.runs:
            if run.id == run_id:
                run.status = status
                run.error = error
                run.finished_at = _now()
                self.add_history("run_finished", run_id=run_id, status=status.value)
                return
        raise KeyError(f"Run inconnu : {run_id}")

    def add_action(
        self,
        name: str,
        *,
        description: str = "",
        result: Any = None,
        action_key: str | None = None,
    ) -> TaskAction:
        action = TaskAction(
            name=name,
            description=description,
            action_key=action_key,
            result=result,
        )
        self.actions.append(action)
        self.add_history("action_added", action_id=action.id, name=name)
        return action

    def update_action(self, action_id: str, **changes: Any) -> TaskAction:
        """Met à jour le statut ou le résultat d'une action exécutée."""
        for action in self.actions:
            if action.id == action_id:
                if "status" in changes:
                    action.status = ActionStatus(changes["status"])
                if "result" in changes:
                    action.result = changes["result"]
                action.updated_at = _now()
                self.add_history("action_updated", action_id=action_id, changes=changes)
                return action
        raise KeyError(f"Action inconnue : {action_id}")

    def add_artifact(self, artifact: Any) -> None:
        self.artifacts.append(artifact)
        self.add_history("artifact_added")

    def mark_completed(self) -> None:
        self.status = TaskStatus.COMPLETED
        self.add_history("task_completed")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "objective": self.objective,
            "status": self.status.value,
            "priority": self.priority,
            "current_state": self.current_state,
            "plan": [step.to_dict() for step in self.plan],
            "waiting_for": [condition.to_dict() for condition in self.waiting_for],
            "runs": [run.to_dict() for run in self.runs],
            "actions": [action.to_dict() for action in self.actions],
            "artifacts": self.artifacts,
            "history": self.history,
            "created_at": _datetime_to_string(self.created_at),
            "updated_at": _datetime_to_string(self.updated_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        return cls(
            id=int(data["id"]),
            objective=str(data["objective"]),
            status=TaskStatus(data.get("status", TaskStatus.PENDING.value)),
            priority=int(data.get("priority", TaskPriority.NORMAL)),
            current_state=dict(data.get("current_state") or {}),
            plan=[
                PlanStep.from_dict(item, position=position)
                for position, item in enumerate(data.get("plan", []), start=1)
            ],
            waiting_for=[
                WaitCondition.from_dict(item) for item in data.get("waiting_for", [])
            ],
            runs=[TaskRun.from_dict(item) for item in data.get("runs", [])],
            actions=[TaskAction.from_dict(item) for item in data.get("actions", [])],
            artifacts=list(data.get("artifacts") or []),
            history=list(data.get("history") or []),
            created_at=_string_to_datetime(data.get("created_at")) or _now(),
            updated_at=_string_to_datetime(data.get("updated_at")) or _now(),
        )


class TaskStore(Protocol):
    """Contrat de persistance utilisé par le runtime."""

    def create(self, objective: str, *, priority: int = int(TaskPriority.NORMAL)) -> Task:
        ...

    def get(self, task_id: int) -> Task | None:
        ...

    def save(self, task: Task) -> Task:
        ...

    def list(self, *, status: TaskStatus | None = None) -> list[Task]:
        ...

    def find_waiting_task(self, event: Any) -> Task | None:
        ...


class InMemoryTaskStore:
    """Stockage thread-safe par défaut, remplaçable sans modifier le runtime."""

    def __init__(self) -> None:
        self._tasks: dict[int, Task] = {}
        self._next_id = 1
        self._lock = threading.RLock()

    def create(self, objective: str, *, priority: int = int(TaskPriority.NORMAL)) -> Task:
        with self._lock:
            task = Task(id=self._next_id, objective=objective, priority=priority)
            self._next_id += 1
            task.add_history("task_created")
            self._tasks[task.id] = copy.deepcopy(task)
            return copy.deepcopy(task)

    def get(self, task_id: int) -> Task | None:
        with self._lock:
            task = self._tasks.get(int(task_id))
            return copy.deepcopy(task) if task else None

    def save(self, task: Task) -> Task:
        with self._lock:
            self._tasks[int(task.id)] = copy.deepcopy(task)
            self._next_id = max(self._next_id, int(task.id) + 1)
            return copy.deepcopy(task)

    def list(self, *, status: TaskStatus | None = None) -> list[Task]:
        with self._lock:
            tasks = list(self._tasks.values())
            if status is not None:
                tasks = [task for task in tasks if task.status == status]
            return copy.deepcopy(sorted(tasks, key=lambda task: task.id))

    def find_waiting_task(self, event: Any) -> Task | None:
        with self._lock:
            candidates = [
                task
                for task in self._tasks.values()
                if task.status == TaskStatus.WAITING
                and any(condition.matches(event) for condition in task.waiting_for)
            ]
            if not candidates:
                return None
            # Une tâche critique passe avant une tâche ordinaire ; à priorité
            # égale, l'identifiant le plus ancien est servi en premier.
            task = sorted(candidates, key=lambda item: (-item.priority, item.id))[0]
            return copy.deepcopy(task)


class JsonTaskStore(InMemoryTaskStore):
    """Stockage JSON simple pour conserver les tâches entre redémarrages."""

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        tasks = [Task.from_dict(item) for item in data.get("tasks", [])]
        with self._lock:
            self._tasks = {task.id: task for task in tasks}
            self._next_id = max(self._tasks, default=0) + 1

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"tasks": [task.to_dict() for task in self.list()]}
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    def create(self, objective: str, *, priority: int = int(TaskPriority.NORMAL)) -> Task:
        task = super().create(objective, priority=priority)
        self._flush()
        return task

    def save(self, task: Task) -> Task:
        saved = super().save(task)
        self._flush()
        return saved


__all__ = [
    "ActionStatus",
    "InMemoryTaskStore",
    "JsonTaskStore",
    "PlanStep",
    "PlanStepStatus",
    "WaitCondition",
    "RunStatus",
    "Task",
    "TaskAction",
    "TaskPriority",
    "TaskRun",
    "TaskStatus",
    "TaskStore",
]
