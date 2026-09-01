"""Runtime minimal d'un agent piloté par des événements.

Ce module ne contient volontairement aucune logique LLM. Il orchestre le
réveil de l'agent, le chargement de son état courant et les futures phases de
la boucle d'exécution.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from queue import Empty
from typing import TYPE_CHECKING, Any, Protocol

from action_ledger import ActionLedger, normalize_action_value
from channels import AgentOutput
from context_assembler import ContextAssembler, ContextComponent
from event_handler import Event, EventHandler, EventQueue
from openrouter_client import OpenRouterClient
from prompt_context import (
    ConversationJournal,
    MemoryMaintenance,
    PromptComposer,
    PromptContextStore,
)
from reflection_engine import ReflectionEngine
from tasks import (
    ActionStatus,
    InMemoryTaskStore,
    RunStatus,
    Task,
    TaskStore,
    TaskStatus,
    WaitCondition,
)

if TYPE_CHECKING:
    from scheduler import Schedule, Scheduler
    from subagents import SubAgentManager


class RuntimeState(str, Enum):
    """États du cycle principal et du futur cycle de tâche."""

    SLEEP = "sleep"
    # Alias de compatibilité avec la première version du runtime.
    DORMANT = "sleep"
    EVENT = "event"
    WAKE = "wake"
    PREEMPT = "preempt"
    MATCH_WAITING_TASK = "match_waiting_task"
    FIND_CREATE_TASK = "find_create_task"
    LOAD_TASK_STATE = "load_task_state"
    RUN = "run"
    DECISION = "decision"
    ACTION = "action"
    OBSERVATION = "observation"
    ANSWER = "answer"
    WAIT = "wait"
    UPDATE_TASK = "update_task"
    OBJECTIVE_ACHIEVED = "objective_achieved"
    COMPLETE = "complete"
    CONTINUE = "continue"


class RunPhase(str, Enum):
    """Sous-phases prévues à l'intérieur d'un ``RUN``."""

    PRE_REFLECTION = "pre_reflection"
    DECISION = "decision"
    REFLECTION = "reflection"
    TOOL = "tool"
    SMALL_OUTPUT = "small_output"
    ANSWER = "answer"
    NEW_TURN = "new_turn"


class StateStore(Protocol):
    """Contrat minimal pour persister l'état de l'agent."""

    def load(self) -> Mapping[str, Any]:
        ...

    def save(self, state: Mapping[str, Any]) -> None:
        ...


@dataclass
class InMemoryStateStore:
    """State store par défaut, utile pour le développement initial."""

    state: dict[str, Any] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def load(self) -> Mapping[str, Any]:
        with self._lock:
            return dict(self.state)

    def save(self, state: Mapping[str, Any]) -> None:
        with self._lock:
            self.state = dict(state)


@dataclass(frozen=True)
class WakeContext:
    """Contexte conservé après le réveil, avant l'implémentation de la boucle."""

    event_id: str
    event_type: str
    source: str | None
    payload: dict[str, Any]
    metadata: dict[str, Any]
    loaded_state: dict[str, Any]
    task_id: int | None
    created_at: Any


@dataclass
class RunContext:
    """Contexte de la future boucle d'exécution.

    Il est volontairement passif pour le moment : aucun modèle ni outil n'est
    appelé. ``phase`` indique le point d'entrée de la prochaine implémentation.
    """

    event: Event
    task: Task | None
    run_id: str | None
    loaded_state: dict[str, Any]
    phase: RunPhase = RunPhase.DECISION
    turn: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    small_outputs: list[str] = field(default_factory=list)
    answer: str | None = None
    reflection: str | None = None
    reflection_error: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    control: str | None = None
    interrupted: bool = False
    interrupting_event_id: str | None = None


@dataclass
class PreemptedRun:
    """Run sauvegardé pour reprendre après une interruption prioritaire."""

    task_id: int
    run_id: str
    context: RunContext


StateChangeHandler = Callable[[RuntimeState, RuntimeState, Event | None], Any]
RuntimeErrorHandler = Callable[[Event, Exception], Any]
OutputHandler = Callable[[AgentOutput], Any]


class AgentRuntime:
    """Runtime dormant réveillé par les événements de :class:`EventHandler`.

    Le runtime peut être attaché à un ``EventHandler`` avec :meth:`attach`.
    Le cycle principal est ``SLEEP -> EVENT -> WAKE -> RUN -> SLEEP``.
    ``RUN`` expose les sous-phases ``PRE_REFLECTION -> DECISION -> TOOL -> SMALL_OUTPUT ->
    ANSWER`` ou ``NEW_TURN`` pour la future boucle d'exécution.
    """

    _SIDE_EFFECT_RUNTIME_TOOLS = frozenset(
        {
            "create_task",
            "set_plan",
            "update_plan_step",
            "update_task_state",
            "wait_for_event",
            "complete_task",
            "schedule_wakeup",
            "create_subagent",
            "update_subagent",
            "delete_subagent",
            "delegate_to_subagent",
            "cancel_subagent_job",
            "send_to_subagent",
            "pause_subagent_job",
            "resume_subagent_job",
        }
    )

    def __init__(
        self,
        *,
        llm_client: OpenRouterClient | None = None,
        state_store: StateStore | None = None,
        task_store: TaskStore | None = None,
        scheduler: Scheduler | None = None,
        subagent_manager: SubAgentManager | None = None,
        system_prompt: str | None = None,
        max_turns: int = 12,
        wake_queue_size: int = 0,
        action_ledger: ActionLedger | None = None,
        action_ledger_path: str | None = "data/action_ledger.sqlite3",
        dedupe_window: float = 86400.0,
        parallel_tool_calls: bool = False,
        response_max_chars: int = 3000,
        response_max_sentences: int = 8,
        response_concise: bool = True,
        reflection_engine: ReflectionEngine | None = None,
        prompt_composer: PromptComposer | None = None,
        prompt_store: PromptContextStore | None = None,
        conversation_journal: ConversationJournal | None = None,
        history_enabled: bool = True,
        history_limit: int = 20,
        history_max_chars: int = 12000,
        context_assembler: ContextAssembler | None = None,
        task_context_max_chars: int = 12000,
        event_context_max_chars: int = 10000,
        memory_maintenance: MemoryMaintenance | None = None,
        on_state_change: StateChangeHandler | None = None,
        on_error: RuntimeErrorHandler | None = None,
        on_output: OutputHandler | None = None,
    ) -> None:
        if max_turns < 1:
            raise ValueError("max_turns doit être supérieur ou égal à un.")
        if dedupe_window < 0:
            raise ValueError("dedupe_window doit être positif ou nul.")
        if history_limit < 1 or history_max_chars < 1:
            raise ValueError("history_limit et history_max_chars doivent être positifs.")
        if task_context_max_chars < 1 or event_context_max_chars < 1:
            raise ValueError("Les limites de contexte doivent être positives.")
        if response_max_chars < 500 or response_max_sentences < 1:
            raise ValueError("Les limites de réponse sont invalides.")
        self.llm_client = llm_client
        self.state_store = state_store or InMemoryStateStore()
        self.task_store = task_store or InMemoryTaskStore()
        self.scheduler = scheduler
        self.subagent_manager = subagent_manager
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.parallel_tool_calls = bool(parallel_tool_calls)
        self.response_max_chars = int(response_max_chars)
        self.response_max_sentences = int(response_max_sentences)
        self.response_concise = bool(response_concise)
        self.reflection_engine = reflection_engine
        self.wake_queue = EventQueue(maxsize=wake_queue_size)
        self.action_ledger = action_ledger or ActionLedger(action_ledger_path or ":memory:")
        self.dedupe_window = float(dedupe_window)
        self.prompt_store = prompt_store or PromptContextStore()
        self.prompt_composer = prompt_composer or PromptComposer(
            self.prompt_store,
            personality_override=system_prompt,
        )
        self.memory_maintenance = memory_maintenance
        self.conversation_journal = conversation_journal or (
            self.memory_maintenance.journal
            if self.memory_maintenance is not None
            else ConversationJournal()
        )
        self.history_enabled = bool(history_enabled)
        self.history_limit = int(history_limit)
        self.history_max_chars = int(history_max_chars)
        self.context_assembler = context_assembler or ContextAssembler(compactor=llm_client)
        self.task_context_max_chars = int(task_context_max_chars)
        self.event_context_max_chars = int(event_context_max_chars)
        self.on_state_change = on_state_change
        self.on_error = on_error
        self.on_output = on_output

        self._state = RuntimeState.SLEEP
        self._state_lock = threading.RLock()
        self._execution_lock = threading.RLock()
        self._stop_requested = threading.Event()
        self._drain_on_stop = True
        self._thread: threading.Thread | None = None
        self._attached_handler: EventHandler | None = None
        self._attached_event_type: str | None = None
        self._last_event: Event | None = None
        self._current_task: Task | None = None
        self._wake_context: WakeContext | None = None
        self._run_context: RunContext | None = None
        self._preempted_runs: list[PreemptedRun] = []
        self._last_error: Exception | None = None
        self._wake_count = 0

    @property
    def state(self) -> RuntimeState:
        with self._state_lock:
            return self._state

    @property
    def is_dormant(self) -> bool:
        return self.state == RuntimeState.SLEEP

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def wake_count(self) -> int:
        return self._wake_count

    @property
    def pending_events(self) -> int:
        return self.wake_queue.qsize()

    @property
    def last_event(self) -> Event | None:
        return self._last_event

    @property
    def current_task(self) -> Task | None:
        """Dernière tâche chargée par le runtime."""
        if self._current_task is None:
            return None
        return self.task_store.get(self._current_task.id)

    @property
    def wake_context(self) -> WakeContext | None:
        return self._wake_context

    @property
    def run_context(self) -> RunContext | None:
        """Dernier contexte de RUN, prêt pour l'implémentation de la boucle."""
        return self._run_context

    @property
    def last_error(self) -> Exception | None:
        return self._last_error

    @property
    def preempted_runs(self) -> int:
        """Nombre de runs suspendus en attente de reprise."""
        with self._execution_lock:
            return len(self._preempted_runs)

    def _transition(self, new_state: RuntimeState, event: Event | None = None) -> None:
        with self._state_lock:
            old_state = self._state
            self._state = new_state
        if self.on_state_change is not None and old_state != new_state:
            self.on_state_change(old_state, new_state, event)

    def attach(
        self,
        event_handler: EventHandler,
        event_type: str = "*",
    ) -> AgentRuntime:
        """Connecte le runtime à un routeur d'événements."""
        if self._attached_handler is not None:
            self.detach()
        event_handler.register(event_type, self.receive_event)
        self._attached_handler = event_handler
        self._attached_event_type = event_type
        return self

    def detach(self) -> None:
        """Déconnecte le runtime sans supprimer les événements déjà en file."""
        if self._attached_handler is not None and self._attached_event_type is not None:
            self._attached_handler.unregister(
                self._attached_event_type, self.receive_event
            )
        self._attached_handler = None
        self._attached_event_type = None

    def receive_event(self, event: Event) -> None:
        """Callback appelé par ``EventHandler`` pour placer un événement."""
        if not isinstance(event, Event):
            raise TypeError("Le runtime attend une instance de Event.")
        with self._execution_lock:
            self.wake_queue.put(event)
            if self._should_preempt(event):
                self._transition(RuntimeState.PREEMPT, event)
                self._pause_active_run(event)
            self._transition(RuntimeState.EVENT, event)

    def start(self) -> AgentRuntime:
        """Démarre le worker du runtime ; l'état initial reste ``SLEEP``."""
        if self.running:
            return self
        self._stop_requested.clear()
        self._drain_on_stop = True
        self._thread = threading.Thread(
            target=self._run,
            name="agent-runtime",
            daemon=True,
        )
        self._thread.start()
        if self.memory_maintenance is not None:
            self.memory_maintenance.start()
        return self

    def stop(self, *, wait: bool = True, drain: bool = True) -> None:
        """Arrête le worker, avec possibilité de traiter les réveils en attente."""
        self._drain_on_stop = drain
        self._stop_requested.set()
        thread = self._thread
        if thread is not None and wait:
            if drain:
                thread.join()
            else:
                thread.join(timeout=2.0)
        self._thread = None
        if self.memory_maintenance is not None:
            self.memory_maintenance.stop(wait=wait)

    def sleep(self) -> None:
        """Replace l'agent en sommeil, ou en attente d'un événement suivant."""
        next_state = RuntimeState.EVENT if not self.wake_queue.empty() else RuntimeState.SLEEP
        self._transition(next_state, self._last_event)

    def create_task(
        self,
        objective: str,
        *,
        priority: int = 20,
    ) -> Task:
        """Crée une tâche à la décision de l'agent, jamais automatiquement.

        Cette méthode est le point d'entrée prévu pour la future phase
        ``RUN``. L'événement courant est seulement enregistré dans l'historique
        de la tâche ; il ne décide ni de sa création ni de sa mise à jour.
        """
        if self._run_context is not None and self._run_context.task is not None:
            raise RuntimeError("Une tâche est déjà associée au RUN courant.")
        task = self.task_store.create(objective, priority=priority)
        if self._last_event is not None:
            task.add_history("task_selected_by_agent", event_id=self._last_event.id)
        self.task_store.save(task)
        return self.bind_task(task.id)

    def bind_task(self, task_id: int) -> Task:
        """Associe explicitement une tâche existante au ``RUN`` courant."""
        task = self.task_store.get(int(task_id))
        if task is None:
            raise KeyError(f"Tâche inconnue : {task_id}")
        if task.status in {TaskStatus.COMPLETED, TaskStatus.CANCELLED}:
            raise ValueError(f"La tâche {task.id} n'est plus active.")

        self._current_task = task
        if self._run_context is not None and self._run_context.task is None:
            run = task.start_run(self._last_event.id if self._last_event else None)
            self.task_store.save(task)
            self._run_context.task = task
            self._run_context.run_id = run.id
            self._run_context.loaded_state = dict(task.current_state)
        return task

    def pause_current_task(
        self,
        *,
        reason: str = "",
        interrupted_by: Event | None = None,
    ) -> Task:
        """Suspend explicitement le run actif et le place dans la pile."""
        with self._execution_lock:
            if self._current_task is None or self._run_context is None:
                raise RuntimeError("Aucun run de tâche actif.")
            if self._run_context.run_id is None:
                raise RuntimeError("La tâche courante n'a pas encore de run.")
            task = self._current_task
            task.pause(
                run_id=self._run_context.run_id,
                reason=reason,
                interrupted_by=interrupted_by.id if interrupted_by else None,
            )
            self.task_store.save(task)
            self._preempted_runs.append(
                PreemptedRun(
                    task_id=task.id,
                    run_id=self._run_context.run_id,
                    context=self._run_context,
                )
            )
            self._current_task = None
            self._run_context = None
            return task

    def resume_preempted_task(self) -> Task | None:
        """Reprend le dernier run préempté (LIFO)."""
        with self._execution_lock:
            if not self._preempted_runs:
                return None
            paused = self._preempted_runs.pop()
            task = self.task_store.get(paused.task_id)
            if task is None:
                raise KeyError(f"Tâche préemptée introuvable : {paused.task_id}")
            task.resume(run_id=paused.run_id)
            self.task_store.save(task)
            paused.context.task = task
            self._current_task = task
            self._run_context = paused.context
            self._transition(RuntimeState.RUN, paused.context.event)
            return task

    def save_current_task(self, task: Task | None = None) -> Task:
        """Persiste une tâche modifiée par l'agent."""
        target = task or self._current_task
        if target is None:
            raise RuntimeError("Aucune tâche n'est associée au RUN courant.")
        saved = self.task_store.save(target)
        self._current_task = saved
        if self._run_context is not None:
            self._run_context.task = saved
        return saved

    def finish_current_run(
        self,
        *,
        status: RunStatus = RunStatus.COMPLETED,
        error: str | None = None,
    ) -> Task:
        """Termine le run courant ; la future boucle pourra choisir le statut."""
        if self._current_task is None or self._run_context is None:
            raise RuntimeError("Aucun run de tâche n'est associé au runtime.")
        if self._run_context.run_id is None:
            raise RuntimeError("La tâche courante n'a pas encore de run.")
        self._current_task.finish_run(
            self._run_context.run_id,
            status=status,
            error=error,
        )
        if status == RunStatus.CANCELLED:
            self._current_task.status = TaskStatus.CANCELLED
        elif status == RunStatus.FAILED:
            self._current_task.status = TaskStatus.FAILED
        finished_task = self.save_current_task(self._current_task)
        return finished_task

    def complete_current_task(self, *, summary: str | None = None) -> Task:
        """Marque l'objectif courant comme atteint et libère une préemption."""
        if self._current_task is None or self._run_context is None:
            raise RuntimeError("Aucune tâche n'est associée au RUN courant.")
        task = self._current_task
        if summary:
            task.add_history("task_completion_summary", summary=summary)
        task.mark_completed()
        if self._run_context.run_id is not None:
            task.finish_run(self._run_context.run_id, status=RunStatus.COMPLETED)
        self._run_context.control = "complete"
        self.save_current_task(task)
        self.resume_preempted_task()
        return task

    def wait_current_task(
        self,
        *,
        event_type: str | None = None,
        source: str | None = None,
        payload_equals: Mapping[str, Any] | None = None,
        metadata_equals: Mapping[str, Any] | None = None,
        description: str = "",
    ) -> WaitCondition:
        """Met la tâche en pause jusqu'à l'arrivée d'un événement compatible."""
        if self._current_task is None or self._run_context is None:
            raise RuntimeError("Aucune tâche n'est associée au RUN courant.")
        condition = self._current_task.wait_for(
            event_type=event_type,
            source=source,
            payload_equals=dict(payload_equals or {}),
            metadata_equals=dict(metadata_equals or {}),
            description=description,
        )
        if self._run_context.run_id is not None:
            self._current_task.finish_run(
                self._run_context.run_id,
                status=RunStatus.COMPLETED,
            )
        self._run_context.control = "wait"
        self.save_current_task(self._current_task)
        self.resume_preempted_task()
        return condition

    def schedule_current_task(
        self,
        scheduler: Scheduler,
        run_at: datetime,
        *,
        payload: Mapping[str, Any] | None = None,
        priority: int = 20,
        description: str = "",
    ) -> Schedule:
        """Planifie un réveil et met la tâche courante en attente.

        Cette opération est destinée à être appelée par l'agent pendant son
        RUN. Elle regroupe le schedule et la condition d'attente afin que le
        prochain événement puisse reprendre la bonne tâche.
        """
        if self._current_task is None:
            raise RuntimeError("Aucune tâche n'est associée au RUN courant.")
        schedule = scheduler.schedule_at(
            run_at,
            task_id=self._current_task.id,
            payload=payload,
            priority=priority,
        )
        self.wait_current_task(
            event_type="schedule",
            metadata_equals={"schedule_id": schedule.id},
            description=description or f"Attendre le schedule {schedule.id}",
        )
        return schedule

    def _runtime_tool_definitions(self) -> list[dict[str, Any]]:
        definitions = [
            {
                "type": "function",
                "function": {
                    "name": "create_task",
                    "description": "Crée un objectif durable uniquement si le travail le justifie.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "objective": {"type": "string"},
                            "priority": {"type": "integer", "minimum": 0},
                        },
                        "required": ["objective"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_task",
                    "description": "Charge une tâche durable par son identifiant.",
                    "parameters": {
                        "type": "object",
                        "properties": {"task_id": {"type": "integer"}},
                        "required": ["task_id"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_tasks",
                    "description": "Liste les tâches durables et leur état de façon compacte, notamment pour vérifier une action ou un rappel antérieur.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "status": {
                                "type": "string",
                                "enum": ["pending", "running", "waiting", "paused", "completed", "failed", "cancelled"],
                            },
                            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                        },
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "bind_task",
                    "description": "Reprend explicitement une tâche existante dans ce RUN.",
                    "parameters": {
                        "type": "object",
                        "properties": {"task_id": {"type": "integer"}},
                        "required": ["task_id"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "set_plan",
                    "description": "Crée ou remplace le plan mutable de la tâche courante.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "steps": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "title": {"type": "string"},
                                        "description": {"type": "string"},
                                    },
                                    "required": ["title"],
                                    "additionalProperties": False,
                                },
                            },
                            "reason": {"type": "string"},
                        },
                        "required": ["steps"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "update_plan_step",
                    "description": "Met à jour une étape du plan courant.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "step_id": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed", "blocked", "skipped"],
                            },
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                            "result": {},
                        },
                        "required": ["step_id"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "update_task_state",
                    "description": "Met à jour l'état courant persistant de la tâche.",
                    "parameters": {
                        "type": "object",
                        "properties": {"patch": {"type": "object"}},
                        "required": ["patch"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "wait_for_event",
                    "description": "Met la tâche en attente sans polling jusqu'à un événement compatible.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "event_type": {"type": "string"},
                            "source": {"type": "string"},
                            "payload_equals": {"type": "object"},
                            "metadata_equals": {"type": "object"},
                            "description": {"type": "string"},
                        },
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "complete_task",
                    "description": "Déclare l'objectif atteint et termine la tâche courante.",
                    "parameters": {
                        "type": "object",
                        "properties": {"summary": {"type": "string"}},
                        "additionalProperties": False,
                    },
                },
            },
        ]
        if self.scheduler is not None:
            definitions.append(
                {
                    "type": "function",
                    "function": {
                        "name": "schedule_wakeup",
                        "description": "Planifie un réveil futur et met la tâche en attente.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "run_at": {"type": "string", "description": "Date ISO 8601 avec fuseau horaire."},
                                "payload": {"type": "object"},
                                "priority": {"type": "integer", "minimum": 0},
                                "description": {"type": "string"},
                                "channel": {"type": "string", "description": "Channel de livraison du rappel, par exemple telegram."},
                                "recipient": {"type": "string", "description": "Destinataire ou identifiant de conversation sur le channel cible."},
                            },
                            "required": ["run_at"],
                            "additionalProperties": False,
                        },
                    },
                }
            )
        if self.subagent_manager is not None:
            definitions.extend(self._subagent_tool_definitions())
        return definitions

    @staticmethod
    def _subagent_tool_definitions() -> list[dict[str, Any]]:
        string_array = {"type": "array", "items": {"type": "string"}}
        return [
            {
                "type": "function",
                "function": {
                    "name": "create_subagent",
                    "description": "Crée un worker IA persistant et spécialisé, indépendant d'Orion.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                            "model": {"type": "string"},
                            "system_prompt": {"type": "string"},
                            "allowed_tools": string_array,
                            "capabilities": string_array,
                            "max_turns": {"type": "integer", "minimum": 1, "maximum": 30},
                        },
                        "required": ["name", "description"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "update_subagent",
                    "description": "Modifie la spécialité, le modèle, les tools ou l'état d'un sous-agent.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "agent_id": {"type": "string"},
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                            "model": {"type": "string"},
                            "system_prompt": {"type": "string"},
                            "allowed_tools": string_array,
                            "capabilities": string_array,
                            "max_turns": {"type": "integer", "minimum": 1, "maximum": 30},
                            "status": {"type": "string", "enum": ["active", "disabled"]},
                        },
                        "required": ["agent_id"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "delete_subagent",
                    "description": "Supprime un sous-agent et annule par défaut ses jobs non terminés.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "agent_id": {"type": "string"},
                            "cancel_jobs": {"type": "boolean"},
                        },
                        "required": ["agent_id"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_subagent",
                    "description": "Consulte la configuration d'un sous-agent.",
                    "parameters": {
                        "type": "object",
                        "properties": {"agent_id": {"type": "string"}},
                        "required": ["agent_id"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_subagents",
                    "description": "Liste les workers disponibles avant de choisir où déléguer.",
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "delegate_to_subagent",
                    "description": "Délègue un travail asynchrone. Retourne immédiatement un job_id ; Orion reste disponible et recevra le résultat comme événement.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "objective": {"type": "string"},
                            "agent_id": {"type": "string", "description": "Optionnel : sélection automatique si absent."},
                            "context": {"type": "string", "description": "Contexte minimal strictement nécessaire au worker."},
                            "priority": {"type": "integer", "minimum": 0, "maximum": 40},
                        },
                        "required": ["objective"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_subagent_job",
                    "description": "Consulte l'état, les progrès et le résultat d'un job délégué.",
                    "parameters": {
                        "type": "object",
                        "properties": {"job_id": {"type": "string"}},
                        "required": ["job_id"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_subagent_session",
                    "description": "Consulte les derniers messages de la session persistante d'un job.",
                    "parameters": {
                        "type": "object",
                        "properties": {"job_id": {"type": "string"}},
                        "required": ["job_id"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_subagent_jobs",
                    "description": "Liste les délégations récentes, éventuellement filtrées par état.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "status": {"type": "string", "enum": ["queued", "running", "waiting", "completed", "failed", "cancelled"]},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                        },
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "cancel_subagent_job",
                    "description": "Annule un job en attente ou demande l'arrêt coopératif d'un job en cours.",
                    "parameters": {
                        "type": "object",
                        "properties": {"job_id": {"type": "string"}},
                        "required": ["job_id"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "send_to_subagent",
                    "description": "Envoie un message à une session de sous-agent en attente et la relance avec son historique.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "job_id": {"type": "string"},
                            "message": {"type": "string"},
                        },
                        "required": ["job_id", "message"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "pause_subagent_job",
                    "description": "Demande la pause coopérative d'un job actif ou met immédiatement en attente un job en file.",
                    "parameters": {
                        "type": "object",
                        "properties": {"job_id": {"type": "string"}},
                        "required": ["job_id"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "resume_subagent_job",
                    "description": "Reprend un job en attente avec sa session persistante.",
                    "parameters": {
                        "type": "object",
                        "properties": {"job_id": {"type": "string"}},
                        "required": ["job_id"],
                        "additionalProperties": False,
                    },
                },
            },
        ]

    def _tool_definitions(self) -> list[dict[str, Any]]:
        """Combine les tools du runtime et ceux enregistrés sur le client."""
        definitions = self._runtime_tool_definitions()
        names = {
            item.get("function", {}).get("name")
            for item in definitions
            if isinstance(item.get("function"), Mapping)
        }
        if self.llm_client is not None:
            for item in self.llm_client.tool_definitions():
                name = item.get("function", {}).get("name")
                if name not in names:
                    definitions.append(item)
        return definitions

    def _legacy_system_instructions(self) -> str:
        return (self.system_prompt or "Tu es Orion, un agent autonome piloté par événements.") + "\n\n" + (
            "Règles opérationnelles :\n"
            "- Réponds directement sans créer de tâche pour une demande simple et éphémère.\n"
            "- Crée une tâche pour un objectif durable, complexe ou à poursuivre plus tard.\n"
            "- Un plan est mutable : adapte-le selon les observations, sans le traiter comme un script rigide.\n"
            "- Utilise wait_for_event ou schedule_wakeup au lieu de faire du polling.\n"
            "- Utilise complete_task uniquement lorsque l'objectif est réellement atteint.\n"
            "- Les tools de tâche sont tes capacités de pilotage ; utilise-les explicitement quand nécessaire.\n"
            "- Délègue aux sous-agents les recherches, explorations et travaux moyens ou longs qui peuvent avancer indépendamment. Garde Orion pour le dialogue, la coordination et les décisions importantes.\n"
            "- Un job de sous-agent est asynchrone : confirme sa délégation puis reste disponible. Son progrès et son résultat reviendront comme événements. Transmets seulement le contexte minimal nécessaire.\n"
            "- Un sous-agent peut appeler wait_for_input lorsqu'il lui manque une information. Il passe alors WAITING et libère son worker ; utilise send_to_subagent pour lui répondre et reprendre sa session.\n"
            "- Crée ou modifie un sous-agent lorsqu'aucun worker existant n'a la spécialité, le modèle ou les tools appropriés.\n"
            "- Si une tâche durable dépend du résultat délégué, appelle wait_for_event sur subagent.completed avec payload_equals.job_id, puis dors au lieu de consulter le job en boucle.\n"
            "- Ne révèle pas tes raisonnements internes détaillés ; donne seulement les sorties utiles."
        )

    def _system_instructions(self) -> str:
        runtime_instructions = (
            "- Réponds directement sans créer de tâche pour une demande simple et éphémère.\n"
            "- Crée une tâche pour un objectif durable, complexe ou à poursuivre plus tard.\n"
            "- Un plan est mutable : adapte-le selon les observations, sans le traiter comme un script rigide.\n"
            "- Utilise wait_for_event ou schedule_wakeup au lieu de faire du polling.\n"
            "- Utilise complete_task uniquement lorsque l'objectif est réellement atteint.\n"
            "- Les tools de tâche sont tes capacités de pilotage ; utilise-les explicitement quand nécessaire.\n"
            "- Pour une action à effet de bord, respecte les résultats duplicate et potential_duplicate.\n"
            "- Si plusieurs tools sont nécessaires, après chaque observation explique brièvement et naturellement ce que tu as appris et ce que tu fais ensuite ; évite les formules répétitives.\n"
            "- Délègue aux sous-agents les recherches, explorations et travaux moyens ou longs qui peuvent avancer indépendamment. Garde Orion pour le dialogue, la coordination et les décisions importantes.\n"
            "- Une délégation est asynchrone : confirme-la puis reste disponible. Le job_id, les progrès et le résultat reviendront comme événements.\n"
            "- Si un sous-agent est en WAITING, utilise send_to_subagent pour lui transmettre une information et reprendre sa session ; n'interroge pas son état en boucle.\n"
            "- Si une tâche durable dépend d'un job, attends son événement subagent.completed avec wait_for_event.\n"
            "- Ne révèle pas tes raisonnements internes détaillés ; donne seulement les sorties utiles."
        )
        if self.response_concise:
            runtime_instructions += (
                f"\n- Réponds comme un humain concis : va droit au but, généralement en quelques phrases ou quelques puces, sans répéter la demande ni ajouter de préambule. La réponse finale doit rester sous environ {self.response_max_chars} caractères et {self.response_max_sentences} phrases, sauf nécessité réelle."
                "\n- Pour une recherche web, donne d'abord une synthèse courte et quelques sources pertinentes ; ne transforme pas automatiquement les résultats en rapport exhaustif."
            )
        runtime_instructions += (
            "\n- Le router de channel envoie automatiquement ta réponse finale vers le channel et le destinataire de l'événement ; ne cherche pas un tool send_telegram.\n"
            "- Pour un rappel futur, utilise schedule_wakeup. Le runtime conserve automatiquement le channel et le destinataire courants ; au réveil, produis le message à délivrer.\n"
            "- Pour vérifier une action ou un rappel antérieur, consulte list_tasks avant de répondre ; n'affirme jamais qu'une action a été faite sans trace persistante.\n"
            "- Les événements et l'historique contiennent leur date et leur heure ; utilise ces horodatages pour interpréter les délais et répondre précisément."
        )
        return self.prompt_composer.compose(runtime_instructions=runtime_instructions)

    def _initial_run_messages(
        self,
        context: RunContext,
        *,
        reflection: str | None = None,
    ) -> list[dict[str, Any]]:
        task_payload = context.task.to_dict() if context.task is not None else None
        event_payload = {
            "id": context.event.id,
            "type": context.event.type,
            "source": context.event.source,
            "priority": context.event.priority,
            "created_at": context.event.created_at.isoformat(),
            "local_time": context.event.created_at.astimezone().isoformat(),
            "payload": context.event.payload,
            "metadata": context.event.metadata,
        }
        components = [
            ContextComponent(
                "task",
                task_payload,
                max_chars=self.task_context_max_chars,
                priority=90,
            ),
            ContextComponent(
                "event",
                event_payload,
                max_chars=self.event_context_max_chars,
                priority=100,
            ),
        ]
        has_history = False
        if self.history_enabled:
            history = self.conversation_journal.recent_messages(
                conversation_id=self._conversation_id(context.event),
                limit=self.history_limit,
            )
            has_history = bool(history)
            components.append(
                ContextComponent(
                    "history",
                    history,
                    max_chars=self.history_max_chars,
                    priority=60,
                )
            )
        assembled = self.context_assembler.assemble(components)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_instructions()},
            {
                "role": "system",
                "content": "Contexte tâche courant (null signifie qu'aucune tâche n'est encore liée) :\n"
                + assembled["task"],
            },
        ]
        if reflection:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Réflexion préparatoire interne. Elle contient des hypothèses, "
                        "pas des faits certains. Utilise-la comme aide pour la décision, "
                        "ne la révèle pas et vérifie-la avec le contexte disponible :\n"
                        + reflection
                    ),
                }
            )
        if has_history:
            messages.append(
                {
                    "role": "system",
                    "content": "Historique unifié récent (chaque message indique sa source et son channel) :\n"
                    + assembled["history"],
                }
            )
        messages.append(
            {
                "role": "user",
                "content": "Événement reçu :\n"
                + assembled["event"],
            }
        )
        return messages

    def _run_pre_reflection(self, context: RunContext) -> str | None:
        """Produit la réflexion interne avant d'initialiser le contexte principal."""
        if self.reflection_engine is None:
            return None
        context.phase = RunPhase.PRE_REFLECTION
        try:
            history = ()
            if self.history_enabled:
                history = self.conversation_journal.recent_messages(
                    conversation_id=self._conversation_id(context.event),
                    limit=min(self.history_limit, 6),
                )
            reflection = self.reflection_engine.reflect(context, history=history)
            context.reflection = reflection or None
            return context.reflection
        except Exception as exc:
            # Une panne du modèle de réflexion ne doit pas empêcher le run
            # principal de répondre ou d'exécuter une action.
            context.reflection_error = f"{type(exc).__name__}: {exc}"
            context.reflection = None
            return None

    @staticmethod
    def _conversation_id(event: Event) -> str:
        """Identifiant de contexte ; ``default`` fusionne les channels."""
        return str(
            event.metadata.get("conversation_id")
            or event.metadata.get("user_id")
            or "default"
        )

    @staticmethod
    def _tool_arguments(call: Mapping[str, Any]) -> dict[str, Any]:
        function = call.get("function", {})
        if not isinstance(function, Mapping):
            function = {}
        raw = function.get("arguments", call.get("arguments", {}))
        if isinstance(raw, str):
            raw = json.loads(raw or "{}")
        if not isinstance(raw, Mapping):
            raise ValueError("Les arguments du tool doivent être un objet JSON.")
        return dict(raw)

    @staticmethod
    def _tool_name(call: Mapping[str, Any]) -> str:
        function = call.get("function", {})
        if not isinstance(function, Mapping):
            function = {}
        return str(function.get("name") or call.get("name") or "")

    @staticmethod
    def _tool_message(call: Mapping[str, Any], value: Any) -> dict[str, Any]:
        function = call.get("function", {})
        if not isinstance(function, Mapping):
            function = {}
        name = str(function.get("name") or call.get("name") or "unknown_tool")
        tool_id = str(call.get("id") or f"call_{uuid.uuid4().hex}")
        content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        return {"role": "tool", "tool_call_id": tool_id, "name": name, "content": content}

    def _fallback_tool_output(self, context: RunContext) -> str | None:
        """Produit un accusé de réception quand un tool termine le RUN."""
        if context.control == "complete":
            return "C'est fait."
        if context.control != "wait":
            if context.small_outputs:
                return context.small_outputs[-1]
            return None
        for call in reversed(context.tool_calls):
            try:
                name = str(call.get("function", {}).get("name") or call.get("name") or "")
                arguments = self._tool_arguments(call)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if name == "schedule_wakeup":
                run_at = arguments.get("run_at")
                if run_at:
                    return f"Rappel programmé pour le {run_at}."
                return "Rappel programmé."
            if name == "wait_for_event":
                description = arguments.get("description")
                if description:
                    return f"C'est noté. J'attends : {description}."
                return "C'est noté. J'attends l'événement attendu."
        return "C'est noté. Orion attend l'événement prévu."

    def _emit_output(self, context: RunContext, content: str, *, intermediate: bool = False) -> None:
        """Envoie immédiatement une sortie vers le channel de l'événement."""
        if self.on_output is None or not content.strip():
            return
        output_limit = min(self.response_max_chars, 700) if intermediate else self.response_max_chars
        content = self._limit_output(content.strip(), output_limit)
        event = context.event
        output_channel = event.metadata.get("channel") or event.payload.get("_orion_channel")
        output_recipient = event.metadata.get("reply_to") or event.payload.get("_orion_reply_to")
        output_metadata = dict(event.metadata)
        output_metadata.setdefault("timestamp", datetime.now().astimezone().isoformat())
        if intermediate:
            output_metadata["intermediate"] = True
            output_metadata["phase"] = context.phase.value
        self.on_output(
            AgentOutput(
                content=content,
                channel=output_channel,
                recipient=output_recipient,
                event_id=event.id,
                task_id=context.task.id if context.task is not None else None,
                metadata=output_metadata,
            )
        )

    @staticmethod
    def _limit_output(content: str, max_chars: int) -> str:
        """Évite qu'une sortie exceptionnelle ne déborde les channels."""
        if len(content) <= max_chars:
            return content
        candidate = content[:max_chars - 1]
        boundary = max(
            candidate.rfind("\n"),
            candidate.rfind(". "),
            candidate.rfind("! "),
            candidate.rfind("? "),
        )
        if boundary >= max_chars // 2:
            candidate = candidate[:boundary + 1]
        return candidate.rstrip() + "…"

    def _finalize_after_control(self, context: RunContext) -> None:
        """Demande une réponse finale après un tool qui contrôle le cycle.

        ``wait_for_event``, ``schedule_wakeup`` et ``complete_task`` modifient
        l'état du runtime, mais cela ne doit pas empêcher le modèle de
        répondre à l'utilisateur. Cette dernière passe est textuelle : aucun
        nouveau tool ne peut être déclenché après le contrôle.
        """
        if self.llm_client is None or context.interrupted:
            return
        try:
            response = self.llm_client.complete(context.messages, tools=None)
        except Exception:
            # Le contrôle du runtime a déjà été exécuté. Une erreur sur la
            # reformulation finale ne doit ni annuler l'action ni faire passer
            # la tâche en FAILED ; le message de secours sera utilisé.
            return
        assistant = OpenRouterClient._assistant_message(response)
        context.messages.append(assistant)
        answer = OpenRouterClient.text_from_message(assistant).strip()
        if answer:
            context.answer = answer

    def _execute_runtime_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        context = self._run_context
        if context is None:
            raise RuntimeError("Aucun RUN actif.")

        if name == "create_task":
            self._transition(RuntimeState.FIND_CREATE_TASK, context.event)
            task = self.create_task(
                arguments["objective"],
                priority=int(arguments.get("priority", 20)),
            )
            return self._compact_task(task)
        if name == "get_task":
            self._transition(RuntimeState.LOAD_TASK_STATE, context.event)
            task = self.task_store.get(int(arguments["task_id"]))
            return self._compact_task(task) if task is not None else {"task": None}
        if name == "list_tasks":
            self._transition(RuntimeState.LOAD_TASK_STATE, context.event)
            status_value = arguments.get("status")
            status = TaskStatus(status_value) if status_value else None
            limit = min(max(int(arguments.get("limit", 10)), 1), 20)
            tasks = self.task_store.list(status=status)
            return {"tasks": [self._compact_task(task) for task in tasks[-limit:]]}
        if name == "bind_task":
            self._transition(RuntimeState.LOAD_TASK_STATE, context.event)
            return self._compact_task(self.bind_task(int(arguments["task_id"])))
        if name == "set_plan":
            if context.task is None:
                raise RuntimeError("Crée ou lie une tâche avant de définir son plan.")
            self._transition(RuntimeState.UPDATE_TASK, context.event)
            context.task.set_plan(arguments["steps"], reason=arguments.get("reason"))
            return self._compact_task(self.save_current_task(context.task))
        if name == "update_plan_step":
            if context.task is None:
                raise RuntimeError("Aucune tâche courante.")
            self._transition(RuntimeState.UPDATE_TASK, context.event)
            changes = {key: value for key, value in arguments.items() if key != "step_id"}
            context.task.update_plan_step(arguments["step_id"], **changes)
            return self._compact_task(self.save_current_task(context.task))
        if name == "update_task_state":
            if context.task is None:
                raise RuntimeError("Aucune tâche courante.")
            self._transition(RuntimeState.UPDATE_TASK, context.event)
            context.task.current_state.update(dict(arguments["patch"]))
            context.task.add_history("current_state_updated")
            return self._compact_task(self.save_current_task(context.task))
        if name == "wait_for_event":
            self._transition(RuntimeState.UPDATE_TASK, context.event)
            condition = self.wait_current_task(
                event_type=arguments.get("event_type"),
                source=arguments.get("source"),
                payload_equals=arguments.get("payload_equals"),
                metadata_equals=arguments.get("metadata_equals"),
                description=arguments.get("description", ""),
            )
            return {"waiting": True, "condition": condition.to_dict()}
        if name == "complete_task":
            self._transition(RuntimeState.UPDATE_TASK, context.event)
            return self._compact_task(self.complete_current_task(summary=arguments.get("summary")))
        if name == "schedule_wakeup":
            if self.scheduler is None:
                raise RuntimeError("Aucun scheduler n'est configuré.")
            self._transition(RuntimeState.UPDATE_TASK, context.event)
            run_at = datetime.fromisoformat(arguments["run_at"])
            payload = dict(arguments.get("payload") or {})

            # Un schedule doit conserver le contexte de livraison. Pour un
            # rappel demandé depuis Telegram, le modèle n'a donc pas besoin
            # de connaître ou de recopier le chat_id manuellement.
            current_channel = context.event.metadata.get("channel") or payload.get("_orion_channel")
            current_recipient = (
                context.event.metadata.get("reply_to")
                or context.event.payload.get("_orion_reply_to")
                or context.event.payload.get("chat_id")
            )
            if arguments.get("channel"):
                payload["_orion_channel"] = arguments["channel"]
            elif current_channel:
                payload["_orion_channel"] = current_channel
            if arguments.get("recipient"):
                payload["_orion_reply_to"] = arguments["recipient"]
            elif current_recipient:
                payload["_orion_reply_to"] = current_recipient
            schedule = self.schedule_current_task(
                self.scheduler,
                run_at,
                payload=payload,
                priority=int(arguments.get("priority", 20)),
                description=arguments.get("description", ""),
            )
            return {"scheduled": True, "schedule": schedule.to_dict()}
        if self.subagent_manager is not None:
            if name == "create_subagent":
                agent = self.subagent_manager.create_agent(
                    arguments["name"],
                    arguments["description"],
                    model=arguments.get("model"),
                    system_prompt=arguments.get("system_prompt"),
                    allowed_tools=arguments.get("allowed_tools"),
                    capabilities=arguments.get("capabilities"),
                    max_turns=arguments.get("max_turns"),
                )
                return self._compact_subagent(agent)
            if name == "update_subagent":
                changes = {key: value for key, value in arguments.items() if key != "agent_id"}
                return self._compact_subagent(
                    self.subagent_manager.update_agent(arguments["agent_id"], **changes)
                )
            if name == "delete_subagent":
                return self.subagent_manager.delete_agent(
                    arguments["agent_id"],
                    cancel_jobs=bool(arguments.get("cancel_jobs", True)),
                )
            if name == "get_subagent":
                agent = self.subagent_manager.get_agent(arguments["agent_id"])
                return self._compact_subagent(agent) if agent else {"subagent": None}
            if name == "list_subagents":
                return {"subagents": [self._compact_subagent(item) for item in self.subagent_manager.list_agents()]}
            if name == "delegate_to_subagent":
                route_keys = {"channel", "reply_to", "conversation_id", "user_id"}
                route_metadata = {
                    key: value for key, value in context.event.metadata.items() if key in route_keys
                }
                # Les événements issus du scheduler peuvent conserver le
                # routage dans le payload plutôt que dans les métadonnées.
                # Dans les deux cas, le résultat doit revenir au channel et
                # au destinataire qui ont lancé la délégation.
                if "channel" not in route_metadata:
                    channel = context.event.payload.get("_orion_channel")
                    if channel:
                        route_metadata["channel"] = channel
                if "reply_to" not in route_metadata:
                    recipient = context.event.payload.get("_orion_reply_to")
                    if recipient:
                        route_metadata["reply_to"] = recipient
                job = self.subagent_manager.submit(
                    arguments["objective"],
                    agent_id=arguments.get("agent_id"),
                    context=arguments.get("context", ""),
                    priority=int(arguments.get("priority", context.event.priority)),
                    parent_task_id=context.task.id if context.task else None,
                    parent_event_id=context.event.id,
                    route_metadata=route_metadata,
                )
                return self._compact_subagent_job(job)
            if name == "get_subagent_job":
                job = self.subagent_manager.get_job(arguments["job_id"])
                return self._compact_subagent_job(job) if job else {"job": None}
            if name == "get_subagent_session":
                job = self.subagent_manager.get_job(arguments["job_id"])
                if job is None:
                    return {"session": None}
                session = self.subagent_manager.get_session(job.session_id)
                if session is None:
                    return {"session": None}
                return {
                    "session_id": session.id,
                    "job_id": session.job_id,
                    "status": session.status,
                    "messages": ContextAssembler.compact_value(session.messages[-10:], max_chars=10000),
                    "updated_at": session.updated_at,
                }
            if name == "list_subagent_jobs":
                jobs = self.subagent_manager.list_jobs(
                    status=arguments.get("status"),
                    limit=int(arguments.get("limit", 20)),
                )
                return {"jobs": [self._compact_subagent_job(item) for item in jobs]}
            if name == "cancel_subagent_job":
                return self._compact_subagent_job(
                    self.subagent_manager.cancel_job(arguments["job_id"])
                )
            if name == "send_to_subagent":
                return self._compact_subagent_job(
                    self.subagent_manager.send_message(
                        arguments["job_id"], arguments["message"]
                    )
                )
            if name == "pause_subagent_job":
                return self._compact_subagent_job(
                    self.subagent_manager.pause_job(arguments["job_id"])
                )
            if name == "resume_subagent_job":
                return self._compact_subagent_job(
                    self.subagent_manager.resume_job(arguments["job_id"])
                )
        raise KeyError(name)

    @staticmethod
    def _compact_subagent(agent: Any) -> dict[str, Any]:
        return {
            "kind": "subagent",
            "id": agent.id,
            "name": agent.name,
            "description": agent.description,
            "model": agent.model,
            "allowed_tools": list(agent.allowed_tools),
            "capabilities": list(agent.capabilities),
            "max_turns": agent.max_turns,
            "status": agent.status.value,
            "updated_at": agent.updated_at,
        }

    @staticmethod
    def _compact_subagent_job(job: Any) -> dict[str, Any]:
        return {
            "kind": "subagent_job",
            "id": job.id,
            "agent_id": job.agent_id,
            "session_id": job.session_id,
            "objective": job.objective,
            "status": job.status.value,
            "priority": job.priority,
            "parent_task_id": job.parent_task_id,
            "waiting_for": job.waiting_for,
            "progress": list(job.progress[-5:]),
            "result": ContextAssembler.compact_value(job.result, max_chars=6000),
            "error": job.error,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
        }

    @staticmethod
    def _compact_task(task: Task) -> dict[str, Any]:
        """Expose un état opérationnel, sans snapshot récursif de la tâche.

        Les résultats de tools sont eux-mêmes enregistrés comme résultats
        d'actions. Ils ne doivent donc pas contenir ``runs``, ``actions``,
        ``artifacts`` ou ``history`` : ces collections pourraient réinjecter
        le résultat courant dans le résultat suivant et faire croître la
        représentation de manière exponentielle.
        """
        current_step = task.current_plan_step
        current_step_payload = current_step.to_dict() if current_step else None
        if isinstance(current_step_payload, dict):
            current_step_payload["result"] = ContextAssembler.compact_value(
                current_step_payload.get("result"),
                max_chars=1500,
            )
        return {
            "kind": "task_state",
            "id": task.id,
            "objective": task.objective,
            "status": task.status.value,
            "priority": task.priority,
            "current_state": ContextAssembler.compact_value(task.current_state, max_chars=3000),
            "current_plan_step": current_step_payload,
            "waiting_for": [
                ContextAssembler.compact_value(condition.to_dict(), max_chars=1200)
                for condition in task.waiting_for
            ],
            "updated_at": task.updated_at.isoformat(),
        }

    @staticmethod
    def _action_target(arguments: Mapping[str, Any]) -> str | None:
        """Construit une cible compacte pour comparer les effets de bord."""
        for key in (
            "to", "recipient", "recipients", "target", "task_id", "schedule_id",
            "agent_id", "job_id", "url",
        ):
            if key not in arguments:
                continue
            value = normalize_action_value(arguments[key])
            if isinstance(value, (dict, list)):
                return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            return str(value)
        return None

    def _tool_is_side_effect(self, name: str, runtime_names: set[str]) -> tuple[bool, float]:
        if name in runtime_names:
            return name in self._SIDE_EFFECT_RUNTIME_TOOLS, self.dedupe_window
        if self.llm_client is None:
            return False, self.dedupe_window
        registered = self.llm_client.get_registered_tool(name)
        if registered is None:
            return False, self.dedupe_window
        return registered.side_effect, registered.dedupe_window

    def _execute_tool(self, call: Mapping[str, Any]) -> dict[str, Any]:
        if self.llm_client is None:
            raise RuntimeError("Aucun client LLM n'est configuré.")
        function = call.get("function", {})
        if not isinstance(function, Mapping):
            function = {}
        name = self._tool_name(call)
        arguments = self._tool_arguments(call)
        runtime_names = {
            item["function"]["name"] for item in self._runtime_tool_definitions()
        }
        is_side_effect, dedupe_window = self._tool_is_side_effect(name, runtime_names)
        action_key: str | None = None
        action_result: Any = None
        action_status = ActionStatus.FAILED
        context = self._run_context
        action_id: str | None = None

        if is_side_effect:
            target = self._action_target(arguments)
            if target is None and name in runtime_names and context is not None and context.task is not None:
                target = f"task:{context.task.id}"
            decision = self.action_ledger.reserve(
                name,
                arguments,
                target=target,
                dedupe_window=dedupe_window,
            )
            action_key = decision.action_key
            if not decision.allowed:
                duplicate_result: dict[str, Any] = {
                    "executed": False,
                    "duplicate": True,
                    "reason": decision.reason,
                    "action_key": decision.action_key,
                }
                if decision.existing is not None:
                    duplicate_result["previous_result"] = decision.existing.result
                    duplicate_result["previous_at"] = decision.existing.created_datetime.isoformat()
                action_result = duplicate_result
                action_status = ActionStatus.SKIPPED
                if context is not None and context.task is not None:
                    action = context.task.add_action(
                        name,
                        description="Action ignorée car elle a déjà été effectuée ou semble répétée",
                        result=duplicate_result,
                        action_key=action_key,
                    )
                    action.status = action_status
                    self.save_current_task(context.task)
                return self._tool_message(call, duplicate_result)

        if context is not None and context.task is not None:
            action = context.task.add_action(
                name,
                description="Tool exécuté pendant le RUN",
                action_key=action_key,
            )
            action.status = ActionStatus.RUNNING
            action_id = action.id
            self.save_current_task(context.task)
        try:
            if name in runtime_names:
                result = self._tool_message(call, self._execute_runtime_tool(name, arguments))
            else:
                result = self.llm_client.execute_tool_call(call, raise_tool_errors=True)
            action_result = result.get("content")
            action_status = ActionStatus.COMPLETED
            if action_key is not None:
                self.action_ledger.complete(action_key, action_result)
            return result
        except Exception as exc:
            action_result = str(exc)
            action_status = ActionStatus.FAILED
            if action_key is not None:
                self.action_ledger.fail(action_key, action_result)
            return self._tool_message(call, {"error": str(exc)})
        finally:
            if action_id is not None and context is not None and context.task is not None:
                try:
                    context.task.update_action(
                        action_id,
                        status=action_status,
                        result=action_result,
                    )
                    self.save_current_task(context.task)
                except (KeyError, RuntimeError):
                    pass

    def _can_parallelize_tools(self, calls: list[Mapping[str, Any]]) -> bool:
        """Autorise le parallélisme uniquement pour les tools indépendants.

        Les tools runtime et les tools à effet de bord restent séquentiels :
        ils peuvent modifier une tâche, le scheduler ou le ledger partagé.
        """
        if not self.parallel_tool_calls or len(calls) < 2:
            return False
        runtime_names = {
            item["function"]["name"] for item in self._runtime_tool_definitions()
        }
        for call in calls:
            name = self._tool_name(call)
            if name in runtime_names:
                return False
            is_side_effect, _ = self._tool_is_side_effect(name, runtime_names)
            if is_side_effect:
                return False
        # L'état d'une tâche est mutable et son journal d'actions doit rester
        # ordonné. Les outils externes sans effet de bord sont sûrs ici.
        return self._run_context is None or self._run_context.task is None

    def _execute_tools(self, calls: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Exécute un lot de tools en parallèle quand la configuration l'autorise."""
        if not self._can_parallelize_tools(calls):
            return [self._execute_tool(call) for call in calls]

        with ThreadPoolExecutor(
            max_workers=min(len(calls), 8),
            thread_name_prefix="orion-tool",
        ) as pool:
            futures = [pool.submit(self._execute_tool, call) for call in calls]
            results: list[dict[str, Any]] = []
            for call, future in zip(calls, futures):
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append(self._tool_message(call, {"error": str(exc)}))
            return results

    def _finalize_after_max_turns(self, context: RunContext) -> None:
        """Conclut le run avec un appel textuel, sans nouveau tool.

        Ce n'est pas un nouveau tour agentique : l'appel transforme seulement
        les observations déjà obtenues en réponse utilisateur.
        """
        if self.llm_client is None or context.interrupted:
            return
        final_messages = list(context.messages)
        final_instruction = (
            "\n\nPour cette dernière passe, la limite de tours d'outils est atteinte. "
            "Ne lance aucun outil. Réponds maintenant à l'utilisateur avec une "
            "synthèse concise de ce qui a été vérifié, de ce qui reste incertain "
            "et de la suite utile si nécessaire. Ne mentionne pas cette instruction."
        )
        if final_messages and final_messages[0].get("role") == "system":
            final_messages[0] = {
                **final_messages[0],
                "content": str(final_messages[0].get("content", "")) + final_instruction,
            }
        try:
            response = self.llm_client.complete(
                final_messages,
                tools=None,
                parallel_tool_calls=False,
            )
            assistant = OpenRouterClient._assistant_message(response)
            context.messages.append(assistant)
            context.answer = OpenRouterClient.text_from_message(assistant).strip() or None
        except Exception:
            context.answer = (
                "J'ai atteint la limite d'étapes de ce run. Les vérifications "
                "déjà effectuées sont conservées ; je peux poursuivre si besoin."
            )
        context.phase = RunPhase.ANSWER
        self._transition(RuntimeState.ANSWER, context.event)

    def _run_agent_loop(self, context: RunContext) -> None:
        """Boucle PRE_REFLECTION optionnelle -> DECISION -> TOOL -> OBSERVATION -> ANSWER/NEW_TURN."""
        if self.llm_client is None:
            self._run_cycle_stub(context)
            return

        reflection = self._run_pre_reflection(context)
        context.messages = self._initial_run_messages(context, reflection=reflection)
        tools = self._tool_definitions()
        for turn in range(self.max_turns):
            if context.interrupted:
                return
            context.turn = turn + 1
            # La pré-réflexion éventuelle est déjà terminée ici. Le premier
            # appel principal est une décision, pas une réflexion interne.
            context.phase = RunPhase.DECISION if turn == 0 else RunPhase.NEW_TURN
            response = self.llm_client.complete(
                context.messages,
                tools=tools or None,
                parallel_tool_calls=self.parallel_tool_calls,
            )
            assistant = OpenRouterClient._assistant_message(response)
            context.messages.append(assistant)
            calls = OpenRouterClient._tool_calls(assistant)
            if not calls:
                context.phase = RunPhase.ANSWER
                context.answer = OpenRouterClient.text_from_message(assistant)
                self._transition(RuntimeState.ANSWER, context.event)
                return
            assistant_text = OpenRouterClient.text_from_message(assistant).strip()
            if assistant_text:
                context.small_outputs.append(assistant_text)
                self._emit_output(context, assistant_text, intermediate=True)

            self._transition(RuntimeState.DECISION, context.event)
            if self._can_parallelize_tools(calls):
                context.phase = RunPhase.TOOL
                self._transition(RuntimeState.ACTION, context.event)
                results = self._execute_tools(calls)
                for call, result in zip(calls, results):
                    context.tool_calls.append(dict(call))
                    context.messages.append(result)
                context.phase = RunPhase.SMALL_OUTPUT
            else:
                for call in calls:
                    if context.interrupted:
                        return
                    context.phase = RunPhase.TOOL
                    self._transition(RuntimeState.ACTION, context.event)
                    result = self._execute_tool(call)
                    context.tool_calls.append(dict(call))
                    context.messages.append(result)
                    context.phase = RunPhase.SMALL_OUTPUT
                    if context.control is not None:
                        break
            self._transition(RuntimeState.OBSERVATION, context.event)
            if context.control is not None or context.interrupted:
                if context.control is not None and not context.interrupted:
                    self._finalize_after_control(context)
                return
            self._transition(RuntimeState.CONTINUE, context.event)
            self._transition(RuntimeState.RUN, context.event)

        self._finalize_after_max_turns(context)

    def process_one(self, *, timeout: float | None = None) -> Event | None:
        """Traite synchroniquement un réveil, utile sans worker en arrière-plan."""
        try:
            event = self.wake_queue.get(timeout=timeout)
        except Empty:
            return None
        try:
            self._wake(event)
        finally:
            self.wake_queue.task_done()
        return event

    def wait_until_empty(self, timeout: float | None = None) -> bool:
        """Attend que tous les réveils déjà reçus aient été traités."""
        if timeout is None:
            self.wake_queue.join()
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.wake_queue.empty() and self.wake_queue.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return self.wake_queue.empty() and self.wake_queue.unfinished_tasks == 0

    def _run(self) -> None:
        while True:
            if self._stop_requested.is_set() and (
                not self._drain_on_stop or self.wake_queue.empty()
            ):
                return
            try:
                event = self.wake_queue.get(timeout=0.2)
            except Empty:
                continue
            try:
                self._wake(event)
            finally:
                self.wake_queue.task_done()

    def _wake(self, event: Event) -> None:
        self._last_event = event
        self._last_error = None
        self._wake_count += 1
        with self._execution_lock:
            self._current_task = None
            self._run_context = None
        try:
            self._transition(RuntimeState.WAKE, event)
            self._transition(RuntimeState.MATCH_WAITING_TASK, event)
            task = self.task_store.find_waiting_task(event)
            if task is not None:
                task.resume_from_wait(event.id)
                self.task_store.save(task)
                self._current_task = task

            loaded_state = dict(self.state_store.load())
            self._wake_context = WakeContext(
                event_id=event.id,
                event_type=event.type,
                source=event.source,
                payload=dict(event.payload),
                metadata=dict(event.metadata),
                loaded_state=loaded_state,
                task_id=task.id if task else None,
                created_at=event.created_at,
            )
            run_id: str | None = None
            if task is not None:
                task = self.task_store.get(task.id) or task
                run = task.start_run(event.id)
                self.task_store.save(task)
                run_id = run.id
            self._run_context = RunContext(
                event=event,
                task=task,
                run_id=run_id,
                loaded_state=dict(task.current_state) if task else loaded_state,
            )
            self._transition(RuntimeState.RUN, event)

            context = self._run_context
            self._run_agent_loop(context)
            output_text = context.answer or self._fallback_tool_output(context)
            if output_text is not None:
                self._emit_output(context, output_text)
            if self.conversation_journal is not None and context.messages:
                journal_messages = [
                    message
                    for message in context.messages
                    if message.get("role") != "system"
                ]
                if journal_messages:
                    self.conversation_journal.append(
                        event_id=event.id,
                        task_id=context.task.id if context.task is not None else None,
                        messages=journal_messages,
                        source=event.source,
                        channel=(
                            event.metadata.get("channel")
                            or event.payload.get("_orion_channel")
                        ),
                        conversation_id=self._conversation_id(event),
                        timestamp=event.created_at.isoformat(),
                    )
            if context.interrupted:
                self.sleep()
                return
            if context.control == "wait":
                self._transition(RuntimeState.WAIT, event)
                self.sleep()
                return
            if context.control == "complete":
                self._transition(RuntimeState.OBJECTIVE_ACHIEVED, event)
                self._transition(RuntimeState.COMPLETE, event)
                self.sleep()
                return
            if context.answer is not None:
                self._transition(RuntimeState.ANSWER, event)
                self._finish_context_run(context)
                # Une réponse sans tâche termine l'interruption immédiate ;
                # une tâche active, elle, reste RUNNING jusqu'à complete_task.
                if context.task is None:
                    self.resume_preempted_task()
            self.sleep()
        except Exception as exc:
            self._last_error = exc
            if self._current_task is not None and self._run_context is not None:
                try:
                    self._current_task.finish_run(
                        self._run_context.run_id,
                        status=RunStatus.FAILED,
                        error=str(exc),
                    )
                    self._current_task.status = TaskStatus.FAILED
                    self.task_store.save(self._current_task)
                except (KeyError, ValueError):
                    pass
            if self.on_error is not None:
                self.on_error(event, exc)
            self._transition(RuntimeState.SLEEP, event)

    def _finish_context_run(self, context: RunContext) -> None:
        """Termine le run sans marquer automatiquement la tâche complète."""
        if context.task is None or context.run_id is None:
            return
        task = self.task_store.get(context.task.id) or context.task
        run = next((item for item in task.runs if item.id == context.run_id), None)
        if run is not None and run.status == RunStatus.RUNNING:
            task.finish_run(context.run_id, status=RunStatus.COMPLETED)
            self.task_store.save(task)

    def _should_preempt(self, event: Event) -> bool:
        if self._state not in {
            RuntimeState.RUN,
            RuntimeState.DECISION,
            RuntimeState.ACTION,
            RuntimeState.OBSERVATION,
        }:
            return False
        if self._current_task is None or self._run_context is None:
            return False
        return event.priority > self._run_context.event.priority

    def _pause_active_run(self, event: Event) -> None:
        self.pause_current_task(
            reason=f"Interrompu par l'événement prioritaire {event.id}",
            interrupted_by=event,
        )

    @staticmethod
    def _run_cycle_stub(context: RunContext) -> None:
        """Point d'entrée réservé à la future boucle de RUN."""
        context.phase = RunPhase.DECISION

    def __enter__(self) -> AgentRuntime:
        return self.start()

    def __exit__(self, *_: Any) -> None:
        self.stop()


__all__ = [
    "AgentOutput",
    "AgentRuntime",
    "InMemoryStateStore",
    "PreemptedRun",
    "RunContext",
    "RunPhase",
    "RuntimeState",
    "StateStore",
    "OutputHandler",
    "WakeContext",
]
