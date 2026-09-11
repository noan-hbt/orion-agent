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
import math
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, IntEnum
from itertools import count
from queue import Empty, Full, PriorityQueue
from typing import Any

from durable_events import (
    DurableEventConflict,
    DurableEventReceipt,
    DurableEventStore,
)


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


@dataclass(frozen=True, slots=True)
class _DedupeRecord:
    """Small idempotency record; deliberately never retains an event payload."""

    fingerprint: str
    event_id: str
    created_at: datetime
    max_attempts: int
    expires_at: float


@dataclass(frozen=True, slots=True)
class _DispatchOutcome:
    status: str
    error: Exception | None = None


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
            return self._items.get()[2]
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

    _DURABLE_NAMESPACE = "event_handler"
    _DURABLE_LEASE_SECONDS = 300.0
    _DURABLE_RECOVERY_BATCH = 1000

    def __init__(
        self,
        *,
        workers: int = 0,
        queue_size: int = 1000,
        default_max_attempts: int = 3,
        retry_delay: float = 1.0,
        retry_backoff: float = 2.0,
        dedupe_ttl: float = 86400.0,
        dedupe_max_entries: int = 10000,
        durable_path: str | None = None,
        on_error: Callable[[Event, Exception], Any] | None = None,
        on_unhandled: Callable[[Event], Any] | None = None,
    ) -> None:
        if workers < 0:
            raise ValueError("workers doit être positif ou nul.")
        if default_max_attempts < 1:
            raise ValueError("default_max_attempts doit être supérieur ou égal à un.")
        if retry_delay < 0 or retry_backoff < 1:
            raise ValueError("retry_delay doit être >= 0 et retry_backoff doit être >= 1.")
        if (
            isinstance(dedupe_ttl, bool)
            or not isinstance(dedupe_ttl, (int, float))
            or not math.isfinite(float(dedupe_ttl))
            or dedupe_ttl < 0
        ):
            raise ValueError("dedupe_ttl doit être un nombre fini >= 0.")
        if (
            isinstance(dedupe_max_entries, bool)
            or not isinstance(dedupe_max_entries, int)
            or dedupe_max_entries < 1
        ):
            raise ValueError("dedupe_max_entries doit être un entier >= 1.")

        self.queue = EventQueue(maxsize=queue_size)
        self.workers = workers
        self.default_max_attempts = default_max_attempts
        self.retry_delay = retry_delay
        self.retry_backoff = retry_backoff
        self.dedupe_ttl = float(dedupe_ttl)
        self.dedupe_max_entries = dedupe_max_entries
        self.durable_path = str(durable_path) if durable_path else None
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
        self._dedupe: OrderedDict[str, _DedupeRecord] = OrderedDict()
        self._dedupe_next_expiry: float | None = None
        self._dedupe_lock = threading.Lock()
        self._callback_errors: list[Exception] = []
        self._durable_store = (
            DurableEventStore(self.durable_path, namespace=self._DURABLE_NAMESPACE)
            if self.durable_path is not None
            else None
        )
        self._durable_lock = threading.RLock()
        self._durable_receipts_by_event_id: dict[str, str] = {}
        self._ram_event_ids: set[str] = set()
        self._durable_owner_prefix = uuid.uuid4().hex

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
        dedupe_key = str(dedupe_key) if dedupe_key else None
        now = time.monotonic()
        fingerprint = self._fingerprint(event) if dedupe_key else None

        if self._durable_store is not None:
            if dedupe_key is not None and fingerprint is not None:
                with self._dedupe_lock:
                    self._prune_dedupe_locked(now)
                    existing = self._dedupe.get(dedupe_key)
                    if existing is not None:
                        self._dedupe.move_to_end(dedupe_key)
                        if existing.fingerprint != fingerprint:
                            raise DuplicateEventError(
                                f"Identifiant déjà utilisé avec un contenu différent : {dedupe_key}"
                            )
                        event.id = existing.event_id
                        event.created_at = existing.created_at
                        event.max_attempts = existing.max_attempts
                        return event
            accepted = self.enqueue(event, timeout=timeout)
            if dedupe_key is not None:
                accepted_fingerprint = self._fingerprint(accepted)
                with self._dedupe_lock:
                    self._prune_dedupe_locked(now)
                    expires_at = now + self.dedupe_ttl
                    self._dedupe[dedupe_key] = _DedupeRecord(
                        fingerprint=accepted_fingerprint,
                        event_id=accepted.id,
                        created_at=accepted.created_at,
                        max_attempts=accepted.max_attempts,
                        expires_at=expires_at,
                    )
                    self._dedupe.move_to_end(dedupe_key)
                    if (
                        self._dedupe_next_expiry is None
                        or expires_at < self._dedupe_next_expiry
                    ):
                        self._dedupe_next_expiry = expires_at
                    while len(self._dedupe) > self.dedupe_max_entries:
                        self._dedupe.popitem(last=False)
            return accepted

        with self._dedupe_lock:
            self._prune_dedupe_locked(now)
            if dedupe_key is not None and fingerprint is not None:
                existing = self._dedupe.get(dedupe_key)
                if existing is not None:
                    self._dedupe.move_to_end(dedupe_key)
                    if existing.fingerprint != fingerprint:
                        raise DuplicateEventError(
                            f"Identifiant déjà utilisé avec un contenu différent : {dedupe_key}"
                        )
                    event.id = existing.event_id
                    event.created_at = existing.created_at
                    event.max_attempts = existing.max_attempts
                    return event
                expires_at = now + self.dedupe_ttl
                self._dedupe[dedupe_key] = _DedupeRecord(
                    fingerprint=fingerprint,
                    event_id=event.id,
                    created_at=event.created_at,
                    max_attempts=event.max_attempts,
                    expires_at=expires_at,
                )
                if self._dedupe_next_expiry is None or expires_at < self._dedupe_next_expiry:
                    self._dedupe_next_expiry = expires_at
                while len(self._dedupe) > self.dedupe_max_entries:
                    self._dedupe.popitem(last=False)
        try:
            self.enqueue(event, timeout=timeout)
        except Full as exc:
            if dedupe_key is not None:
                with self._dedupe_lock:
                    existing = self._dedupe.get(dedupe_key)
                    if existing is not None and existing.event_id == event.id:
                        self._dedupe.pop(dedupe_key, None)
                        if not self._dedupe:
                            self._dedupe_next_expiry = None
            raise EventQueueFullError(retry_after=0.1) from exc
        return event

    def _prune_dedupe_locked(self, now: float) -> None:
        """Drop expired records; caller must hold ``_dedupe_lock``."""
        next_expiry = self._dedupe_next_expiry
        if next_expiry is None or now < next_expiry:
            return
        expired = [
            key for key, record in self._dedupe.items() if record.expires_at <= now
        ]
        for key in expired:
            self._dedupe.pop(key, None)
        self._dedupe_next_expiry = min(
            (record.expires_at for record in self._dedupe.values()),
            default=None,
        )

    def enqueue(self, event: Event, *, timeout: float | None = None) -> Event:
        """Place un événement déjà construit dans la file."""
        if not isinstance(event, Event):
            raise TypeError("event doit être une instance de Event.")
        if self._durable_store is not None:
            receipt = self._durably_accept(event)
            accepted = self._event_from_receipt(receipt)
            with self._durable_lock:
                self._durable_receipts_by_event_id[accepted.id] = receipt.receipt_id
            if receipt.status == "queued":
                try:
                    self._enqueue_ram_once(accepted, timeout=timeout)
                except EventQueueFullError:
                    # The durable commit is the acceptance boundary. Reporting
                    # QueueFull here would tell callers that the event was not
                    # accepted and invite a retry with a fresh event id. Leave
                    # the receipt queued instead; hydration will move it into
                    # RAM as soon as capacity becomes available.
                    pass
            return accepted
        try:
            self.queue.put(event, timeout=timeout)
        except EventQueueFullError:
            raise
        except Full as exc:
            raise EventQueueFullError(retry_after=0.1) from exc
        return event

    def _durably_accept(self, event: Event) -> DurableEventReceipt:
        store = self._durable_store
        if store is None:
            raise RuntimeError("durable event store is disabled")
        try:
            return store.accept(self._durable_mapping(event))
        except DurableEventConflict as exc:
            raise DuplicateEventError(
                "Identifiant durable déjà utilisé avec un contenu différent."
            ) from exc

    def _durable_mapping(self, event: Event) -> dict[str, Any]:
        return {
            "event_id": event.id,
            "idempotency_key": event.idempotency_key,
            "message_id": event.message_id,
            "fingerprint": self._fingerprint(event),
            "event": {
                "type": event.type,
                "payload": event.payload,
                "priority": event.priority,
                "source": event.source,
                "metadata": event.metadata,
                "id": event.id,
                "created_at": event.created_at.isoformat(),
                "max_attempts": event.max_attempts,
                "message_id": event.message_id,
                "correlation_id": event.correlation_id,
                "idempotency_key": event.idempotency_key,
            },
        }

    @staticmethod
    def _event_from_receipt(receipt: DurableEventReceipt) -> Event:
        raw = receipt.payload.get("event")
        if not isinstance(raw, Mapping):
            raise ValueError("durable event receipt does not contain an event object")
        created_raw = raw.get("created_at")
        if not isinstance(created_raw, str):
            raise ValueError("durable event receipt has no created_at")
        created_at = datetime.fromisoformat(created_raw)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        payload = raw.get("payload", {})
        metadata = raw.get("metadata", {})
        if not isinstance(payload, Mapping) or not isinstance(metadata, Mapping):
            raise ValueError("durable event payload/metadata is invalid")
        return Event(
            type=str(raw.get("type", "custom")),
            payload=dict(payload),
            priority=int(raw.get("priority", int(EventPriority.NORMAL))),
            source=str(raw["source"]) if raw.get("source") is not None else None,
            metadata=dict(metadata),
            id=str(raw.get("id") or receipt.event_id),
            created_at=created_at,
            max_attempts=int(raw.get("max_attempts", 3)),
            attempts=int(receipt.attempts),
            message_id=receipt.message_id,
            correlation_id=(
                str(raw["correlation_id"])
                if raw.get("correlation_id") is not None
                else None
            ),
            idempotency_key=receipt.idempotency_key,
        )

    def _enqueue_ram_once(self, event: Event, *, timeout: float | None = None) -> bool:
        with self._durable_lock:
            if event.id in self._ram_event_ids:
                return False
            try:
                self.queue.put(event, timeout=timeout)
            except Full as exc:
                raise EventQueueFullError(retry_after=0.1) from exc
            self._ram_event_ids.add(event.id)
            return True

    def start(self) -> None:
        """Démarre les workers configurés ; sans worker, le dispatch est manuel."""
        current = threading.current_thread()
        while True:
            with self._lifecycle_lock:
                if self._running:
                    return
                self._threads = [thread for thread in self._threads if thread.is_alive()]
                threads = list(self._threads)
            if not threads:
                break
            if current in threads:
                raise RuntimeError("Un worker EventHandler ne peut pas redémarrer sa propre génération.")
            # A previous non-blocking stop is still quiescing. Wait for that
            # exact generation instead of clearing its stop signal or silently
            # losing this start request.
            for thread in threads:
                thread.join()

        with self._lifecycle_lock:
            if self._running:
                return
            self._threads = [thread for thread in self._threads if thread.is_alive()]
            if self._threads:
                return
            self._stop_requested.clear()
            self._drain_on_stop = True
            self._recover_durable()
            self._hydrate_durable_queue()
            if self.workers == 0:
                return
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
            self._threads = [thread for thread in self._threads if thread.is_alive()]

    def cancel(self) -> None:
        """Demande un arrêt immédiat sans drainer la file."""
        self.stop(wait=False, drain=False)

    def dispatch_one(self, *, timeout: float | None = None) -> Event | None:
        """Traite un événement manuellement et retourne celui traité."""
        if self._durable_store is not None:
            self._recover_durable()
            self._hydrate_durable_queue()
        try:
            event = self._queue_get(timeout=timeout)
        except Empty:
            return None
        try:
            self._process_event(event, owner_id=f"{self._durable_owner_prefix}:manual")
        finally:
            self.queue.task_done()
            self._hydrate_durable_queue()
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
        owner_id = f"{self._durable_owner_prefix}:{threading.get_ident()}"
        while True:
            if self._stop_requested.is_set():
                if not self._drain_on_stop:
                    return
                self._recover_durable()
                self._hydrate_durable_queue()
                if self.queue.empty():
                    return
            try:
                event = self._queue_get(timeout=0.2)
            except Empty:
                self._recover_durable()
                self._hydrate_durable_queue()
                continue
            try:
                self._process_event(event, owner_id=owner_id)
            finally:
                self.queue.task_done()
                self._hydrate_durable_queue()

    def _queue_get(self, *, timeout: float | None) -> Event:
        event = self.queue.get(timeout=timeout)
        if self._durable_store is not None:
            with self._durable_lock:
                self._ram_event_ids.discard(event.id)
        return event

    def _recover_durable(self) -> None:
        store = self._durable_store
        if store is None:
            return
        while True:
            recovered = store.recover_stale(limit=self._DURABLE_RECOVERY_BATCH)
            if len(recovered) < self._DURABLE_RECOVERY_BATCH:
                return

    def _hydrate_durable_queue(self) -> int:
        store = self._durable_store
        if store is None:
            return 0
        loaded = 0
        receipts = store.list_events(status="queued", limit=self._DURABLE_RECOVERY_BATCH)
        for receipt in receipts:
            event = self._event_from_receipt(receipt)
            with self._durable_lock:
                self._durable_receipts_by_event_id[event.id] = receipt.receipt_id
            try:
                if self._enqueue_ram_once(event, timeout=0):
                    loaded += 1
            except EventQueueFullError:
                break
        return loaded

    def _process_event(self, event: Event, *, owner_id: str) -> _DispatchOutcome:
        store = self._durable_store
        if store is None:
            return self._dispatch(event)
        with self._durable_lock:
            receipt_id = self._durable_receipts_by_event_id.get(event.id)
        if receipt_id is None:
            # The public ``enqueue`` path always records durable state first.
            # If callers bypass it and mutate ``queue`` directly, refuse to
            # dispatch an unfenced event rather than silently weakening the
            # durable contract.
            return _DispatchOutcome("skipped")
        claim = store.claim_receipt(
            receipt_id,
            owner_id=owner_id,
            lease_seconds=self._DURABLE_LEASE_SECONDS,
        )
        if claim is None:
            return _DispatchOutcome("skipped")
        event.attempts = max(0, claim.attempts - 1)
        heartbeat_stop = threading.Event()
        heartbeat_lost = threading.Event()
        heartbeat = threading.Thread(
            target=self._renew_claim_until_stopped,
            args=(
                receipt_id,
                owner_id,
                claim.fence_token,
                heartbeat_stop,
                heartbeat_lost,
            ),
            name="event-handler-lease-heartbeat",
            daemon=True,
        )
        heartbeat.start()
        try:
            outcome = self._dispatch(event, requeue=False)
        finally:
            heartbeat_stop.set()
            heartbeat.join()
        if heartbeat_lost.is_set():
            # The callback may already have produced an external side effect,
            # but this process can no longer prove that it owns the durable
            # receipt.  Never report/commit a successful ACK or failure with a
            # stale fence.  The still-processing receipt will be recovered by
            # the normal lease-expiry path and replayed under a fresh claim.
            return _DispatchOutcome(
                "skipped",
                RuntimeError("durable event ownership lost during dispatch"),
            )
        if outcome.status == "acked":
            store.ack(
                receipt_id,
                owner_id=owner_id,
                fence_token=claim.fence_token,
            )
            return outcome
        if outcome.status == "retry":
            requeued = store.fail(
                receipt_id,
                str(outcome.error or "event dispatch failed"),
                owner_id=owner_id,
                fence_token=claim.fence_token,
                retry=True,
            )
            if requeued:
                try:
                    self._enqueue_ram_once(event, timeout=0)
                except EventQueueFullError:
                    # The durable row remains queued and will be hydrated as
                    # soon as RAM capacity becomes available.
                    pass
            return outcome
        if outcome.status == "failed":
            store.fail(
                receipt_id,
                str(outcome.error or "event dispatch failed"),
                owner_id=owner_id,
                fence_token=claim.fence_token,
                retry=False,
            )
        return outcome

    def _renew_claim_until_stopped(
        self,
        receipt_id: str,
        owner_id: str,
        fence_token: int,
        stop: threading.Event,
        lost: threading.Event,
    ) -> None:
        """Keep a durable dispatch lease live for the whole callback duration."""
        store = self._durable_store
        if store is None:
            return
        interval = max(0.01, self._DURABLE_LEASE_SECONDS / 3.0)
        while not stop.wait(interval):
            try:
                if not store.renew_claim(
                    receipt_id,
                    owner_id=owner_id,
                    fence_token=fence_token,
                    lease_seconds=self._DURABLE_LEASE_SECONDS,
                ):
                    lost.set()
                    return
            except Exception as exc:
                # Renewal failures must not crash the worker thread. Preserve
                # them for diagnostics; the fenced ACK/fail below still cannot
                # commit after ownership has actually been lost.
                with self._dead_letters_lock:
                    self._callback_errors.append(exc)
                lost.set()
                return

    def _matching_handlers(self, event: Event) -> list[EventHandlerFunction]:
        return list(self._handlers.get(event.type, [])) + list(self._handlers.get("*", []))

    def _dispatch(self, event: Event, *, requeue: bool = True) -> _DispatchOutcome:
        handlers = self._matching_handlers(event)
        if not handlers:
            if self.on_unhandled is not None:
                try:
                    self._invoke(self.on_unhandled, event)
                except Exception as callback_error:
                    with self._dead_letters_lock:
                        self._callback_errors.append(callback_error)
                    self._dead_letter(event)
                    return _DispatchOutcome("failed", callback_error)
                return _DispatchOutcome("acked")
            else:
                error = LookupError(f"Aucun handler enregistré pour {event.type}")
                self._dead_letter(event)
                return _DispatchOutcome("failed", error)

        event.attempts += 1
        try:
            for handler in handlers:
                self._invoke(handler, event)
        except Exception as exc:
            if event.attempts < event.max_attempts:
                delay = self.retry_delay * (self.retry_backoff ** (event.attempts - 1))
                if delay:
                    time.sleep(delay)
                if requeue:
                    try:
                        self.queue.put_nowait(event)
                    except Full as retry_exc:
                        self._dead_letter(event)
                        self._invoke_error_callback_safely(self.on_error, event, retry_exc)
                        return _DispatchOutcome("failed", retry_exc)
                return _DispatchOutcome("retry", exc)
            self._dead_letter(event)
            self._invoke_error_callback_safely(self.on_error, event, exc)
            return _DispatchOutcome("failed", exc)
        return _DispatchOutcome("acked")

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
        metadata = dict(event.metadata)
        # Transport observation time is volatile across adapter replay/restart
        # and must not turn a stable message/idempotency identity into a
        # content conflict. Keep all other metadata bound to the fingerprint.
        metadata.pop("received_at", None)
        body = {
            "type": event.type,
            "payload": event.payload,
            "priority": event.priority,
            "source": event.source,
            "metadata": metadata,
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

    def close(self) -> None:
        """Stop workers and close the optional durable inbox connection."""
        if self._running or self._threads:
            with self._lifecycle_lock:
                drain = self._drain_on_stop
            self.stop(wait=True, drain=drain)
            # stop(wait=True, drain=False) intentionally has a bounded wait.
            # close(), however, owns the SQLite lifetime and must never close
            # it underneath a callback that is still completing its fenced ACK.
            current = threading.current_thread()
            with self._lifecycle_lock:
                threads = list(self._threads)
            for thread in threads:
                if thread is not current:
                    thread.join()
            with self._lifecycle_lock:
                self._threads = [thread for thread in self._threads if thread.is_alive()]
                if self._threads:
                    raise RuntimeError("Impossible de fermer EventHandler depuis un worker actif.")
        with self._durable_lock:
            store = self._durable_store
            self._durable_store = None
        if store is not None:
            store.close()

    def __enter__(self) -> EventHandler:
        self.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


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
