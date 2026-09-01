"""Sous-agents persistants et indépendants du runtime principal d'Orion."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from itertools import count
from pathlib import Path
from queue import Empty, PriorityQueue
from typing import Any, Mapping

from event_handler import EventHandler, EventPriority
from openrouter_client import OpenRouterClient


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


class SubAgentStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class SubAgentJobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class SubAgent:
    id: str
    name: str
    description: str
    model: str
    system_prompt: str
    allowed_tools: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    max_turns: int = 8
    status: SubAgentStatus = SubAgentStatus.ACTIVE
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SubAgent":
        return cls(
            id=str(value["id"]),
            name=str(value["name"]),
            description=str(value.get("description", "")),
            model=str(value["model"]),
            system_prompt=str(value.get("system_prompt", "")),
            allowed_tools=[str(item) for item in value.get("allowed_tools", [])],
            capabilities=[str(item) for item in value.get("capabilities", [])],
            max_turns=int(value.get("max_turns", 8)),
            status=SubAgentStatus(value.get("status", SubAgentStatus.ACTIVE.value)),
            created_at=str(value.get("created_at", _now())),
            updated_at=str(value.get("updated_at", _now())),
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        return value


@dataclass
class SubAgentJob:
    id: str
    agent_id: str
    objective: str
    context: str = ""
    priority: int = int(EventPriority.NORMAL)
    status: SubAgentJobStatus = SubAgentJobStatus.QUEUED
    parent_task_id: int | None = None
    parent_event_id: str | None = None
    route_metadata: dict[str, Any] = field(default_factory=dict)
    result: str | None = None
    error: str | None = None
    progress: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    cancel_requested: bool = False
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    completed_at: str | None = None
    updated_at: str = field(default_factory=_now)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SubAgentJob":
        return cls(
            id=str(value["id"]),
            agent_id=str(value["agent_id"]),
            objective=str(value["objective"]),
            context=str(value.get("context", "")),
            priority=int(value.get("priority", int(EventPriority.NORMAL))),
            status=SubAgentJobStatus(value.get("status", SubAgentJobStatus.QUEUED.value)),
            parent_task_id=value.get("parent_task_id"),
            parent_event_id=value.get("parent_event_id"),
            route_metadata=dict(value.get("route_metadata", {})),
            result=value.get("result"),
            error=value.get("error"),
            progress=[str(item) for item in value.get("progress", [])],
            tool_calls=[dict(item) for item in value.get("tool_calls", [])],
            cancel_requested=bool(value.get("cancel_requested", False)),
            created_at=str(value.get("created_at", _now())),
            started_at=value.get("started_at"),
            completed_at=value.get("completed_at"),
            updated_at=str(value.get("updated_at", _now())),
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        return value


class SubAgentManager:
    """Registre, persistance et pool de workers pour les sous-agents."""

    TERMINAL_JOB_STATUSES = {
        SubAgentJobStatus.COMPLETED,
        SubAgentJobStatus.FAILED,
        SubAgentJobStatus.CANCELLED,
    }

    def __init__(
        self,
        llm_client: OpenRouterClient,
        event_handler: EventHandler,
        *,
        state_path: str | Path = "data/subagents.json",
        workers: int = 3,
        default_model: str | None = None,
        default_tools: list[str] | None = None,
        default_max_turns: int = 8,
        max_context_chars: int = 16000,
        max_result_chars: int = 24000,
        max_tool_output_chars: int = 12000,
        history_limit: int = 200,
        emit_progress_events: bool = True,
    ) -> None:
        if workers < 1 or default_max_turns < 1:
            raise ValueError("workers et default_max_turns doivent être positifs.")
        self.llm_client = llm_client
        self.event_handler = event_handler
        self.state_path = Path(state_path).resolve()
        self.workers = int(workers)
        self.default_model = default_model or llm_client.model
        self.default_tools = list(default_tools or ["web_search", "web_fetch", "fetch_url", "fetch_json_api"])
        self.default_max_turns = int(default_max_turns)
        self.max_context_chars = int(max_context_chars)
        self.max_result_chars = int(max_result_chars)
        self.max_tool_output_chars = int(max_tool_output_chars)
        self.history_limit = max(10, int(history_limit))
        self.emit_progress_events = bool(emit_progress_events)

        self._lock = threading.RLock()
        self._agents: dict[str, SubAgent] = {}
        self._jobs: dict[str, SubAgentJob] = {}
        self._queue: PriorityQueue[tuple[int, int, str]] = PriorityQueue()
        self._sequence = count()
        self._threads: list[threading.Thread] = []
        self._stop_requested = threading.Event()
        self._running = False
        self._load()

    @property
    def running(self) -> bool:
        return self._running

    def _load(self) -> None:
        if not self.state_path.is_file():
            return
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"État des sous-agents illisible : {self.state_path}") from exc
        with self._lock:
            self._agents = {
                item.id: item
                for item in (SubAgent.from_dict(raw) for raw in value.get("agents", []))
            }
            self._jobs = {
                item.id: item
                for item in (SubAgentJob.from_dict(raw) for raw in value.get("jobs", []))
            }
            for job in self._jobs.values():
                if job.status == SubAgentJobStatus.RUNNING:
                    job.status = SubAgentJobStatus.QUEUED
                    job.started_at = None
                    job.error = "Job repris après redémarrage d'Orion."
                if job.status == SubAgentJobStatus.QUEUED:
                    self._enqueue_locked(job)

    def _save_locked(self) -> None:
        terminal = sorted(
            (job for job in self._jobs.values() if job.status in self.TERMINAL_JOB_STATUSES),
            key=lambda item: item.updated_at,
            reverse=True,
        )
        retained_terminal_ids = {job.id for job in terminal[: self.history_limit]}
        self._jobs = {
            job_id: job
            for job_id, job in self._jobs.items()
            if job.status not in self.TERMINAL_JOB_STATUSES or job_id in retained_terminal_ids
        }
        payload = {
            "version": 1,
            "agents": [item.to_dict() for item in self._agents.values()],
            "jobs": [item.to_dict() for item in self._jobs.values()],
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(f".{self.state_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, self.state_path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _enqueue_locked(self, job: SubAgentJob) -> None:
        self._queue.put((-job.priority, next(self._sequence), job.id))

    def start(self) -> "SubAgentManager":
        with self._lock:
            if self._running:
                return self
            self._stop_requested.clear()
            self._threads = []
            for index in range(self.workers):
                thread = threading.Thread(
                    target=self._worker,
                    name=f"orion-subagent-{index + 1}",
                    daemon=True,
                )
                thread.start()
                self._threads.append(thread)
            self._running = True
        return self

    def stop(self, *, wait: bool = True) -> None:
        self._stop_requested.set()
        threads = list(self._threads)
        if wait:
            for thread in threads:
                thread.join(timeout=2.0)
        self._threads = []
        self._running = False

    def create_agent(
        self,
        name: str,
        description: str,
        *,
        model: str | None = None,
        system_prompt: str | None = None,
        allowed_tools: list[str] | None = None,
        capabilities: list[str] | None = None,
        max_turns: int | None = None,
    ) -> SubAgent:
        name = name.strip()
        if not name:
            raise ValueError("Le sous-agent doit avoir un nom.")
        with self._lock:
            if any(agent.name.lower() == name.lower() for agent in self._agents.values()):
                raise ValueError(f"Un sous-agent nommé {name} existe déjà.")
            agent = SubAgent(
                id=uuid.uuid4().hex[:12],
                name=name,
                description=description.strip(),
                model=(model or self.default_model).strip(),
                system_prompt=(system_prompt or self._default_system_prompt(name, description)).strip(),
                allowed_tools=list(self.default_tools if allowed_tools is None else allowed_tools),
                capabilities=list(capabilities or []),
                max_turns=max(1, min(int(max_turns or self.default_max_turns), 30)),
            )
            self._agents[agent.id] = agent
            self._save_locked()
            return SubAgent.from_dict(agent.to_dict())

    @staticmethod
    def _default_system_prompt(name: str, description: str) -> str:
        return (
            f"Tu es {name}, un sous-agent spécialisé d'Orion. {description.strip()}\n"
            "Accomplis uniquement l'objectif délégué avec les outils autorisés. "
            "Travaille de façon autonome, vérifie tes observations et termine par un résultat "
            "directement exploitable par Orion. N'invente pas de capacités indisponibles."
        )

    def update_agent(self, agent_id: str, **changes: Any) -> SubAgent:
        allowed = {"name", "description", "model", "system_prompt", "allowed_tools", "capabilities", "max_turns", "status"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"Champs inconnus : {', '.join(sorted(unknown))}")
        with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None:
                raise KeyError(f"Sous-agent inconnu : {agent_id}")
            for key, value in changes.items():
                if value is None:
                    continue
                if key in {"allowed_tools", "capabilities"}:
                    value = [str(item) for item in value]
                elif key == "max_turns":
                    value = max(1, min(int(value), 30))
                elif key == "status":
                    value = SubAgentStatus(value)
                else:
                    value = str(value).strip()
                setattr(agent, key, value)
            agent.updated_at = _now()
            self._save_locked()
            return SubAgent.from_dict(agent.to_dict())

    def delete_agent(self, agent_id: str, *, cancel_jobs: bool = True) -> dict[str, Any]:
        with self._lock:
            agent = self._agents.pop(agent_id, None)
            if agent is None:
                raise KeyError(f"Sous-agent inconnu : {agent_id}")
            affected = 0
            if cancel_jobs:
                for job in self._jobs.values():
                    if job.agent_id != agent_id or job.status in self.TERMINAL_JOB_STATUSES:
                        continue
                    affected += 1
                    job.cancel_requested = True
                    if job.status == SubAgentJobStatus.QUEUED:
                        job.status = SubAgentJobStatus.CANCELLED
                        job.completed_at = _now()
                    job.updated_at = _now()
            self._save_locked()
            return {"deleted": True, "agent_id": agent_id, "jobs_cancelled_or_stopping": affected}

    def get_agent(self, agent_id: str) -> SubAgent | None:
        with self._lock:
            agent = self._agents.get(agent_id)
            return SubAgent.from_dict(agent.to_dict()) if agent else None

    def list_agents(self) -> list[SubAgent]:
        with self._lock:
            return [SubAgent.from_dict(item.to_dict()) for item in self._agents.values()]

    def _select_agent_locked(self, objective: str) -> SubAgent:
        candidates = [item for item in self._agents.values() if item.status == SubAgentStatus.ACTIVE]
        if not candidates:
            raise RuntimeError("Aucun sous-agent actif. Crée-en un avant de déléguer.")
        words = {word.lower().strip(".,:;!?()[]") for word in objective.split() if len(word) > 3}
        def score(agent: SubAgent) -> tuple[int, str]:
            haystack = " ".join([agent.name, agent.description, *agent.capabilities]).lower()
            return sum(1 for word in words if word in haystack), agent.updated_at
        return max(candidates, key=score)

    def submit(
        self,
        objective: str,
        *,
        agent_id: str | None = None,
        context: str = "",
        priority: int = int(EventPriority.NORMAL),
        parent_task_id: int | None = None,
        parent_event_id: str | None = None,
        route_metadata: Mapping[str, Any] | None = None,
    ) -> SubAgentJob:
        if not objective.strip():
            raise ValueError("L'objectif délégué ne peut pas être vide.")
        with self._lock:
            agent = self._agents.get(agent_id) if agent_id else self._select_agent_locked(objective)
            if agent is None:
                raise KeyError(f"Sous-agent inconnu : {agent_id}")
            if agent.status != SubAgentStatus.ACTIVE:
                raise RuntimeError(f"Le sous-agent {agent.name} est désactivé.")
            job = SubAgentJob(
                id=uuid.uuid4().hex[:12],
                agent_id=agent.id,
                objective=objective.strip(),
                context=_clip(context, self.max_context_chars),
                priority=max(0, int(priority)),
                parent_task_id=parent_task_id,
                parent_event_id=parent_event_id,
                route_metadata=dict(route_metadata or {}),
            )
            self._jobs[job.id] = job
            self._enqueue_locked(job)
            self._save_locked()
            return SubAgentJob.from_dict(job.to_dict())

    def get_job(self, job_id: str) -> SubAgentJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return SubAgentJob.from_dict(job.to_dict()) if job else None

    def list_jobs(self, *, status: str | None = None, limit: int = 20) -> list[SubAgentJob]:
        requested = SubAgentJobStatus(status) if status else None
        with self._lock:
            values = [job for job in self._jobs.values() if requested is None or job.status == requested]
            values.sort(key=lambda item: item.created_at, reverse=True)
            return [SubAgentJob.from_dict(item.to_dict()) for item in values[: max(1, min(limit, 100))]]

    def cancel_job(self, job_id: str) -> SubAgentJob:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(f"Job de sous-agent inconnu : {job_id}")
            if job.status not in self.TERMINAL_JOB_STATUSES:
                job.cancel_requested = True
                if job.status == SubAgentJobStatus.QUEUED:
                    job.status = SubAgentJobStatus.CANCELLED
                    job.completed_at = _now()
                job.updated_at = _now()
                self._save_locked()
            return SubAgentJob.from_dict(job.to_dict())

    def _worker(self) -> None:
        while not self._stop_requested.is_set():
            try:
                _, _, job_id = self._queue.get(timeout=0.2)
            except Empty:
                continue
            try:
                self._execute_job(job_id)
            finally:
                self._queue.task_done()

    def _execute_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != SubAgentJobStatus.QUEUED or job.cancel_requested:
                return
            agent = self._agents.get(job.agent_id)
            if agent is None or agent.status != SubAgentStatus.ACTIVE:
                job.status = SubAgentJobStatus.FAILED
                job.error = "Sous-agent absent ou désactivé."
                job.completed_at = _now()
                job.updated_at = _now()
                self._save_locked()
                self._publish(job, "subagent.failed", job.error, EventPriority.NORMAL)
                return
            agent_copy = SubAgent.from_dict(agent.to_dict())
            job.status = SubAgentJobStatus.RUNNING
            job.started_at = _now()
            job.updated_at = _now()
            self._save_locked()

        try:
            result = self._run_agent(agent_copy, job_id)
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None:
                    return
                if job.cancel_requested:
                    job.status = SubAgentJobStatus.CANCELLED
                    event_type = "subagent.cancelled"
                    message = "Travail annulé."
                else:
                    job.status = SubAgentJobStatus.COMPLETED
                    job.result = _clip(result, self.max_result_chars)
                    event_type = "subagent.completed"
                    message = job.result
                job.completed_at = _now()
                job.updated_at = _now()
                self._save_locked()
            self._publish(job, event_type, message, EventPriority.NORMAL)
        except Exception as exc:
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None:
                    return
                job.status = SubAgentJobStatus.FAILED
                job.error = f"{type(exc).__name__}: {exc}"
                job.completed_at = _now()
                job.updated_at = _now()
                self._save_locked()
            self._publish(job, "subagent.failed", job.error, EventPriority.NORMAL)

    def _allowed_tool_definitions(self, agent: SubAgent) -> list[dict[str, Any]]:
        allowed = set(agent.allowed_tools)
        return [
            definition
            for definition in self.llm_client.tool_definitions()
            if definition.get("function", {}).get("name") in allowed
        ]

    def _run_agent(self, agent: SubAgent, job_id: str) -> str:
        with self._lock:
            job = self._jobs[job_id]
            objective = job.objective
            delegated_context = job.context
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": agent.system_prompt},
            {
                "role": "user",
                "content": (
                    f"Objectif délégué par Orion :\n{objective}\n\n"
                    f"Contexte utile, potentiellement incomplet :\n{delegated_context or '(aucun)'}"
                ),
            },
        ]
        tools = self._allowed_tool_definitions(agent)
        for turn in range(agent.max_turns):
            if self._is_cancel_requested(job_id):
                return "Travail interrompu à la demande d'Orion."
            response = self.llm_client.complete(
                messages,
                model=agent.model,
                tools=tools or None,
                parallel_tool_calls=True if tools else None,
            )
            assistant = OpenRouterClient._assistant_message(response)
            messages.append(assistant)
            calls = OpenRouterClient._tool_calls(assistant)
            text = OpenRouterClient.text_from_message(assistant).strip()
            if not calls:
                return text or "Le sous-agent a terminé sans produire de résultat exploitable."
            if text:
                self._record_progress(job_id, text)
            for call in calls:
                if self._is_cancel_requested(job_id):
                    return "Travail interrompu à la demande d'Orion."
                name = str(call.get("function", {}).get("name", ""))
                if name not in agent.allowed_tools:
                    result = self._tool_message(call, {"error": f"Tool non autorisé pour ce sous-agent : {name}"})
                else:
                    result = self.llm_client.execute_tool_call(call, raise_tool_errors=False)
                    result["content"] = _clip(result.get("content", ""), self.max_tool_output_chars)
                messages.append(result)
                self._record_tool_call(job_id, call)

        final_instruction = {
            "role": "system",
            "content": "La limite d'étapes est atteinte. Ne lance plus d'outil et fournis maintenant le meilleur résultat exploitable à Orion.",
        }
        response = self.llm_client.complete([*messages, final_instruction], model=agent.model, tools=None)
        assistant = OpenRouterClient._assistant_message(response)
        return OpenRouterClient.text_from_message(assistant).strip() or "Travail partiellement terminé."

    @staticmethod
    def _tool_message(call: Mapping[str, Any], value: Any) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": str(call.get("id") or uuid.uuid4().hex),
            "name": str(call.get("function", {}).get("name", "unknown_tool")),
            "content": json.dumps(value, ensure_ascii=False, default=str),
        }

    def _is_cancel_requested(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            return job is None or job.cancel_requested

    def _record_progress(self, job_id: str, message: str) -> None:
        compact = _clip(message, 1200)
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.progress.append(compact)
            job.progress = job.progress[-20:]
            job.updated_at = _now()
            self._save_locked()
            snapshot = SubAgentJob.from_dict(job.to_dict())
        if self.emit_progress_events:
            self._publish(snapshot, "subagent.progress", compact, EventPriority.LOW)

    def _record_tool_call(self, job_id: str, call: Mapping[str, Any]) -> None:
        function = call.get("function", {})
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.tool_calls.append(
                {
                    "name": str(function.get("name", "")),
                    "arguments": _clip(function.get("arguments", {}), 2000),
                    "at": _now(),
                }
            )
            job.tool_calls = job.tool_calls[-50:]
            job.updated_at = _now()
            self._save_locked()

    def _publish(self, job: SubAgentJob, event_type: str, message: str | None, priority: EventPriority) -> None:
        agent = self.get_agent(job.agent_id)
        payload = {
            "job_id": job.id,
            "agent_id": job.agent_id,
            "agent_name": agent.name if agent else job.agent_id,
            "status": job.status.value,
            "objective": job.objective,
            "message": message,
            "result": job.result if event_type == "subagent.completed" else None,
            "error": job.error if event_type == "subagent.failed" else None,
            "parent_task_id": job.parent_task_id,
        }
        metadata = {
            **job.route_metadata,
            "subagent_job_id": job.id,
            "subagent_id": job.agent_id,
            "parent_event_id": job.parent_event_id,
        }
        self.event_handler.publish(
            event_type,
            payload,
            priority=priority,
            source=f"subagent:{job.agent_id}",
            metadata=metadata,
            max_attempts=1,
        )


__all__ = [
    "SubAgent",
    "SubAgentJob",
    "SubAgentJobStatus",
    "SubAgentManager",
    "SubAgentStatus",
]
