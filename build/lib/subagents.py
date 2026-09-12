"""Sous-agents persistants et indépendants du runtime principal d'Orion."""

from __future__ import annotations

import json
import hashlib
import copy
import os
import re
import threading
import time
import uuid
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from itertools import count
from pathlib import Path
from queue import Empty, PriorityQueue
from typing import Any, Callable, Mapping

from action_ledger import ActionLedger, normalize_action_value
from event_handler import EventHandler, EventPriority
from openrouter_client import OpenRouterClient
from handoff_context import HandoffContext
from tool_policy import ToolClassification, ToolPolicy


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
                f"SubAgentManager state_path est déjà ouvert par un autre writer : {self.target}"
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _durable_event_receipt(event_handler: Any, idempotency_key: str) -> Any | None:
    """Return the durable EventHandler receipt for one stable producer key."""
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
    """Requeue the exact failed durable receipt without changing its identity.

    EventHandler intentionally treats FAILED receipts as terminal and exposes no
    public retry transition. Producer outboxes, however, must be able to recover
    a downstream handoff failure without minting a new idempotency key. Keep the
    repair narrowly fenced to the same receipt/status and then hydrate that
    queued receipt through EventHandler's normal RAM boundary.
    """
    receipt = _durable_event_receipt(event_handler, idempotency_key)
    if receipt is None or str(getattr(receipt, "status", "")) != "failed":
        return False
    store = getattr(event_handler, "_durable_store", None)
    lock = getattr(store, "_lock", None)
    db = getattr(store, "_db", None)
    namespace = getattr(store, "namespace", None)
    if lock is None or db is None or namespace is None:
        return False
    now = time.time()
    try:
        with lock:
            db.execute("BEGIN IMMEDIATE")
            try:
                changed = db.execute(
                    "UPDATE durable_events SET status='queued', owner_id=NULL, "
                    "lease_until=NULL, last_error=NULL, updated_at=? "
                    "WHERE receipt_id=? AND namespace=? AND status='failed'",
                    (now, str(receipt.receipt_id), str(namespace)),
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
            # The durable row remains queued and will hydrate on the next
            # EventHandler poll/start even if RAM admission is unavailable now.
            pass
    return True


def _clip(value: Any, limit: int) -> str:
    """Serialize a value and cap its textual representation safely."""
    limit = max(1, int(limit))
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


_SECRET_PATTERNS = (
    (re.compile(r"(?i)(bearer\s+)[a-z0-9._~+/=-]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:api[_ -]?key|token|secret|password)\s*[:=]\s*)[^\s,;]+"), r"\1[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "[REDACTED]"),
)


def _redact(value: Any) -> str:
    """Redact common credential forms before they enter durable state."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _redact_structure(value: Any) -> Any:
    """Copy JSON-like metadata while redacting sensitive string leaves."""
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, Mapping):
        return {str(key): _redact_structure(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_structure(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_structure(item) for item in value]
    return value


class SubAgentStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class SubAgentJobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
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
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    context: str = ""
    priority: int = int(EventPriority.NORMAL)
    status: SubAgentJobStatus = SubAgentJobStatus.QUEUED
    parent_task_id: int | None = None
    parent_event_id: str | None = None
    route_metadata: dict[str, Any] = field(default_factory=dict)
    result: str | None = None
    error: str | None = None
    waiting_for: str | None = None
    pending_approval_id: str | None = None
    pending_approval_tool: str | None = None
    pending_approval_call_id: str | None = None
    pending_approval_status: str | None = None
    progress: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    cancel_requested: bool = False
    pause_requested: bool = False
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    completed_at: str | None = None
    updated_at: str = field(default_factory=_now)
    handoff_context: HandoffContext | None = None
    state_version: int = 0

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SubAgentJob":
        return cls(
            id=str(value["id"]),
            agent_id=str(value["agent_id"]),
            objective=str(value["objective"]),
            session_id=str(value.get("session_id", uuid.uuid4().hex[:12])),
            context=str(value.get("context", "")),
            priority=int(value.get("priority", int(EventPriority.NORMAL))),
            status=SubAgentJobStatus(value.get("status", SubAgentJobStatus.QUEUED.value)),
            parent_task_id=value.get("parent_task_id"),
            parent_event_id=value.get("parent_event_id"),
            route_metadata=dict(value.get("route_metadata", {})),
            result=value.get("result"),
            error=value.get("error"),
            waiting_for=value.get("waiting_for"),
            pending_approval_id=value.get("pending_approval_id"),
            pending_approval_tool=value.get("pending_approval_tool"),
            pending_approval_call_id=value.get("pending_approval_call_id"),
            pending_approval_status=value.get("pending_approval_status"),
            progress=[str(item) for item in value.get("progress", [])],
            tool_calls=[dict(item) for item in value.get("tool_calls", [])],
            cancel_requested=bool(value.get("cancel_requested", False)),
            pause_requested=bool(value.get("pause_requested", False)),
            created_at=str(value.get("created_at", _now())),
            started_at=value.get("started_at"),
            completed_at=value.get("completed_at"),
            updated_at=str(value.get("updated_at", _now())),
            handoff_context=(
                HandoffContext.from_dict(value.get("handoff_context"))
                if value.get("handoff_context") else None
            ),
            state_version=max(0, int(value.get("state_version", 0))),
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        if self.handoff_context is not None:
            value["handoff_context"] = self.handoff_context.to_dict()
        return value

    @property
    def handoff(self) -> HandoffContext | None:
        return self.handoff_context


@dataclass
class SubAgentSession:
    """Historique LLM isolé d'un job, conservé entre deux reprises."""

    id: str
    job_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    status: str = "active"
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SubAgentSession":
        return cls(
            id=str(value["id"]),
            job_id=str(value["job_id"]),
            messages=[dict(item) for item in value.get("messages", []) if isinstance(item, Mapping)],
            status=str(value.get("status", "active")),
            created_at=str(value.get("created_at", _now())),
            updated_at=str(value.get("updated_at", _now())),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _WaitingResult:
    message: str


class SubAgentManager:
    """Registre, persistance et pool de workers pour les sous-agents."""

    _MAX_DOWNSTREAM_RECOVERIES = 3

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
        tool_guidance: Mapping[str, Any] | None = None,
        default_max_turns: int = 8,
        max_context_chars: int = 16000,
        max_result_chars: int = 24000,
        max_tool_output_chars: int = 12000,
        max_runtime_seconds: float = 900.0,
        max_session_messages: int = 100,
        max_session_chars: int | None = None,
        max_session_tokens: int | None = None,
        history_limit: int = 200,
        emit_progress_events: bool = True,
        scope: str = "default",
        instance_id: str = "orion",
        tool_policy: ToolPolicy | None = None,
        tool_authorizer: Callable[[SubAgent, SubAgentJob, str, str], bool] | None = None,
        tool_approval_broker: Callable[
            [SubAgent, SubAgentJob, str, str, Mapping[str, Any]], Mapping[str, Any] | bool
        ]
        | None = None,
        action_ledger: ActionLedger | None = None,
        action_ledger_path: str | Path | None = None,
    ) -> None:
        if workers < 1 or default_max_turns < 1:
            raise ValueError("workers et default_max_turns doivent être positifs.")
        if any(int(value) < 1 for value in (max_context_chars, max_result_chars, max_tool_output_chars, max_session_messages, history_limit)):
            raise ValueError("Les limites de contexte, résultats et historique doivent être positives.")
        if max_session_chars is not None and int(max_session_chars) < 1:
            raise ValueError("max_session_chars doit être positif.")
        if max_session_tokens is not None and int(max_session_tokens) < 1:
            raise ValueError("max_session_tokens doit être positif.")
        if float(max_runtime_seconds) <= 0:
            raise ValueError("La durée maximale d'un job doit être positive.")
        self.llm_client = llm_client
        self.event_handler = event_handler
        self.state_path = Path(state_path).resolve()
        self._state_writer_lock = _SingleWriterFileLock(self.state_path)
        self._state_writer_lock.acquire()
        self._closed = False
        self.workers = int(workers)
        self.default_model = default_model or llm_client.model
        # Capability ceilings are fail-closed.  A sub-agent created without an
        # explicit operator tool list must not silently inherit bundled tools;
        # those packages may not even be enabled in a modular/default-empty
        # Orion installation.
        configured_default_tools = [] if default_tools is None else default_tools
        self.default_tools = tuple(self._normalize_tool_names(configured_default_tools))
        # Operator configuration is the immutable capability ceiling for every
        # model-created or persisted sub-agent.  Keep the enforcement set
        # separate from the public default tuple so later accidental mutation
        # or reassignment cannot widen the ceiling.
        self._tool_capability_ceiling = frozenset(self.default_tools)
        # Manifest ids are used as keys.  Keep this separate from tool
        # definitions: guidance is only rendered for the system message of a
        # worker and is never copied into delegated user context.
        self.tool_guidance = dict(tool_guidance or {})
        self.default_max_turns = int(default_max_turns)
        self.max_context_chars = int(max_context_chars)
        self.max_result_chars = int(max_result_chars)
        self.max_tool_output_chars = int(max_tool_output_chars)
        self.max_runtime_seconds = float(max_runtime_seconds)
        self.max_session_messages = max(20, int(max_session_messages))
        # Message-count bounding alone still allowed 100 individually bounded
        # messages to exceed a provider context by an order of magnitude.  Use
        # a cumulative budget as well.  Four times the delegated-context cap is
        # deliberately conservative for backwards compatibility while turning
        # the previous ~1.6M-character worst case into a bounded request.
        self.max_session_chars = int(
            max_session_chars
            if max_session_chars is not None
            else max(1, self.max_context_chars * 4)
        )
        self.max_session_tokens = int(
            max_session_tokens
            if max_session_tokens is not None
            else max(1, self.max_session_chars // 4)
        )
        self.history_limit = max(10, int(history_limit))
        self.emit_progress_events = bool(emit_progress_events)
        self.scope = str(scope).strip() or "default"
        self.instance_id = str(instance_id).strip() or "orion"
        self.tool_policy = tool_policy.copy() if tool_policy is not None else ToolPolicy()
        self.tool_authorizer = tool_authorizer
        self.tool_approval_broker = tool_approval_broker
        if action_ledger is not None and action_ledger_path is not None:
            raise ValueError("action_ledger et action_ledger_path sont mutuellement exclusifs.")
        if action_ledger_path is None:
            ledger_path: str | Path = self.state_path.with_name("action_ledger.sqlite3")
        elif str(action_ledger_path) == ":memory:":
            ledger_path = ":memory:"
        else:
            ledger_path = Path(action_ledger_path).resolve()
        # Use the same durable ledger filename as AgentRuntime by default.  A
        # caller may inject the runtime ledger directly when configuration uses
        # a custom path, avoiding a second idempotency domain.
        self._owns_action_ledger = action_ledger is None
        try:
            self.action_ledger = action_ledger or ActionLedger(
                ledger_path,
                owner_id=f"subagent:{self.instance_id}:{uuid.uuid4().hex}",
            )
        except Exception:
            self._state_writer_lock.release()
            raise

        self._lock = threading.RLock()
        self._external_call_gate = threading.Lock()
        self._persistence_lock = threading.Lock()
        self._persistence_generation = 0
        self._persisted_generation = 0
        self._agents: dict[str, SubAgent] = {}
        self._jobs: dict[str, SubAgentJob] = {}
        self._sessions: dict[str, SubAgentSession] = {}
        self._queue: PriorityQueue[tuple[int, int, str]] = PriorityQueue()
        self._sequence = count()
        self._threads: list[threading.Thread] = []
        self._stop_requested = threading.Event()
        self._running = False
        self._outbox: dict[str, dict[str, Any]] = {}
        self._published_outbox: set[str] = set()
        self._outbox_claimed: set[str] = set()
        self._outbox_ready_generation: dict[str, int] = {}
        try:
            self._load()
        except Exception:
            if self._owns_action_ledger:
                self.action_ledger.close()
            self._state_writer_lock.release()
            raise

    @property
    def running(self) -> bool:
        return self._running

    def snapshot(self) -> dict[str, Any]:
        """Return compact operational counters without objectives or results."""
        with self._lock:
            agent_counts = {status.value: 0 for status in SubAgentStatus}
            for agent in self._agents.values():
                agent_counts[agent.status.value] += 1

            job_counts = {status.value: 0 for status in SubAgentJobStatus}
            oldest_pending_at: datetime | None = None
            for job in self._jobs.values():
                job_counts[job.status.value] += 1
                if job.status in {
                    SubAgentJobStatus.QUEUED,
                    SubAgentJobStatus.RUNNING,
                    SubAgentJobStatus.WAITING,
                }:
                    try:
                        created_at = datetime.fromisoformat(job.created_at)
                    except (TypeError, ValueError):
                        created_at = None
                    if created_at is not None and (
                        oldest_pending_at is None or created_at < oldest_pending_at
                    ):
                        oldest_pending_at = created_at

            outbox_pending = sum(
                1 for item in self._outbox.values() if not item.get("published")
            )
            outbox_published = sum(
                1 for item in self._outbox.values() if item.get("published")
            )
            claimed = len(self._outbox_claimed)
            alive_workers = sum(1 for thread in self._threads if thread.is_alive())

        now = datetime.now(timezone.utc)
        return {
            "component": "subagents",
            "running": self.running,
            "workers": {"configured": self.workers, "alive": alive_workers},
            "agents": {
                "total": sum(agent_counts.values()),
                "status_counts": agent_counts,
            },
            "job_status_counts": job_counts,
            "pending": job_counts.get(SubAgentJobStatus.QUEUED.value, 0)
            + job_counts.get(SubAgentJobStatus.WAITING.value, 0),
            "inflight": job_counts.get(SubAgentJobStatus.RUNNING.value, 0),
            "failed": job_counts.get(SubAgentJobStatus.FAILED.value, 0),
            "retry_pending": outbox_pending,
            "outbox": {
                "pending": outbox_pending,
                "inflight": claimed,
                "published": outbox_published,
            },
            "oldest_pending_age_seconds": (
                max(0.0, (now - oldest_pending_at.astimezone(timezone.utc)).total_seconds())
                if oldest_pending_at is not None
                else None
            ),
        }

    def _load(self) -> None:
        if not self.state_path.is_file():
            return
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"État des sous-agents illisible : {self.state_path}") from exc
        pending_save: tuple[int, dict[str, Any]] | None = None
        with self._lock:
            try:
                loaded_generation = max(0, int(value.get("persistence_generation", 0)))
            except (TypeError, ValueError):
                loaded_generation = 0
            self._persistence_generation = loaded_generation
            self._persisted_generation = loaded_generation
            self._agents = {
                item.id: item
                for item in (SubAgent.from_dict(raw) for raw in value.get("agents", []))
            }
            capabilities_constrained = False
            for agent in self._agents.values():
                constrained = [
                    name
                    for name in self._normalize_tool_names(agent.allowed_tools)
                    if name in self._tool_capability_ceiling
                ]
                if constrained != agent.allowed_tools:
                    agent.allowed_tools = constrained
                    agent.updated_at = _now()
                    capabilities_constrained = True
            self._jobs = {
                item.id: item
                for item in (SubAgentJob.from_dict(raw) for raw in value.get("jobs", []))
            }
            self._sessions = {
                item.id: item
                for item in (SubAgentSession.from_dict(raw) for raw in value.get("sessions", []))
            }
            self._outbox = {
                str(item.get("key")): dict(item)
                for item in value.get("outbox", [])
                if isinstance(item, Mapping) and item.get("key")
            }
            # Every item loaded from disk is already durable at the generation
            # represented by that file (legacy files use generation zero).
            self._outbox_ready_generation = {
                key: loaded_generation for key in self._outbox
            }
            recovery_changed = False
            for job in self._jobs.values():
                if job.session_id not in self._sessions:
                    self._sessions[job.session_id] = SubAgentSession(
                        id=job.session_id,
                        job_id=job.id,
                    )
                if job.status == SubAgentJobStatus.RUNNING and job.cancel_requested:
                    job.status = SubAgentJobStatus.CANCELLED
                    job.completed_at = _now()
                    job.error = "Job annulé avant le redémarrage d'Orion."
                    job.state_version += 1
                    self._queue_outbox_locked(job, "subagent.cancelled", job.error, EventPriority.NORMAL)
                    recovery_changed = True
                elif job.status == SubAgentJobStatus.RUNNING:
                    job.status = SubAgentJobStatus.QUEUED
                    job.started_at = None
                    job.error = "Job repris après redémarrage d'Orion."
                    job.state_version += 1
                    recovery_changed = True
                if job.status == SubAgentJobStatus.QUEUED and not job.cancel_requested:
                    self._enqueue_locked(job)
            if capabilities_constrained or recovery_changed:
                # Persist secure capability migration and restart recovery
                # immediately so a second crash cannot resurrect stale state.
                pending_save = self._prepare_save_locked()
        if pending_save is not None:
            self._persist_snapshot(pending_save)
        # A manager restart is also a delivery retry point.  Publishing is
        # outside the load lock so a handler may safely call back into us.
        self._drain_outbox()

    def _prepare_save_locked(self) -> tuple[int, dict[str, Any]]:
        """Capture an immutable state snapshot while the state lock is held.

        JSON serialization and disk I/O intentionally happen later, outside
        ``self._lock``.  A monotonic generation lets concurrent writers safely
        coalesce: once a newer generation is durable, an older snapshot is
        never allowed to replace it.
        """
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
        retained_session_ids = {job.session_id for job in self._jobs.values()}
        self._sessions = {
            session_id: session
            for session_id, session in self._sessions.items()
            if session_id in retained_session_ids
        }
        # Published notifications are only delivery history; retain a bounded
        # tail while preserving every pending notification for retry.
        published = sorted(
            (
                item
                for item in self._outbox.values()
                if item.get("downstream_acked", False)
                or item.get("delivery_failed", False)
            ),
            key=lambda item: str(item.get("key", "")),
            reverse=True,
        )
        retained_outbox = {str(item.get("key")): item for item in published[: self.history_limit]}
        retained_outbox.update(
            (str(key), item)
            for key, item in self._outbox.items()
            if not item.get("downstream_acked", False)
            and not item.get("delivery_failed", False)
        )
        self._outbox = retained_outbox
        self._persistence_generation += 1
        generation = self._persistence_generation
        self._outbox_ready_generation = {
            key: self._outbox_ready_generation.get(key, generation)
            for key in self._outbox
        }
        payload = {
            "version": 1,
            "persistence_generation": generation,
            "agents": [item.to_dict() for item in self._agents.values()],
            "jobs": [item.to_dict() for item in self._jobs.values()],
            "sessions": [item.to_dict() for item in self._sessions.values()],
            "outbox": copy.deepcopy(list(self._outbox.values())),
        }
        return generation, payload

    def _persist_snapshot(self, snapshot: tuple[int, dict[str, Any]]) -> bool:
        """Atomically persist one captured generation without stale overwrite."""
        generation, payload = snapshot
        # The expensive full-state serialization is deliberately outside both
        # the mutable-state lock and the short critical section below.
        encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        with self._persistence_lock:
            if generation <= self._persisted_generation:
                return False
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_name(
                f".{self.state_path.name}.{generation}.{uuid.uuid4().hex}.tmp"
            )
            try:
                temporary.write_text(encoded, encoding="utf-8")
                os.replace(temporary, self.state_path)
                self._persisted_generation = generation
            finally:
                if temporary.exists():
                    temporary.unlink()
        return True

    def _save_locked(self) -> None:
        """Compatibility save helper; only snapshot capture holds state lock."""
        with self._lock:
            snapshot = self._prepare_save_locked()
        self._persist_snapshot(snapshot)

    def _enqueue_locked(self, job: SubAgentJob) -> None:
        self._queue.put((-job.priority, next(self._sequence), job.id))

    def _job_visible_locked(self, job: SubAgentJob, scope: str, parent_task_id: int | None, correlation_id: str | None) -> bool:
        if scope == "*":
            return False
        handoff_scope = job.handoff_context.source.get("scope") if job.handoff_context else self.scope
        if handoff_scope != scope and (job.handoff_context.target.get("scope") if job.handoff_context else None) != scope:
            return False
        if parent_task_id is not None and job.parent_task_id != parent_task_id:
            return False
        if correlation_id is not None and (not job.handoff_context or job.handoff_context.correlation_id != str(correlation_id)):
            return False
        return True

    def _authorize_job_locked(self, job: SubAgentJob, caller_scope: str | None) -> None:
        scope = caller_scope or self.scope
        if not self._job_visible_locked(job, scope, None, None):
            raise PermissionError("Job de sous-agent hors du scope du caller")

    def _event_payload(self, job: SubAgentJob, event_type: str, message: str | None) -> dict[str, Any]:
        agent = self._agents.get(job.agent_id)
        agent_name = agent.name if agent else job.agent_id
        result = _redact(job.result) if event_type == "subagent.completed" and job.result else None
        error = _redact(job.error) if event_type == "subagent.failed" and job.error else None
        waiting_for = _redact(job.waiting_for) if event_type == "subagent.waiting" and job.waiting_for else None
        correlation_id = job.handoff_context.correlation_id if job.handoff_context else None
        handoff_id = job.handoff_context.handoff_id if job.handoff_context else None
        resume_orchestrator = bool(job.route_metadata.get("resume_orchestrator"))
        delivery_mode = (
            "durable_task"
            if job.parent_task_id is not None
            else "taskless_conversational"
            if resume_orchestrator
            else "standalone"
        )
        return {
            "internal_event": True, "event_type": event_type, "job_id": job.id,
            "session_id": job.session_id, "agent_id": job.agent_id,
            "agent_name": agent_name, "status": job.status.value,
            "objective": _redact(job.objective), "message": _redact(message) if message else message,
            # Legacy flat fields stay intact.  The structured outcome/provenance
            # contract removes the old ambiguity where ``message`` meant a
            # result, an error, or a waiting question depending on event_type.
            "result": result,
            "error": error,
            "waiting_for": waiting_for,
            "parent_task_id": job.parent_task_id,
            "handoff_id": handoff_id,
            "correlation_id": correlation_id,
            "parent_event_id": job.parent_event_id,
            "state_version": job.state_version,
            "provenance": {
                "kind": "subagent",
                "agent_id": job.agent_id,
                "agent_name": agent_name,
                "job_id": job.id,
                "session_id": job.session_id,
                "handoff_id": handoff_id,
                "correlation_id": correlation_id,
                "parent_event_id": job.parent_event_id,
            },
            "outcome": {
                "status": job.status.value,
                "terminal": job.status in self.TERMINAL_JOB_STATUSES,
                "result": result,
                "error": error,
                "waiting_for": waiting_for,
            },
            "delivery": {
                "mode": delivery_mode,
                "resume_orchestrator": resume_orchestrator,
                "parent_task_id": job.parent_task_id,
            },
            "handoff_context": _redact_structure(job.handoff_context.to_dict()) if job.handoff_context else None,
        }

    def _queue_outbox_locked(self, job: SubAgentJob, event_type: str, message: str | None, priority: EventPriority) -> None:
        handoff_id = job.handoff_context.handoff_id if job.handoff_context else job.id
        key = f"{handoff_id}:{job.state_version}"
        self._outbox.setdefault(key, {
            "key": key, "event_type": event_type,
            "job_id": job.id,
            "state_version": job.state_version,
            "idempotency_key": f"subagent:{job.id}:v{job.state_version}",
            "payload": self._event_payload(job, event_type, message),
            # Preserve the originating channel and recipient through the
            # durable outbox.  Without this, a completed Telegram delegation
            # is replayed as an unaddressed event and falls back to the CLI
            # default channel after a worker/restart race.
            "metadata": {
                **_redact_structure(job.route_metadata),
                "handoff_id": handoff_id,
                "correlation_id": job.handoff_context.correlation_id if job.handoff_context else None,
                "parent_event_id": job.parent_event_id,
                "state_version": job.state_version,
                "internal_event": True,
            },
            "priority": int(priority), "published": False,
            "downstream_acked": False,
            "delivery_failed": False,
            "downstream_recoveries": 0,
        })

    @staticmethod
    def _outbox_idempotency_key(item: Mapping[str, Any]) -> str | None:
        explicit = item.get("idempotency_key")
        if explicit:
            return str(explicit)
        payload = item.get("payload")
        if isinstance(payload, Mapping):
            job_id = payload.get("job_id")
            state_version = payload.get("state_version")
            if job_id and state_version is not None:
                return f"subagent:{job_id}:v{state_version}"
        return None

    def _drain_outbox(self) -> None:
        # A durable EventHandler receipt can fail *after* publish() accepted it.
        # Published therefore means "accepted by EventHandler", not irrevocably
        # delivered. Keep unacked entries in the durable outbox and revive the
        # exact FAILED receipt under its stable idempotency key.
        reconciliation_save = None
        with self._lock:
            published_items = [
                dict(item) for item in self._outbox.values() if item.get("published")
            ]
        durable_enabled = getattr(self.event_handler, "_durable_store", None) is not None
        reconciliation_changed = False
        for item in published_items:
            stable_key = self._outbox_idempotency_key(item)
            if not stable_key:
                continue
            receipt = _durable_event_receipt(self.event_handler, stable_key)
            status = str(getattr(receipt, "status", "")) if receipt is not None else None
            if status == "failed":
                recoveries = int(item.get("downstream_recoveries", 0))
                if recoveries >= self._MAX_DOWNSTREAM_RECOVERIES:
                    with self._lock:
                        current = self._outbox.get(str(item.get("key", "")))
                        if current is not None and not current.get("delivery_failed", False):
                            current["published"] = False
                            current["delivery_failed"] = True
                            current["downstream_acked"] = False
                            current["last_downstream_error"] = str(
                                getattr(receipt, "last_error", "") or "event_handler_failed"
                            )[:1000]
                            self._published_outbox.discard(str(item.get("key", "")))
                            reconciliation_changed = True
                elif _revive_failed_event_receipt(self.event_handler, stable_key):
                    status = "queued"
                    with self._lock:
                        current = self._outbox.get(str(item.get("key", "")))
                        if current is not None:
                            current["downstream_recoveries"] = int(
                                current.get("downstream_recoveries", 0)
                            ) + 1
                            current["last_downstream_recovery_at"] = _now()
                            current["downstream_acked"] = False
                            current["delivery_failed"] = False
                            reconciliation_changed = True
            elif status == "acked":
                with self._lock:
                    current = self._outbox.get(str(item.get("key", "")))
                    if current is not None and not current.get("downstream_acked", False):
                        current["downstream_acked"] = True
                        reconciliation_changed = True
            elif not durable_enabled:
                with self._lock:
                    current = self._outbox.get(str(item.get("key", "")))
                    if current is not None and not current.get("downstream_acked", False):
                        current["downstream_acked"] = True
                        reconciliation_changed = True
        if reconciliation_changed:
            with self._lock:
                reconciliation_save = self._prepare_save_locked()
            self._persist_snapshot(reconciliation_save)

        # Legacy/internal callers may queue an item and drain immediately
        # without an explicit state save. Arm those items with a durable
        # snapshot first; normal mutation paths already assign this generation
        # in their own snapshot before releasing the state lock.
        pending_save = None
        with self._lock:
            if any(
                not item.get("published")
                and str(item.get("key", ""))
                and str(item.get("key", "")) not in self._outbox_ready_generation
                for item in self._outbox.values()
            ):
                pending_save = self._prepare_save_locked()
        if pending_save is not None:
            self._persist_snapshot(pending_save)

        pending = []
        with self._persistence_lock:
            durable_generation = self._persisted_generation
        with self._lock:
            pending = []
            for item in self._outbox.values():
                key = str(item.get("key", ""))
                if (
                    not item.get("published")
                    and not item.get("delivery_failed", False)
                    and key
                    and key not in self._outbox_claimed
                    and self._outbox_ready_generation.get(key, durable_generation + 1)
                    <= durable_generation
                ):
                    self._outbox_claimed.add(key)
                    pending.append(dict(item))
        for item in pending:
            try:
                stable_key = str(self._outbox_idempotency_key(item) or "")
                receipt = self.event_handler.publish(
                    item["event_type"],
                    item["payload"],
                    priority=EventPriority(item.get("priority", int(EventPriority.NORMAL))),
                    source="subagent:outbox",
                    metadata=item["metadata"],
                    max_attempts=1,
                    idempotency_key=stable_key or None,
                )
            except Exception:
                with self._lock:
                    self._outbox_claimed.discard(str(item.get("key", "")))
                continue
            durable_receipt = (
                _durable_event_receipt(self.event_handler, stable_key)
                if durable_enabled and stable_key
                else None
            )
            revived_now = False
            if (
                durable_receipt is not None
                and str(getattr(durable_receipt, "status", "")) == "failed"
                and int(item.get("downstream_recoveries", 0))
                < self._MAX_DOWNSTREAM_RECOVERIES
                and _revive_failed_event_receipt(self.event_handler, stable_key)
            ):
                revived_now = True
                durable_receipt = _durable_event_receipt(self.event_handler, stable_key)
            previous_published = False
            previous_published_at = None
            previous_receipt_event_id = None
            receipt_present = False
            with self._lock:
                current = self._outbox.get(item["key"])
                if current is not None:
                    receipt_present = True
                    previous_published = bool(current.get("published"))
                    previous_published_at = current.get("published_at")
                    previous_receipt_event_id = current.get("receipt_event_id")
                    current["published"] = True
                    current["published_at"] = _now()
                    current["receipt_event_id"] = getattr(receipt, "id", None)
                    current["downstream_acked"] = (
                        not durable_enabled
                        or str(getattr(durable_receipt, "status", "")) == "acked"
                    )
                    if revived_now:
                        current["downstream_recoveries"] = int(
                            current.get("downstream_recoveries", 0)
                        ) + 1
                        current["last_downstream_recovery_at"] = _now()
                    self._published_outbox.add(item["key"])

            if not receipt_present:
                with self._lock:
                    self._outbox_claimed.discard(item["key"])
                continue

            try:
                # Capture under the state lock, then serialize/write after it
                # is released. The claim remains live throughout this receipt
                # transaction, so no second drainer can publish the same item.
                self._save_locked()
            except Exception:
                correction_save = None
                with self._lock:
                    current = self._outbox.get(item["key"])
                    if current is not None:
                        # Publication was accepted, but the receipt was not
                        # durably recorded. Restore pending state. Capturing a
                        # newer correction generation also prevents a snapshot
                        # taken during the failed write from later restoring a
                        # stale published=True receipt.
                        current["published"] = previous_published
                        if previous_published_at is None:
                            current.pop("published_at", None)
                        else:
                            current["published_at"] = previous_published_at
                        if previous_receipt_event_id is None:
                            current.pop("receipt_event_id", None)
                        else:
                            current["receipt_event_id"] = previous_receipt_event_id
                        self._published_outbox.discard(item["key"])
                        correction_save = self._prepare_save_locked()
                if correction_save is not None:
                    try:
                        self._persist_snapshot(correction_save)
                    except Exception:
                        # The previously durable pending record remains the
                        # recovery source if storage is still unavailable.
                        pass
            finally:
                with self._lock:
                    self._outbox_claimed.discard(item["key"])

    def start(self) -> "SubAgentManager":
        with self._lock:
            if self._closed:
                raise RuntimeError("SubAgentManager fermé.")
            if self._running:
                return self
            if any(thread.is_alive() for thread in self._threads):
                raise RuntimeError("Le pool précédent est encore actif; attendez son arrêt avant de redémarrer Orion.")
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
        # Linearize shutdown against the admission point of external LLM/tool
        # calls. Calls already admitted may finish, but no new external side
        # effect is admitted once the stop flag has been set here.
        with self._external_call_gate:
            self._stop_requested.set()
        # Do not mark RUNNING work terminal here. A blocking external call may
        # still be in flight; the worker records FAILED only after it regains
        # control and observes the stop, keeping durable state truthful.
        self._drain_outbox()
        threads = list(self._threads)
        if wait:
            for thread in threads:
                if thread is not threading.current_thread():
                    # OrionApplication closes EventHandler immediately after
                    # this method returns. A still-running subagent could then
                    # persist a terminal state and publish its durable wake into
                    # an already-closed consumer. A draining stop must therefore
                    # wait for every admitted external call to settle fully.
                    thread.join()
        with self._lock:
            self._threads = [thread for thread in threads if thread.is_alive()]
            self._running = bool(self._threads)

    def close(self) -> None:
        if self._closed:
            return
        with self._lock:
            needs_stop = self._running or any(thread.is_alive() for thread in self._threads)
        if needs_stop:
            self.stop(wait=True)
        with self._lock:
            if any(thread.is_alive() for thread in self._threads):
                raise RuntimeError("Impossible de fermer SubAgentManager avec des workers actifs.")
            self._closed = True
        if self._owns_action_ledger:
            self.action_ledger.close()
        self._state_writer_lock.release()

    def _raise_if_stopping(self) -> None:
        if self._stop_requested.is_set():
            raise InterruptedError("Sous-agent interrompu lors de l'arrêt du manager.")

    def _external_call(self, callback: Callable[[], Any]) -> Any:
        """Run one LLM/tool call with a shutdown-linearized admission point."""
        with self._external_call_gate:
            self._raise_if_stopping()
        result = callback()
        # A stop requested while the blocking call was in flight prevents any
        # subsequent model/tool action and lets _execute_job settle the job.
        self._raise_if_stopping()
        return result

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
            selected_model = self._validate_model_id(model or self.default_model)
            selected_tools = self._validated_allowed_tools(allowed_tools)
            agent = SubAgent(
                id=uuid.uuid4().hex[:12],
                name=name,
                description=description.strip(),
                model=selected_model,
                system_prompt=(system_prompt or self._default_system_prompt(name, description)).strip(),
                allowed_tools=selected_tools,
                capabilities=list(capabilities or []),
                max_turns=max(1, min(int(max_turns or self.default_max_turns), 30)),
            )
            self._agents[agent.id] = agent
            pending_save = self._prepare_save_locked()
            result = SubAgent.from_dict(agent.to_dict())
        self._persist_snapshot(pending_save)
        return result

    @staticmethod
    def _default_system_prompt(name: str, description: str) -> str:
        return (
            f"Tu es {name}, un sous-agent spécialisé d'Orion. {description.strip()}\n"
            "Accomplis uniquement l'objectif délégué avec les outils autorisés. "
            "Travaille de façon autonome, vérifie tes observations et termine par un résultat "
            "directement exploitable par Orion. N'invente pas de capacités indisponibles. "
            "Si une information d'Orion est nécessaire, utilise wait_for_input au lieu de terminer la session."
        )

    @staticmethod
    def _validate_model_id(value: Any) -> str:
        """Valide un identifiant de modele OpenRouter sans appeler le reseau."""
        model = str(value or "").strip()
        if (
            not model
            or model.startswith(("http://", "https://"))
            or any(char.isspace() for char in model)
            or model.count("/") != 1
        ):
            raise ValueError(
                "Le modele du sous-agent doit utiliser le format OpenRouter "
                "provider/model-name, par exemple deepseek/deepseek-v4-flash-0731."
            )
        provider, model_name = model.split("/", 1)
        if not provider or not model_name:
            raise ValueError(
                "Le modele du sous-agent doit utiliser le format OpenRouter provider/model-name."
            )
        return model

    @staticmethod
    def _normalize_tool_names(values: Any) -> list[str]:
        if values is None:
            return []
        aliases = {
            "orion.web": "web",
            "web_search": "web",
            "web_fetch": "web",
            "fetch_url": "web",
            "fetch_json_api": "web",
            "orion.files": "files",
            "list_files": "files",
            "read_file": "files",
            "search_files": "files",
            "orion.terminal": "terminal",
        }
        normalized: list[str] = []
        seen: set[str] = set()
        for item in values:
            name = str(item).strip()
            name = aliases.get(name, name)
            if not name or name in seen:
                continue
            normalized.append(name)
            seen.add(name)
        return normalized

    def _validated_allowed_tools(self, values: Any) -> list[str]:
        # Manager-level callers and persisted state retain legacy alias
        # compatibility. The model-facing runtime performs stricter canonical
        # callable validation before reaching this layer.
        requested = list(self.default_tools) if values is None else self._normalize_tool_names(values)
        forbidden = sorted(set(requested) - self._tool_capability_ceiling)
        if forbidden:
            ceiling = ", ".join(self.default_tools) or "(aucun tool)"
            raise PermissionError(
                "Tools interdits pour ce sous-agent : "
                + ", ".join(forbidden)
                + f". Plafond configuré : {ceiling}."
            )
        return requested

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
                if key == "allowed_tools":
                    value = self._validated_allowed_tools(value)
                elif key == "capabilities":
                    value = [str(item) for item in value]
                elif key == "model":
                    value = self._validate_model_id(value)
                elif key == "max_turns":
                    value = max(1, min(int(value), 30))
                elif key == "status":
                    value = SubAgentStatus(value)
                else:
                    value = str(value).strip()
                setattr(agent, key, value)
            agent.updated_at = _now()
            pending_save = self._prepare_save_locked()
            result = SubAgent.from_dict(agent.to_dict())
        self._persist_snapshot(pending_save)
        return result

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
                        job.state_version += 1
                        if job.handoff_context is not None:
                            job.handoff_context = job.handoff_context.with_state("cancelled", error="Sous-agent supprimé.")
                        self._queue_outbox_locked(job, "subagent.cancelled", "Sous-agent supprimé.", EventPriority.NORMAL)
                    job.updated_at = _now()
            pending_save = self._prepare_save_locked()
            result = {"deleted": True, "agent_id": agent_id, "jobs_cancelled_or_stopping": affected}
        self._persist_snapshot(pending_save)
        self._drain_outbox()
        return result

    def get_agent(self, agent_id: str) -> SubAgent | None:
        with self._lock:
            agent = self._agents.get(agent_id)
            return SubAgent.from_dict(agent.to_dict()) if agent else None

    def list_agents(self) -> list[SubAgent]:
        with self._lock:
            return [SubAgent.from_dict(item.to_dict()) for item in self._agents.values()]

    @staticmethod
    def _routing_terms(value: str) -> set[str]:
        return {
            token
            for token in re.findall(r"[\w-]+", str(value).lower(), flags=re.UNICODE)
            if len(token) >= 3
        }

    def _agent_loads_locked(self) -> dict[str, tuple[int, int]]:
        """Aggregate active job pressure once for deterministic routing.

        The tuple is ``(weighted_pressure, active_jobs)``.  RUNNING work costs
        more than queued work, while WAITING work still consumes coordination
        capacity but is deliberately cheaper.
        """
        weights = {
            SubAgentJobStatus.QUEUED: 2,
            SubAgentJobStatus.RUNNING: 3,
            SubAgentJobStatus.WAITING: 1,
        }
        loads: dict[str, tuple[int, int]] = {}
        for job in self._jobs.values():
            weight = weights.get(job.status)
            if weight is None:
                continue
            pressure, active_jobs = loads.get(job.agent_id, (0, 0))
            loads[job.agent_id] = (pressure + weight, active_jobs + 1)
        return loads

    def _select_agent_locked(self, objective: str) -> SubAgent:
        candidates = [item for item in self._agents.values() if item.status == SubAgentStatus.ACTIVE]
        if not candidates:
            raise RuntimeError("Aucun sous-agent actif. Crée-en un avant de déléguer.")
        objective_text = " ".join(str(objective).lower().split())
        objective_terms = self._routing_terms(objective)
        loads = self._agent_loads_locked()
        saturation_jobs = max(1, self.workers)

        def semantic_score(agent: SubAgent) -> int:
            score = 0
            name = " ".join(agent.name.lower().split())
            name_terms = self._routing_terms(agent.name)
            if name and name in objective_text:
                score += 120
            score += 24 * len(objective_terms & name_terms)

            for capability in agent.capabilities:
                capability_text = " ".join(str(capability).lower().split())
                capability_terms = self._routing_terms(str(capability))
                if capability_text and capability_text in objective_text:
                    score += 90
                score += 18 * len(objective_terms & capability_terms)

            description_text = " ".join(agent.description.lower().split())
            description_terms = self._routing_terms(agent.description)
            if description_text and description_text in objective_text:
                score += 45
            score += 5 * len(objective_terms & description_terms)
            return score

        ranked: list[tuple[int, int, int, int, str, SubAgent]] = []
        for agent in candidates:
            specialty = semantic_score(agent)
            pressure, active_jobs = loads.get(agent.id, (0, 0))
            saturated = active_jobs >= saturation_jobs

            # Normal load gently balances equivalent specialists. Saturation
            # adds a nonlinear penalty so a busy agent stops attracting every
            # vaguely related job, while an explicit capability/name match can
            # still beat an unrelated idle fallback.
            overload = max(0, active_jobs - saturation_jobs + 1)
            load_penalty = (10 * pressure) + (18 * overload * overload)
            effective = specialty - load_penalty
            ranked.append(
                (
                    effective,
                    specialty,
                    0 if saturated else 1,
                    -pressure,
                    agent.id,
                    agent,
                )
            )

        # Highest effective/specialty score wins. Prefer non-saturated and
        # lighter agents next; perfect ties use the lexicographically smallest
        # id so routing never depends on insertion/dict order.
        ranked.sort(
            key=lambda item: (-item[0], -item[1], -item[2], -item[3], item[4])
        )
        return ranked[0][5]

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
        handoff_context: HandoffContext | Mapping[str, Any] | None = None,
        correlation_id: str | None = None,
        source_scope: str | None = None,
        target_scope: str | None = None,
    ) -> SubAgentJob:
        if not objective.strip():
            raise ValueError("L'objectif délégué ne peut pas être vide.")
        with self._lock:
            agent = self._agents.get(agent_id) if agent_id else self._select_agent_locked(objective)
            if agent is None:
                raise KeyError(f"Sous-agent inconnu : {agent_id}")
            if agent.status != SubAgentStatus.ACTIVE:
                raise RuntimeError(f"Le sous-agent {agent.name} est désactivé.")
            if handoff_context is None:
                source_scope_value = source_scope or self.scope
                handoff_context = HandoffContext.create(
                    kind="subagent",
                    objective=objective,
                    correlation_id=correlation_id or parent_event_id or uuid.uuid4().hex,
                    source_scope=source_scope_value,
                    source_instance_id=self.instance_id,
                    target_scope=target_scope or source_scope_value,
                    target_instance_id=self.instance_id,
                    target_agent_id=agent.id,
                    parent_event_id=parent_event_id,
                    parent_task_id=str(parent_task_id) if parent_task_id is not None else None,
                    memory={"facts": [context]} if context else None,
                    routing=route_metadata,
                )
            elif not isinstance(handoff_context, HandoffContext):
                raw_target = handoff_context.get("target", {}) if isinstance(handoff_context, Mapping) else {}
                if isinstance(raw_target, Mapping) and raw_target.get("agent_id") not in {None, agent.id}:
                    raise PermissionError("Handoff target agent mismatch")
                handoff_context = HandoffContext.from_dict(handoff_context)
            # Enforce tenant scope after normalization for both mappings and
            # pre-built envelopes; typed objects must not bypass this guard.
            source_scope_value = handoff_context.source.get("scope")
            target_scope_value = handoff_context.target.get("scope")
            if source_scope_value not in {None, self.scope} or target_scope_value not in {None, self.scope}:
                raise PermissionError("Handoff scope mismatch")
            if handoff_context.target.get("agent_id") not in {None, agent.id}:
                raise PermissionError("Handoff target agent mismatch")
            # Handoff envelopes are persisted with the job; sanitize their
            # JSON representation without changing the public envelope API.
            handoff_context = HandoffContext.from_dict(_redact_structure(handoff_context.to_dict()))
            job = SubAgentJob(
                id=uuid.uuid4().hex[:12],
                agent_id=agent.id,
                objective=_clip(_redact(objective.strip()), self.max_context_chars),
                context=_clip(_redact(context), self.max_context_chars),
                priority=max(0, int(priority)),
                parent_task_id=parent_task_id,
                parent_event_id=parent_event_id,
                route_metadata=_redact_structure(dict(route_metadata or {})),
                handoff_context=handoff_context,
            )
            self._jobs[job.id] = job
            self._sessions[job.session_id] = SubAgentSession(
                id=job.session_id,
                job_id=job.id,
            )
            self._enqueue_locked(job)
            pending_save = self._prepare_save_locked()
            result = SubAgentJob.from_dict(job.to_dict())
        self._persist_snapshot(pending_save)
        return result

    def get_job(self, job_id: str, *, caller_scope: str | None = None) -> SubAgentJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                self._authorize_job_locked(job, caller_scope)
            return SubAgentJob.from_dict(job.to_dict()) if job else None

    def get_session(self, session_id: str, *, caller_scope: str | None = None) -> SubAgentSession | None:
        """Return a session only if its owning job is visible to the caller."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                job = self._jobs.get(session.job_id)
                if job is None:
                    return None
                self._authorize_job_locked(job, caller_scope)
            return SubAgentSession.from_dict(session.to_dict()) if session else None

    def send_message(self, job_id: str, message: str, *, caller_scope: str | None = None) -> SubAgentJob:
        """Ajoute un message à une session en attente et la remet en file."""
        if not message.strip():
            raise ValueError("Le message destiné au sous-agent ne peut pas être vide.")
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(f"Job de sous-agent inconnu : {job_id}")
            self._authorize_job_locked(job, caller_scope)
            if job.status != SubAgentJobStatus.WAITING:
                raise RuntimeError("Un message ne peut être envoyé qu'à un job en attente.")
            session = self._sessions.get(job.session_id)
            if session is None:
                raise RuntimeError("Session du sous-agent introuvable.")
            session.messages.append({"role": "user", "content": _clip(message.strip(), self.max_context_chars)})
            session.messages = self._bounded_messages(session.messages)
            session.status = "active"
            session.updated_at = _now()
            job.status = SubAgentJobStatus.QUEUED
            job.waiting_for = None
            job.pending_approval_id = None
            job.pending_approval_tool = None
            job.pause_requested = False
            job.cancel_requested = False
            job.updated_at = _now()
            job.state_version += 1
            if job.handoff_context is not None:
                job.handoff_context = job.handoff_context.with_state("queued")
            self._enqueue_locked(job)
            pending_save = self._prepare_save_locked()
            snapshot = SubAgentJob.from_dict(job.to_dict())
        self._persist_snapshot(pending_save)
        self._publish(snapshot, "subagent.resumed", message, EventPriority.NORMAL)
        return snapshot

    def pause_job(self, job_id: str, *, caller_scope: str | None = None) -> SubAgentJob:
        """Suspend un job après son appel courant, ou immédiatement s'il est en file."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(f"Job de sous-agent inconnu : {job_id}")
            self._authorize_job_locked(job, caller_scope)
            if job.status in self.TERMINAL_JOB_STATUSES:
                return SubAgentJob.from_dict(job.to_dict())
            job.pause_requested = True
            if job.status == SubAgentJobStatus.QUEUED:
                job.status = SubAgentJobStatus.WAITING
                job.waiting_for = "pause demandée par Orion"
                session = self._sessions.get(job.session_id)
                if session is not None:
                    session.status = "waiting"
                    session.updated_at = _now()
                job.state_version += 1
                if job.handoff_context is not None:
                    job.handoff_context = job.handoff_context.with_state("waiting", waiting_for=job.waiting_for)
            job.updated_at = _now()
            pending_save = self._prepare_save_locked()
            result = SubAgentJob.from_dict(job.to_dict())
        self._persist_snapshot(pending_save)
        return result

    def resume_job(self, job_id: str, *, caller_scope: str | None = None) -> SubAgentJob:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(f"Job de sous-agent inconnu : {job_id}")
            self._authorize_job_locked(job, caller_scope)
            if job.status != SubAgentJobStatus.WAITING:
                raise RuntimeError("Seul un job en attente peut être repris.")
            job.status = SubAgentJobStatus.QUEUED
            job.waiting_for = None
            job.pending_approval_id = None
            job.pending_approval_tool = None
            job.pause_requested = False
            job.cancel_requested = False
            session = self._sessions.get(job.session_id)
            if session is not None:
                session.status = "active"
                session.updated_at = _now()
            job.updated_at = _now()
            job.state_version += 1
            if job.handoff_context is not None:
                job.handoff_context = job.handoff_context.with_state("queued")
            self._enqueue_locked(job)
            pending_save = self._prepare_save_locked()
            snapshot = SubAgentJob.from_dict(job.to_dict())
        self._persist_snapshot(pending_save)
        self._publish(snapshot, "subagent.resumed", "Reprise demandée par Orion.", EventPriority.NORMAL)
        return snapshot

    def pending_approval_ids(self) -> list[str]:
        with self._lock:
            return sorted({str(job.pending_approval_id) for job in self._jobs.values() if job.status == SubAgentJobStatus.WAITING and job.pending_approval_id})

    def handle_approval_decision(self, approval_id: str, status: str) -> list[str]:
        """Resume jobs blocked on an exact privileged-tool approval decision.

        This is intentionally internal orchestration: the approval event already
        wakes Orion, so resuming the worker here does not emit an additional
        ``subagent.resumed`` event that could create a duplicate orchestration
        wake. The persisted session receives the decision and the model can
        retry the same exact tool call when approved.
        """
        approval_id = str(approval_id).strip()
        status = str(status).strip().lower()
        if not approval_id or status not in {"approved", "rejected", "expired"}:
            return []
        resumed: list[str] = []
        pending_save = None
        with self._lock:
            for job in self._jobs.values():
                if (
                    job.status != SubAgentJobStatus.WAITING
                    or job.pending_approval_id != approval_id
                ):
                    continue
                session = self._sessions.get(job.session_id)
                if session is None:
                    continue
                tool_name = job.pending_approval_tool or "tool privilégié"
                if status == "approved":
                    message = (
                        f"Orion a reçu l'approbation {approval_id} pour {tool_name}. "
                        "Tu peux réessayer exactement le même appel si cet outil est toujours nécessaire."
                    )
                else:
                    message = (
                        f"L'approbation {approval_id} pour {tool_name} est {status}. "
                        "Ne réessaie pas cet appel privilégié ; poursuis avec une alternative sûre."
                    )
                session.messages.append(
                    {"role": "user", "content": _clip(message, self.max_context_chars)}
                )
                session.messages = self._bounded_messages(session.messages)
                session.status = "active"
                session.updated_at = _now()
                job.status = SubAgentJobStatus.QUEUED
                job.waiting_for = None
                job.pending_approval_id = None
                job.pending_approval_tool = None
                job.pause_requested = False
                job.cancel_requested = False
                job.updated_at = _now()
                job.state_version += 1
                if job.handoff_context is not None:
                    job.handoff_context = job.handoff_context.with_state("queued")
                self._enqueue_locked(job)
                resumed.append(job.id)
            if resumed:
                pending_save = self._prepare_save_locked()
        if pending_save is not None:
            self._persist_snapshot(pending_save)
        return resumed

    def list_jobs(self, *, status: str | None = None, limit: int = 20, caller_scope: str | None = None, parent_task_id: int | None = None, correlation_id: str | None = None) -> list[SubAgentJob]:
        requested = SubAgentJobStatus(status) if status else None
        try:
            requested_limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("La limite de jobs doit être un entier.") from exc
        if requested_limit < 1:
            return []
        with self._lock:
            scope = caller_scope or self.scope
            values = [job for job in self._jobs.values() if (requested is None or job.status == requested) and self._job_visible_locked(job, scope, parent_task_id, correlation_id)]
            values.sort(key=lambda item: item.created_at, reverse=True)
            return [SubAgentJob.from_dict(item.to_dict()) for item in values[: min(requested_limit, 100)]]

    def cancel_job(self, job_id: str, *, caller_scope: str | None = None) -> SubAgentJob:
        pending_save = None
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(f"Job de sous-agent inconnu : {job_id}")
            self._authorize_job_locked(job, caller_scope)
            if job.status not in self.TERMINAL_JOB_STATUSES:
                job.cancel_requested = True
                if job.status in {SubAgentJobStatus.QUEUED, SubAgentJobStatus.WAITING}:
                    job.status = SubAgentJobStatus.CANCELLED
                    job.completed_at = _now()
                    session = self._sessions.get(job.session_id)
                    if session is not None:
                        session.status = "cancelled"
                        session.updated_at = _now()
                    job.state_version += 1
                    self._queue_outbox_locked(job, "subagent.cancelled", "Travail annulé.", EventPriority.NORMAL)
                job.updated_at = _now()
                pending_save = self._prepare_save_locked()
            snapshot = SubAgentJob.from_dict(job.to_dict())
        if pending_save is not None:
            self._persist_snapshot(pending_save)
        self._drain_outbox()
        return snapshot

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
        early_failure_save = None
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
                job.state_version += 1
                session = self._sessions.get(job.session_id)
                if session is not None:
                    session.status = "failed"
                    session.updated_at = _now()
                self._queue_outbox_locked(job, "subagent.failed", job.error, EventPriority.NORMAL)
                early_failure_save = self._prepare_save_locked()
                running_save = None
                agent_copy = None
            else:
                agent_copy = SubAgent.from_dict(agent.to_dict())
                job.status = SubAgentJobStatus.RUNNING
                job.started_at = _now()
                job.updated_at = _now()
                job.state_version += 1
                if job.handoff_context is not None:
                    job.handoff_context = job.handoff_context.with_state("running", attempt=int(job.handoff_context.state.get("attempt", 0)) + 1)
                running_save = self._prepare_save_locked()

        if early_failure_save is not None:
            self._persist_snapshot(early_failure_save)
            self._drain_outbox()
            return
        assert running_save is not None and agent_copy is not None
        self._persist_snapshot(running_save)

        try:
            result = self._run_agent(agent_copy, job_id)
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None or job.status in self.TERMINAL_JOB_STATUSES:
                    return
                session = self._sessions.get(job.session_id)
                if job.cancel_requested:
                    job.status = SubAgentJobStatus.CANCELLED
                    event_type = "subagent.cancelled"
                    message = "Travail annulé."
                    if session is not None:
                        session.status = "cancelled"
                    job.completed_at = _now()
                elif isinstance(result, _WaitingResult):
                    job.status = SubAgentJobStatus.WAITING
                    job.waiting_for = result.message
                    job.pause_requested = False
                    event_type = "subagent.waiting"
                    message = result.message
                    if session is not None:
                        session.status = "waiting"
                    job.completed_at = None
                else:
                    job.status = SubAgentJobStatus.COMPLETED
                    job.result = _clip(_redact(result), self.max_result_chars)
                    event_type = "subagent.completed"
                    message = job.result
                    if session is not None:
                        session.status = "completed"
                    job.completed_at = _now()
                if session is not None:
                    session.updated_at = _now()
                job.updated_at = _now()
                job.state_version += 1
                if job.status in self.TERMINAL_JOB_STATUSES or job.status == SubAgentJobStatus.WAITING:
                    if job.handoff_context is not None:
                        if job.status == SubAgentJobStatus.WAITING:
                            job.handoff_context = job.handoff_context.with_state(
                                "waiting", waiting_for=job.waiting_for
                            )
                        else:
                            job.handoff_context = job.handoff_context.with_state(
                                job.status.value, result=job.result, error=job.error
                            )
                    self._queue_outbox_locked(job, event_type, message, EventPriority.NORMAL)
                pending_save = self._prepare_save_locked()
            self._persist_snapshot(pending_save)
            self._drain_outbox()
        except Exception as exc:
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None:
                    return
                if job.status in self.TERMINAL_JOB_STATUSES:
                    return
                job.status = SubAgentJobStatus.FAILED
                job.error = _redact(f"{type(exc).__name__}: {exc}")
                job.completed_at = _now()
                job.updated_at = _now()
                job.state_version += 1
                session = self._sessions.get(job.session_id)
                if session is not None:
                    session.status = "failed"
                    session.updated_at = _now()
                if job.handoff_context is not None:
                    job.handoff_context = job.handoff_context.with_state("failed", error=job.error)
                self._queue_outbox_locked(job, "subagent.failed", job.error, EventPriority.NORMAL)
                pending_save = self._prepare_save_locked()
            self._persist_snapshot(pending_save)
            self._drain_outbox()

    def _allowed_tool_definitions(self, agent: SubAgent) -> list[dict[str, Any]]:
        # Keep the operator ceiling enforced at the execution boundary too.
        # Create/update/load already constrain persisted state, but this makes
        # an accidental in-memory mutation unable to widen a running worker.
        allowed = set(agent.allowed_tools) & self._tool_capability_ceiling
        definitions = [
            definition
            for definition in self.llm_client.tool_definitions()
            if (
                definition.get("function", {}).get("name") in allowed
                and (
                    self.tool_policy.rule_for(
                        str(definition.get("function", {}).get("name", ""))
                    ).classification
                    is not ToolClassification.PRIVILEGED
                    or not self.tool_policy.approvals_enabled
                    or self.tool_authorizer is not None
                    or self.tool_approval_broker is not None
                )
            )
        ]
        definitions.append(
            {
                "type": "function",
                "function": {
                    "name": "wait_for_input",
                    "description": "Met ce job en attente lorsqu'une information d'Orion est nécessaire. Le worker libère alors son exécution.",
                    "parameters": {
                        "type": "object",
                        "properties": {"question": {"type": "string"}},
                        "required": ["question"],
                        "additionalProperties": False,
                    },
                },
            }
        )
        return definitions

    @staticmethod
    def _arguments_digest(arguments: Mapping[str, Any]) -> str:
        encoded = json.dumps(
            dict(arguments),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _action_target(arguments: Mapping[str, Any]) -> str | None:
        """Match AgentRuntime's compact target used for action deduplication."""
        for key in (
            "to",
            "recipient",
            "recipients",
            "target",
            "task_id",
            "schedule_id",
            "agent_id",
            "job_id",
            "url",
        ):
            if key not in arguments:
                continue
            value = normalize_action_value(arguments[key])
            if isinstance(value, (dict, list)):
                return json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            return str(value)
        return None

    def _tool_effect_policy(self, tool_name: str) -> tuple[bool, float]:
        """Return side-effect classification and the runtime-compatible window."""
        rule = self.tool_policy.rule_for(tool_name)
        is_side_effect = rule.has_side_effects
        dedupe_window = 86400.0
        get_registered = getattr(self.llm_client, "get_registered_tool", None)
        if callable(get_registered):
            try:
                registered = get_registered(tool_name)
            except Exception:
                registered = None
            if registered is not None:
                is_side_effect = is_side_effect or bool(
                    getattr(registered, "side_effect", False)
                )
                try:
                    dedupe_window = max(
                        0.0, float(getattr(registered, "dedupe_window", dedupe_window))
                    )
                except (TypeError, ValueError):
                    dedupe_window = 86400.0
        return is_side_effect, dedupe_window

    @staticmethod
    def _ledger_blocked_result(decision: Any) -> dict[str, Any]:
        existing = getattr(decision, "existing", None)
        existing_status = getattr(existing, "status", None)
        uncertain = (
            getattr(decision, "reason", None) in {"needs_reconciliation", "already_running"}
            or existing_status in {"running", "uncertain"}
        )
        if uncertain:
            result: dict[str, Any] = {
                "executed": False,
                "uncertain": True,
                "needs_reconciliation": True,
                "reason": getattr(decision, "reason", "needs_reconciliation"),
                "action_key": decision.action_key,
            }
            if existing_status is not None:
                result["existing_status"] = existing_status
            return result
        result = {
            "executed": False,
            "duplicate": True,
            "reason": getattr(decision, "reason", "duplicate"),
            "action_key": decision.action_key,
        }
        if existing is not None:
            result["previous_result"] = getattr(existing, "result", None)
        return result

    def _execute_tool_with_action_ledger(
        self,
        call: Mapping[str, Any],
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> Any:
        """Execute one tool with durable at-most-once protection when needed.

        The ledger reservation is committed before external dispatch, and a
        successful result is committed before the surrounding LLM session is
        persisted.  Therefore a crash in that latter window cannot replay the
        side effect: the next identical call observes the existing action key.

        A handler exception after dispatch is conservatively ambiguous.  The
        reservation is deliberately left RUNNING rather than marked FAILED;
        this blocks automatic retry and ActionLedger will move the lease to
        UNCERTAIN for operator reconciliation if it is not finalized.
        """
        is_side_effect, dedupe_window = self._tool_effect_policy(tool_name)
        if not is_side_effect:
            return self._external_call(
                lambda: self.llm_client.execute_tool_call(
                    call, raise_tool_errors=False
                )
            )

        decision = self.action_ledger.reserve(
            tool_name,
            arguments,
            target=self._action_target(arguments),
            dedupe_window=dedupe_window,
        )
        if not decision.allowed:
            return self._tool_message(call, self._ledger_blocked_result(decision))

        owner_id = decision.owner_id
        fence_token = decision.fence_token
        try:
            result = self._external_call(
                lambda: self.llm_client.execute_tool_call(
                    call, raise_tool_errors=True
                )
            )
        except InterruptedError as exc:
            # Shutdown may have raced with an already-dispatched call.  The
            # callback could already have committed its external effect, so
            # move the exact fenced reservation to UNCERTAIN immediately
            # rather than leaving it RUNNING until lease expiry.
            self.action_ledger.mark_uncertain(
                decision.action_key,
                f"post_dispatch_interrupted: {type(exc).__name__}: {exc}",
                owner_id=owner_id,
                fence_token=fence_token,
            )
            raise
        except Exception as exc:
            uncertain = self.action_ledger.mark_uncertain(
                decision.action_key,
                f"post_dispatch_failure: {type(exc).__name__}: {exc}",
                owner_id=owner_id,
                fence_token=fence_token,
            )
            return self._tool_message(
                call,
                {
                    "executed": False,
                    "uncertain": True,
                    "needs_reconciliation": True,
                    "reason": (
                        "post_dispatch_failure"
                        if uncertain is not None and uncertain.status == "uncertain"
                        else "reservation_lost_after_dispatch"
                    ),
                    "action_key": decision.action_key,
                    "error": _clip(_redact(f"{type(exc).__name__}: {exc}"), 1000),
                },
            )

        action_result = result.get("content") if isinstance(result, Mapping) else result
        try:
            completed = self.action_ledger.complete(
                decision.action_key,
                action_result,
                owner_id=owner_id,
                fence_token=fence_token,
            )
        except Exception as exc:
            return self._tool_message(
                call,
                {
                    "executed": True,
                    "uncertain": True,
                    "needs_reconciliation": True,
                    "reason": "ledger_completion_failed",
                    "action_key": decision.action_key,
                    "error": _clip(_redact(f"{type(exc).__name__}: {exc}"), 1000),
                },
            )
        if completed is None or completed.needs_reconciliation or completed.status != "succeeded":
            return self._tool_message(
                call,
                {
                    "executed": True,
                    "uncertain": True,
                    "needs_reconciliation": True,
                    "reason": "reservation_lost_before_completion",
                    "action_key": decision.action_key,
                },
            )
        return result

    def _tool_call_authorization(
        self,
        agent: SubAgent,
        job_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        # Capability ceiling is the immutable outer boundary. Policy approval
        # can never widen it, even if an authorizer callback returns True.
        if (
            tool_name not in agent.allowed_tools
            or tool_name not in self._tool_capability_ceiling
        ):
            return {"allowed": False, "reason": "capability_ceiling"}

        rule = self.tool_policy.rule_for(tool_name)
        approved = not self.tool_policy.approvals_enabled
        if (
            rule.classification is ToolClassification.PRIVILEGED
            and self.tool_policy.approvals_enabled
        ):
            with self._lock:
                current = self._jobs.get(job_id)
                if current is None:
                    return {"allowed": False, "reason": "job_missing"}
                job_snapshot = SubAgentJob.from_dict(current.to_dict())
            agent_snapshot = SubAgent.from_dict(agent.to_dict())
            digest = self._arguments_digest(arguments)
            if self.tool_approval_broker is not None:
                try:
                    broker_result = self.tool_approval_broker(
                        agent_snapshot,
                        job_snapshot,
                        tool_name,
                        digest,
                        dict(arguments),
                    )
                except Exception as exc:
                    return {
                        "allowed": False,
                        "reason": "approval_broker_error",
                        "detail": _redact(f"{type(exc).__name__}: {exc}"),
                    }
                if isinstance(broker_result, Mapping):
                    result = dict(broker_result)
                    result.setdefault("allowed", False)
                    return result
                approved = bool(broker_result)
            elif self.tool_authorizer is not None:
                try:
                    approved = bool(
                        self.tool_authorizer(
                            agent_snapshot,
                            job_snapshot,
                            tool_name,
                            digest,
                        )
                    )
                except Exception:
                    return {"allowed": False, "reason": "authorizer_error"}
            else:
                return {"allowed": False, "reason": "approval_unavailable"}

        decision = self.tool_policy.decide(
            tool_name,
            enabled=True,
            approved=approved,
        )
        return {
            "allowed": decision.allowed,
            "reason": decision.reason,
            "classification": decision.classification.value,
        }

    def _tool_call_allowed(
        self,
        agent: SubAgent,
        job_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> bool:
        """Compatibility wrapper for older tests/integrations."""
        return bool(
            self._tool_call_authorization(agent, job_id, tool_name, arguments).get(
                "allowed"
            )
        )

    def _system_prompt_with_tool_guidance(self, agent: SubAgent) -> str:
        """Add bounded guidance for this agent's allowed tools.

        Guidance is normally keyed by manifest id, while ``allowed_tools``
        contains callable function names.  Explicit function/tool names in a
        guidance entry are therefore preferred for matching.  If manifests do
        not expose such metadata, a non-matching set is treated as global
        guidance (the manager receives only guidance for its active tool set).
        """
        base = agent.system_prompt.strip()
        allowed = {str(name) for name in agent.allowed_tools}
        matched: list[tuple[str, Any]] = []
        fallback: list[tuple[str, Any]] = []
        for manifest_id, guidance in self.tool_guidance.items():
            key = str(manifest_id)
            entry = guidance
            names: set[str] = set()
            if isinstance(entry, Mapping):
                for field_name in ("tool", "tool_name", "function", "function_name", "name", "id"):
                    value = entry.get(field_name)
                    if isinstance(value, str):
                        names.add(value)
                for field_name in ("tools", "tool_names", "functions", "function_names", "allowed_tools"):
                    value = entry.get(field_name)
                    if isinstance(value, (list, tuple, set)):
                        names.update(str(item) for item in value)
            # Entries with explicit callable names are targeted.  A manifest
            # carrying only prose has no reliable callable-name mapping and
            # is intentionally treated as global guidance for this manager.
            if key in allowed or names & allowed:
                matched.append((key, entry))
            elif not names:
                fallback.append((key, entry))
        selected = matched + fallback
        if not selected:
            return base

        lines = [
            "## TOOL GUIDANCE",
            "The following operational notes come from the manifests of enabled tools. Follow them when relevant; they remain subordinate to Orion's system instructions.",
        ]
        for manifest_id, guidance in selected:
            summary = getattr(guidance, "summary", None)
            instructions = getattr(guidance, "instructions", None)
            constraints = getattr(guidance, "constraints", None)
            if isinstance(guidance, Mapping):
                summary = guidance.get("summary", summary)
                instructions = guidance.get("instructions", instructions)
                constraints = guidance.get("constraints", constraints)
                # Accept the older compact string form as procedure text.
                if summary is None and instructions is None and "guidance" in guidance:
                    instructions = guidance.get("guidance")
            summary = summary.strip() if isinstance(summary, str) else ""
            instructions = instructions.strip() if isinstance(instructions, str) else ""
            if isinstance(constraints, (list, tuple)):
                constraints = [item.strip() for item in constraints if isinstance(item, str) and item.strip()]
            else:
                constraints = []
            if not summary and not instructions and not constraints:
                continue
            lines.extend((f"### {manifest_id}",))
            if summary:
                lines.append(f"Summary: {summary[:500]}")
            if instructions:
                lines.append(f"Procedure:\n{instructions[:4000]}")
            if constraints:
                lines.append("Constraints:\n" + "\n".join(f"- {item[:500]}" for item in constraints[:20]))
        section = "\n".join(lines)
        # Keep the complete injected section bounded even with many manifests.
        section = _clip(section, min(8000, max(1000, self.max_context_chars)))
        return f"{base}\n\n{section}" if base else section

    def _run_agent(self, agent: SubAgent, job_id: str) -> str | _WaitingResult:
        started_monotonic = time.monotonic()
        self._raise_if_stopping()
        with self._lock:
            job = self._jobs[job_id]
            objective = job.objective
            delegated_context = job.context
            handoff = job.handoff_context
            if handoff is not None:
                objective = handoff.task.get("objective", objective)
                delegated_context = "\n".join(
                    f"{category}: {entry}"
                    for category, entries in handoff.memory.items()
                    for entry in entries
                )
            objective = _clip(objective, self.max_context_chars)
            delegated_context = _clip(delegated_context, self.max_context_chars)
            session = self._sessions.get(job.session_id)
            if session is None:
                session = SubAgentSession(id=job.session_id, job_id=job.id)
                self._sessions[job.session_id] = session
            messages = [dict(message) for message in session.messages]
            system_prompt = self._system_prompt_with_tool_guidance(agent)
            if not messages:
                messages = [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": (
                            f"Objectif délégué par Orion :\n{objective}\n\n"
                            f"Contexte utile, potentiellement incomplet :\n{delegated_context or '(aucun)'}"
                        ),
                    },
                ]
                pending_session_save = self._save_session_locked(session, messages)
            else:
                # Refresh the initial system message on every run so a
                # changed manifest set is reflected after a restart/resume.
                system_index = next((i for i, item in enumerate(messages) if item.get("role") == "system"), None)
                if system_index is None:
                    messages.insert(0, {"role": "system", "content": system_prompt})
                else:
                    messages[system_index] = {**messages[system_index], "content": system_prompt}
                pending_session_save = self._save_session_locked(session, messages)
        self._persist_snapshot(pending_session_save)
        tools = self._allowed_tool_definitions(agent)
        for turn in range(agent.max_turns):
            if time.monotonic() - started_monotonic >= self.max_runtime_seconds:
                raise TimeoutError("Durée maximale du job de sous-agent atteinte.")
            if self._is_cancel_requested(job_id):
                return "Travail interrompu à la demande d'Orion."
            # The in-memory loop can outgrow the persisted session between
            # turns; compact before every model call so the actual request is
            # bounded as well as the restart state.
            messages = self._bounded_messages(messages)
            usage = getattr(self.llm_client, "usage_context", None)
            handoff_id = handoff.handoff_id if handoff is not None else job.id
            correlation_id = handoff.correlation_id if handoff is not None else None
            stage = handoff.task.get("phase", "delegation") if handoff is not None else "delegation"
            scope = usage(request_id=handoff_id, correlation_id=correlation_id, stage=stage, parent_call_id=job.parent_event_id) if callable(usage) else nullcontext()
            with scope:
                response = self._external_call(
                    lambda: self.llm_client.complete(
                        messages,
                        model=agent.model,
                        tools=tools or None,
                        parallel_tool_calls=True if tools else None,
                    )
                )
            assistant = OpenRouterClient._assistant_message(response)
            messages.append(assistant)
            calls = OpenRouterClient._tool_calls(assistant)
            text = OpenRouterClient.text_from_message(assistant).strip()
            if not calls:
                self._save_session(job_id, messages)
                if self._is_pause_requested(job_id):
                    return _WaitingResult("Pause demandée par Orion.")
                return text or "Le sous-agent a terminé sans produire de résultat exploitable."
            if text:
                # The completed exchange is persisted below, after every tool
                # result has been attached.  Avoid writing the same state once
                # merely to publish progress.
                self._record_progress(job_id, text, persist=False)
            for call in calls:
                self._raise_if_stopping()
                if self._is_cancel_requested(job_id):
                    return "Travail interrompu à la demande d'Orion."
                if self._is_pause_requested(job_id):
                    return _WaitingResult("Pause demandée par Orion.")
                name = str(call.get("function", {}).get("name", ""))
                arguments = self._call_arguments(call)
                if name == "wait_for_input":
                    question = str(arguments.get("question", "J'ai besoin d'une information d'Orion."))
                    result = self._tool_message(call, {"waiting": True, "question": question})
                    messages.append(result)
                    self._save_session(job_id, messages)
                    return _WaitingResult(question)
                authorization = self._tool_call_authorization(
                    agent, job_id, name, arguments
                )
                if authorization.get("pending"):
                    approval_id = str(authorization.get("approval_id") or "").strip()
                    payload = {
                        "executed": False,
                        "approval_required": True,
                        "approval_status": str(
                            authorization.get("approval_status") or "pending"
                        ),
                        "approval_id": approval_id or None,
                        "tool": name,
                        "message": (
                            "Approbation demandée à Orion. Le job reprendra automatiquement "
                            "après la décision."
                        ),
                    }
                    result = self._tool_message(call, payload)
                    messages.append(result)
                    self._record_tool_call(job_id, call)
                    self._save_session(job_id, messages)
                    with self._lock:
                        current = self._jobs.get(job_id)
                        if current is not None:
                            current.pending_approval_id = approval_id or None
                            current.pending_approval_tool = name
                            current.updated_at = _now()
                            pending_save = self._prepare_save_locked()
                        else:
                            pending_save = None
                    if pending_save is not None:
                        self._persist_snapshot(pending_save)
                    return _WaitingResult(
                        f"Approbation Orion requise pour {name}"
                        + (f" ({approval_id})" if approval_id else "")
                    )
                if not authorization.get("allowed"):
                    result = self._tool_message(
                        call,
                        {
                            "error": f"Tool non autorisé pour ce sous-agent : {name}",
                            "reason": authorization.get("reason") or "denied",
                        },
                    )
                else:
                    result = self._execute_tool_with_action_ledger(
                        call,
                        name,
                        arguments,
                    )
                    if not isinstance(result, Mapping):
                        result = {"role": "tool", "tool_call_id": str(call.get("id") or uuid.uuid4().hex), "name": name, "content": result}
                    else:
                        result = dict(result)
                    result["content"] = _clip(result.get("content", ""), self.max_tool_output_chars)
                messages.append(result)
                self._record_tool_call(job_id, call)
            # Persist an assistant tool call together with all of its results.
            # A restart cannot load a malformed partial tool exchange; a crash
            # during execution may retry it, with normal action deduplication.
            self._save_session(job_id, messages)
            if self._is_pause_requested(job_id):
                return _WaitingResult("Pause demandée par Orion.")

        final_instruction = {
            "role": "system",
            "content": "La limite d'étapes est atteinte. Ne lance plus d'outil et fournis maintenant le meilleur résultat exploitable à Orion.",
        }
        # Apply the same bound to the final request.  This instruction is only
        # transient and is deliberately not retained in the session history.
        final_messages = self._bounded_messages([*messages, final_instruction])
        usage = getattr(self.llm_client, "usage_context", None)
        scope = usage(request_id=handoff_id, correlation_id=correlation_id, stage=stage, parent_call_id=job.parent_event_id) if callable(usage) else nullcontext()
        with scope:
            response = self._external_call(
                lambda: self.llm_client.complete(
                    final_messages, model=agent.model, tools=None
                )
            )
        assistant = OpenRouterClient._assistant_message(response)
        self._save_session(job_id, [*messages, assistant], status="completed")
        return OpenRouterClient.text_from_message(assistant).strip() or "Travail partiellement terminé."

    @staticmethod
    def _call_arguments(call: Mapping[str, Any]) -> dict[str, Any]:
        function = call.get("function", {})
        raw = function.get("arguments", {}) if isinstance(function, Mapping) else {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw or "{}")
            except ValueError:
                raw = {}
        return dict(raw) if isinstance(raw, Mapping) else {}

    def _save_session_locked(
        self,
        session: SubAgentSession,
        messages: list[dict[str, Any]],
        *,
        status: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        bounded = self._bounded_messages(messages)
        session.messages = [
            {**message, "content": _clip(_redact(message.get("content", "")), self.max_tool_output_chars)}
            if isinstance(message, Mapping) and "content" in message else dict(message)
            for message in bounded
        ]
        if status is not None:
            session.status = status
        session.updated_at = _now()
        return self._prepare_save_locked()

    @staticmethod
    def _session_usage(messages: list[dict[str, Any]]) -> tuple[int, int]:
        """Return deterministic cumulative char/token estimates for one request."""
        encoded = json.dumps(
            messages,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )
        chars = len(encoded)
        # No tokenizer dependency is required by SubAgentManager.  UTF-8 bytes
        # divided by four is a conservative, deterministic approximation that
        # is sufficient to enforce a second independent cumulative budget.
        tokens = max(1, (len(encoded.encode("utf-8")) + 3) // 4)
        return chars, tokens

    @classmethod
    def _shrink_messages_to_budget(
        cls,
        messages: list[dict[str, Any]],
        *,
        max_chars: int,
        max_tokens: int,
    ) -> list[dict[str, Any]]:
        """Shrink textual leaves while preserving message/tool-call structure."""
        bounded = copy.deepcopy(messages)

        def fits() -> bool:
            chars, tokens = cls._session_usage(bounded)
            return chars <= max_chars and tokens <= max_tokens

        if fits():
            return bounded

        latest_user = max(
            (index for index, item in enumerate(bounded) if item.get("role") == "user"),
            default=-1,
        )
        while not fits():
            candidates: list[tuple[int, int, str, int | None, int]] = []
            for index, message in enumerate(bounded):
                role = str(message.get("role") or "")
                # System instructions and the current/latest user request are
                # the last textual leaves we sacrifice under extreme pressure.
                importance = 3 if role == "system" or index == latest_user else 2 if role == "user" else 1
                content = message.get("content")
                if isinstance(content, str) and content:
                    candidates.append((importance, index, "content", None, len(content)))
                calls = message.get("tool_calls")
                if isinstance(calls, list):
                    for call_index, call in enumerate(calls):
                        if not isinstance(call, Mapping):
                            continue
                        function = call.get("function")
                        if not isinstance(function, Mapping):
                            continue
                        arguments = function.get("arguments")
                        if isinstance(arguments, str) and arguments:
                            candidates.append(
                                (importance, index, "arguments", call_index, len(arguments))
                            )
            if not candidates:
                break
            importance, index, field, call_index, length = min(
                candidates,
                key=lambda item: (item[0], -item[4]),
            )
            del importance
            current_chars, current_tokens = cls._session_usage(bounded)
            excess_chars = max(0, current_chars - max_chars)
            excess_tokens = max(0, current_tokens - max_tokens) * 4
            reduction = max(1, excess_chars, excess_tokens, length // 8)
            target = max(0, length - reduction)
            marker = "…" if target > 0 else ""
            keep = max(0, target - len(marker))
            if field == "content":
                original = str(bounded[index].get("content") or "")
                bounded[index]["content"] = original[:keep].rstrip() + marker
            else:
                assert call_index is not None
                function = bounded[index]["tool_calls"][call_index]["function"]
                original = str(function.get("arguments") or "")
                function["arguments"] = original[:keep].rstrip() + marker
        return bounded

    def _bounded_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Compacte une session sans laisser de tool call orphelin.

        Les anciens messages restent résumés par le couple system/user initial;
        la queue commence toujours sur une frontière user ou assistant textuel.
        Le nombre de messages *et* le volume cumulé caractères/tokens sont
        bornés, tout en gardant une séquence acceptée par les APIs OpenAI
        compatibles après redémarrage.
        """
        limit = self.max_session_messages
        if not messages:
            return []

        # Keep the initial instructions when possible, while excluding their
        # original positions from the selectable tail.
        system_index = next((i for i, item in enumerate(messages) if item.get("role") == "system"), None)
        user_index = next((i for i, item in enumerate(messages) if item.get("role") == "user"), None)
        prefix: list[dict[str, Any]] = []
        if system_index is not None:
            prefix.append(dict(messages[system_index]))
        if user_index is not None and user_index != system_index:
            prefix.append(dict(messages[user_index]))
        skipped_prefix = {index for index in (system_index, user_index) if index is not None}

        # Build atomic history blocks.  An assistant tool request and every
        # result for that request must enter or leave the retained history as a
        # unit; otherwise a restart sends an invalid protocol sequence.
        blocks: list[list[dict[str, Any]]] = []
        index = 0
        while index < len(messages):
            if index in skipped_prefix:
                index += 1
                continue
            item = messages[index]
            role = item.get("role")
            if role == "tool":
                # Results without their assistant request are never useful to
                # the model and are rejected by OpenAI-compatible APIs.
                index += 1
                continue
            if role == "assistant" and item.get("tool_calls"):
                calls = item.get("tool_calls")
                calls = list(calls) if isinstance(calls, list) else []
                call_ids = [str(call.get("id")) for call in calls if isinstance(call, Mapping) and call.get("id")]
                result_messages: list[dict[str, Any]] = []
                next_index = index + 1
                while next_index < len(messages) and messages[next_index].get("role") == "tool":
                    result_messages.append(dict(messages[next_index]))
                    next_index += 1
                result_ids = {str(result.get("tool_call_id")) for result in result_messages}
                if call_ids and all(call_id in result_ids for call_id in call_ids):
                    # Keep only results belonging to this request.  A stray
                    # result in the same run is an orphan and must be dropped.
                    expected = set(call_ids)
                    results = [result for result in result_messages if str(result.get("tool_call_id")) in expected]
                    blocks.append([dict(item), *results])
                index = next_index
                continue
            blocks.append([dict(item)])
            index += 1

        budget = max(0, limit - len(prefix))
        retained_reversed: list[list[dict[str, Any]]] = []
        used = 0
        for block in reversed(blocks):
            block_size = len(block)
            if used + block_size <= budget:
                retained_reversed.append(block)
                used += block_size
                continue
            if not retained_reversed and block_size > budget:
                # A single tool exchange can be larger than the configured
                # limit.  Keeping it intact is safer than producing an invalid
                # request; this is the only intentional overrun.
                retained_reversed.append(block)
            break
        retained_blocks = list(reversed(retained_reversed))
        protected_ids: set[int] = set()
        if blocks:
            # The newest block can be the tool exchange that produced the
            # observation for the next turn, so it is protocol-critical.
            protected_ids.add(id(blocks[-1]))
        latest_user_block = next(
            (
                block
                for block in reversed(blocks)
                if any(item.get("role") == "user" for item in block)
            ),
            None,
        )
        if latest_user_block is not None:
            # On a resumed worker this is Orion's newest answer/input.  Keeping
            # only the latest assistant/tool block while dropping this request
            # makes the continuation semantically detached from its cause.
            protected_ids.add(id(latest_user_block))

        retained_ids = {id(block) for block in retained_blocks}
        for block in blocks:
            if id(block) in protected_ids and id(block) not in retained_ids:
                retained_blocks.append(block)
                retained_ids.add(id(block))
        block_order = {id(block): index for index, block in enumerate(blocks)}
        retained_blocks.sort(key=lambda block: block_order[id(block)])

        # Re-apply the count budget after restoring protected blocks.  Drop old
        # unprotected blocks first; atomic/protected blocks may intentionally
        # exceed a tiny message-count limit rather than corrupt the protocol.
        while sum(len(block) for block in retained_blocks) > budget:
            removable = next(
                (
                    index
                    for index, block in enumerate(retained_blocks)
                    if id(block) not in protected_ids
                ),
                None,
            )
            if removable is None:
                break
            retained_blocks.pop(removable)

        # Drop oldest unprotected history blocks until the cumulative budget
        # fits.  The latest user request and newest protocol block are retained
        # and, if necessary, text-shrunk below without breaking atomicity.
        while True:
            result = prefix + [item for block in retained_blocks for item in block]
            chars, tokens = self._session_usage(result)
            if chars <= self.max_session_chars and tokens <= self.max_session_tokens:
                return result
            removable = next(
                (
                    index
                    for index, block in enumerate(retained_blocks)
                    if id(block) not in protected_ids
                ),
                None,
            )
            if removable is None:
                break
            retained_blocks.pop(removable)

        result = prefix + [item for block in retained_blocks for item in block]

        # Extreme case: system/current request or the newest atomic tool block
        # alone exceeds the configured budget.  Shrink textual leaves without
        # deleting protocol structure or orphaning tool results.
        return self._shrink_messages_to_budget(
            result,
            max_chars=self.max_session_chars,
            max_tokens=self.max_session_tokens,
        )

    def _save_session(
        self,
        job_id: str,
        messages: list[dict[str, Any]],
        *,
        status: str | None = None,
    ) -> None:
        pending_save = None
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            session = self._sessions.get(job.session_id)
            if session is not None:
                pending_save = self._save_session_locked(session, messages, status=status)
        if pending_save is not None:
            self._persist_snapshot(pending_save)

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

    def _is_pause_requested(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            return job is None or job.pause_requested

    def _record_progress(self, job_id: str, message: str, *, persist: bool = True) -> None:
        compact = _clip(_redact(message), 1200)
        pending_save = None
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.progress.append(compact)
            job.progress = job.progress[-20:]
            job.updated_at = _now()
            if persist:
                pending_save = self._prepare_save_locked()
            snapshot = SubAgentJob.from_dict(job.to_dict())
        if pending_save is not None:
            self._persist_snapshot(pending_save)
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
                    "arguments": _clip(_redact(function.get("arguments", {})), 2000),
                    "at": _now(),
                }
            )
            job.tool_calls = job.tool_calls[-50:]
            job.updated_at = _now()
            # The enclosing turn persists the job together with the complete
            # assistant/tool exchange, avoiding one state rewrite per call.

    def _publish(self, job: SubAgentJob, event_type: str, message: str | None, priority: EventPriority) -> None:
        payload = self._event_payload(job, event_type, message)
        metadata = {
            **_redact_structure(job.route_metadata),
            "internal_event": True,
            "subagent_job_id": job.id,
            "subagent_id": job.agent_id,
            "parent_event_id": job.parent_event_id,
            "handoff_id": job.handoff_context.handoff_id if job.handoff_context else None,
            "correlation_id": job.handoff_context.correlation_id if job.handoff_context else None,
            "state_version": job.state_version,
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
    "SubAgentSession",
    "SubAgentStatus",
]
