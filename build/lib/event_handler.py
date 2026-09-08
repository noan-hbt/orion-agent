"""File d'événements prioritaire, indépendante de tout LLM.

Le module reçoit des événements provenant de sources diverses (message, mail,
webhook, appel externe, cron...) et les distribue à des handlers locaux.
L'ordre de traitement est : priorité décroissante, puis ordre d'arrivée.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, IntEnum
from itertools import count
from queue import Empty, Full, PriorityQueue
from typing import Any


class EventPriority(IntEnum):
    """Niveaux de priorité disponibles pour un événement."""

    LOW = 10
    NORMAL = 20
    HIGH = 30
    CRITICAL = 40


class EventType(str, Enum):
    """Types courants ; une chaîne libre peut aussi être utilisée."""

    MESSAGE = "message"
    EMAIL = "email"
    WEBHOOK = "webhook"
    EXTERNAL_CALL = "external_call"
    CRON = "cron"
    SCHEDULE = "schedule"
    CUSTOM = "custom"


EventHandlerFunction = Callable[["Event"], Any]


class DeliveryError(Exception):
    """Erreur stable de livraison, avec un code utilisable par un appelant."""

    status_code = 503

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class EventQueueFullError(Full):
    """La file est pleine et l'événement n'a pas été accepté."""

    status_code = 429

    def __init__(self, message: str = "La file d'événements est pleine.", *, retry_after: float = 0.1) -> None:
        super().__init__(message)
        self.retry_after = retry_after


BackpressureError = EventQueueFullError


class DuplicateEventError(DeliveryError):
    """Un identifiant de déduplication a déjà été utilisé avec un autre contenu."""

    status_code = 409


@dataclass(slots=True)
class Event:
    """Événement placé dans la file et transmis aux handlers."""

    type: str
    payload: dict[str, Any]
    priority: int = int(EventPriority.NORMAL)
    source: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    max_attempts: int = 3
    attempts: int = 0
    message_id: str | None = None
    correlation_id: str | None = None
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        self.type = self.type.value if isinstance(self.type, Enum) else str(self.type)
        self.priority = int(self.priority)
        if self.priority < 0:
            raise ValueError("La priorité doit être supérieure ou égale à zéro.")
        if self.max_attempts < 1:
            raise ValueError("max_attempts doit être supérieur ou égal à un.")
        if self.message_id is None:
            self.message_id = self.metadata.get("message_id")
        if self.correlation_id is None:
            self.correlation_id = self.metadata.get("correlation_id")
        if self.idempotency_key is None:
            self.idempotency_key = self.metadata.get("idempotency_key")


class EventQueue:
    """File thread-safe : priorité élevée d'abord, FIFO à priorité égale."""

    def __init__(self, maxsize: int = 0) -> None:
        if maxsize < 0:
            raise ValueError("maxsize doit être positif ou nul.")
        self._items: PriorityQueue[tuple[int, int, Event]] = PriorityQueue(maxsize=maxsize)
        self._sequence = count()

    def put(self, event: Event, *, timeout: float | None = None) -> None:
        """Ajoute un événement ; lève ``queue.Full`` si la file est pleine."""
        priority_item = (-event.priority, next(self._sequence), event)
        if timeout is None or timeout == 0:
            self._items.put_nowait(priority_item)
        elif timeout < 0:
            raise ValueError("timeout doit être supérieur ou égal à zéro ou nul.")
        else:
            self._items.put(priority_item, timeout=timeout)

    def put_nowait(self, event: Event) -> None:
        self._items.put_nowait((-event.priority, next(self._sequence), event))

    def get(self, *, timeout: float | None = None) -> Event:
        if timeout is None:
            return self._items.get()
        return self._items.get(timeout=timeout)[2]

    def get_nowait(self) -> Event:
        return self._items.get_nowait()[2]

    def task_done(self) -> None:
        self._items.task_done()

    def join(self) -> None:
        self._items.join()

    def empty(self) -> bool:
        return self._items.empty()

    def qsize(self) -> int:
        return self._items.qsize()

    @property
    def unfinished_tasks(self) -> int:
        return self._items.unfinished_tasks


class EventHandler:
    """Routeur d'événements avec file prioritaire et workers optionnels.

    Les handlers reçoivent un objet :class:`Event`. Ils peuvent être
    synchrones ou asynchrones. Un événement en erreur est retenté jusqu'à
    ``max_attempts`` ; après cela, il est placé dans ``dead_letters`` et le
    callback ``on_error`` est appelé.

    Le module ne connaît pas ``OpenRouterClient`` et n'appelle aucun LLM.
    """

    def __init__(
        self,
        *,
        workers: int = 0,
        queue_size: int = 1000,
        default_max_attempts: int = 3,
        retry_delay: float = 1.0,
        retry_backoff: float = 2.0,
        on_error: Callable[[Event, Exception], Any] | None = None,
        on_unhandled: Callable[[Event], Any] | None = None,
    ) -> None:
        if workers < 0:
            raise ValueError("workers doit être positif ou nul.")
        if default_max_attempts < 1:
            raise ValueError("default_max_attempts doit être supérieur ou égal à un.")
        if retry_delay < 0 or retry_backoff < 1:
            raise ValueError("retry_delay doit être >= 0 et retry_backoff doit être >= 1.")

        self.queue = EventQueue(maxsize=queue_size)
        self.workers = workers
        self.default_max_attempts = default_max_attempts
        self.retry_delay = retry_delay
        self.retry_backoff = retry_backoff
        self.on_error = on_error
        self.on_unhandled = on_unhandled

        self._handlers: dict[str, list[EventHandlerFunction]] = {}
        self._dead_letters: list[Event] = []
        self._dead_letters_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._stop_requested = threading.Event()
        self._drain_on_stop = True
        self._threads: list[threading.Thread] = []
        self._running = False
        self._dedupe: dict[str, tuple[str, Event]] = {}
        self._dedupe_lock = threading.Lock()
        self._callback_errors: list[Exception] = []

    @property
    def running(self) -> bool:
        return self._running

    @property
    def dead_letters(self) -> list[Event]:
        """Copie des événements qui ont épuisé leurs tentatives."""
        with self._dead_letters_lock:
            return list(self._dead_letters)

    @property
    def callback_errors(self) -> list[Exception]:
        """Erreurs de callbacks observées sans interrompre le worker."""
        with self._dead_letters_lock:
            return list(self._callback_errors)

    def register(self, event_type: str | EventType, handler: EventHandlerFunction) -> None:
        """Associe un handler à un type. ``*`` reçoit tous les événements."""
        if not callable(handler):
            raise TypeError("handler doit être appelable.")
        key = event_type.value if isinstance(event_type, EventType) else str(event_type)
        self._handlers.setdefault(key, []).append(handler)

    def unregister(self, event_type: str | EventType, handler: EventHandlerFunction) -> None:
        """Retire un handler précédemment enregistré."""
        key = event_type.value if isinstance(event_type, EventType) else str(event_type)
        handlers = self._handlers.get(key, [])
        if handler in handlers:
            handlers.remove(handler)
        if not handlers:
            self._handlers.pop(key, None)

    def publish(
        self,
        event_type: str | EventType,
        payload: Mapping[str, Any] | None = None,
        *,
        priority: int | EventPriority = EventPriority.NORMAL,
        source: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        max_attempts: int | None = None,
        timeout: float | None = None,
        message_id: str | None = None,
        correlation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> Event:
        """Crée et place un événement dans la file."""
        event_metadata = dict(metadata or {})
        if message_id is not None:
            event_metadata.setdefault("message_id", message_id)
        if correlation_id is not None:
            event_metadata.setdefault("correlation_id", correlation_id)
        if idempotency_key is not None:
            event_metadata.setdefault("idempotency_key", idempotency_key)
        event = Event(
            type=event_type.value if isinstance(event_type, EventType) else str(event_type),
            payload=dict(payload or {}),
            priority=int(priority),
            source=source,
            metadata=event_metadata,
            max_attempts=(
                max_attempts if max_attempts is not None else self.default_max_attempts
            ),
            message_id=message_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        dedupe_key = message_id or idempotency_key
        if dedupe_key:
            fingerprint = self._fingerprint(event)
            with self._dedupe_lock:
                existing = self._dedupe.get(str(dedupe_key))
                if existing is not None:
                    if existing[0] != fingerprint:
                        raise DuplicateEventError(
                            f"Identifiant déjà utilisé avec un contenu différent : {dedupe_key}"
                        )
                    return existing[1]
                self._dedupe[str(dedupe_key)] = (fingerprint, event)
        try:
            self.enqueue(event, timeout=timeout)
        except Full as exc:
            if dedupe_key:
                with self._dedupe_lock:
                    self._dedupe.pop(str(dedupe_key), None)
            raise EventQueueFullError(retry_after=0.1) from exc
        return event

    def enqueue(self, event: Event, *, timeout: float | None = None) -> None:
        """Place un événement déjà construit dans la file."""
        if not isinstance(event, Event):
            raise TypeError("event doit être une instance de Event.")
        try:
            self.queue.put(event, timeout=timeout)
        except EventQueueFullError:
            raise
        except Full as exc:
            raise EventQueueFullError(retry_after=0.1) from exc

    def start(self) -> None:
        """Démarre les workers configurés ; sans worker, le dispatch est manuel."""
        if self.workers == 0:
            return
        with self._lifecycle_lock:
            if self._running:
                return
            self._stop_requested.clear()
            self._drain_on_stop = True
            self._threads = []
            for index in range(self.workers):
                thread = threading.Thread(
                    target=self._worker,
                    name=f"event-handler-{index + 1}",
                    daemon=True,
                )
                thread.start()
                self._threads.append(thread)
            self._running = True

    def stop(self, *, wait: bool = True, drain: bool = True) -> None:
        """Arrête les workers.

        Avec ``drain=True``, les événements déjà en file sont traités avant
        l'arrêt. Avec ``drain=False``, les workers s'arrêtent rapidement et la
        file restante peut être reprise par un futur ``start()``.
        """
        with self._lifecycle_lock:
            self._drain_on_stop = drain
            self._stop_requested.set()
            threads = list(self._threads)
            self._running = False

        if wait and drain:
            for thread in threads:
                thread.join()
        elif wait:
            for thread in threads:
                thread.join(timeout=2.0)

        with self._lifecycle_lock:
            self._threads = []

    def cancel(self) -> None:
        """Demande un arrêt immédiat sans drainer la file."""
        self.stop(wait=False, drain=False)

    def dispatch_one(self, *, timeout: float | None = None) -> Event | None:
        """Traite un événement manuellement et retourne celui traité."""
        try:
            event = self.queue.get(timeout=timeout)
        except Empty:
            return None
        try:
            self._dispatch(event)
        finally:
            self.queue.task_done()
        return event

    def wait_until_empty(self, timeout: float | None = None) -> bool:
        """Attend que tous les événements aient été traités."""
        if timeout is None:
            self.queue.join()
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.queue.empty() and self.queue.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return self.queue.empty() and self.queue.unfinished_tasks == 0

    def _worker(self) -> None:
        while True:
            if self._stop_requested.is_set() and (
                not self._drain_on_stop or self.queue.empty()
            ):
                return
            try:
                event = self.queue.get(timeout=0.2)
            except Empty:
                continue
            try:
                self._dispatch(event)
            finally:
                self.queue.task_done()

    def _matching_handlers(self, event: Event) -> list[EventHandlerFunction]:
        return list(self._handlers.get(event.type, [])) + list(self._handlers.get("*", []))

    def _dispatch(self, event: Event) -> None:
        handlers = self._matching_handlers(event)
        if not handlers:
            if self.on_unhandled is not None:
                try:
                    self._invoke(self.on_unhandled, event)
                except Exception as callback_error:
                    with self._dead_letters_lock:
                        self._callback_errors.append(callback_error)
                    self._dead_letter(event)
            else:
                self._dead_letter(event)
            return

        event.attempts += 1
        try:
            for handler in handlers:
                self._invoke(handler, event)
        except Exception as exc:
            if event.attempts < event.max_attempts:
                delay = self.retry_delay * (self.retry_backoff ** (event.attempts - 1))
                if delay:
                    time.sleep(delay)
                try:
                    self.queue.put_nowait(event)
                except Full as retry_exc:
                    self._dead_letter(event)
                    self._invoke_error_callback_safely(self.on_error, event, retry_exc)
                return
            self._dead_letter(event)
            self._invoke_error_callback_safely(self.on_error, event, exc)

    def _dead_letter(self, event: Event) -> None:
        with self._dead_letters_lock:
            if event not in self._dead_letters:
                self._dead_letters.append(event)

    def _invoke_error_callback_safely(
        self,
        callback: Callable[[Event, Exception], Any] | None,
        event: Event,
        error: Exception,
    ) -> None:
        if callback is None:
            return
        try:
            self._invoke_error_callback(callback, event, error)
        except Exception as callback_error:
            with self._dead_letters_lock:
                self._callback_errors.append(callback_error)

    @staticmethod
    def _fingerprint(event: Event) -> str:
        body = {
            "type": event.type,
            "payload": event.payload,
            "priority": event.priority,
            "source": event.source,
            "metadata": event.metadata,
        }
        encoded = json.dumps(body, sort_keys=True, default=str, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _invoke(handler: Callable[..., Any], event: Event) -> Any:
        result = handler(event)
        if inspect.isawaitable(result):
            return asyncio.run(result)
        return result

    @staticmethod
    def _invoke_error_callback(
        callback: Callable[[Event, Exception], Any], event: Event, error: Exception
    ) -> Any:
        result = callback(event, error)
        if inspect.isawaitable(result):
            return asyncio.run(result)
        return result

    def __enter__(self) -> EventHandler:
        self.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.stop()


__all__ = [
    "Event",
    "EventHandler",
    "EventPriority",
    "EventQueue",
    "EventType",
    "BackpressureError",
    "DeliveryError",
    "DuplicateEventError",
    "EventQueueFullError",
]
