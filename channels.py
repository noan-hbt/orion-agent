"""Abstraction multi-channel d'Orion.

Les adaptateurs traduisent leur protocole en ``InboundMessage`` et ne
connaissent pas le Core. Le router transforme ensuite les messages entrants en
evenements et distribue les sorties vers l'adaptateur cible.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import uuid
import hashlib
from typing import Any, Protocol

from communication_ledger import CommunicationLedger
from event_handler import Event, EventHandler, EventPriority, EventType


class ChannelLifecycleError(RuntimeError):
    """Un adaptateur n'a pas pu démarrer ou s'arrêter proprement."""

    status_code = 503


@dataclass(frozen=True)
class Principal:
    """Identité canonique d'un interlocuteur, indépendante du protocole."""
    id: str
    channel: str | None = None
    display_name: str | None = None


@dataclass(frozen=True)
class ConversationThread:
    """Conversation et sous-fil (notamment les topics Telegram)."""
    id: str
    conversation_id: str
    channel: str | None = None
    message_thread_id: str | None = None


@dataclass(frozen=True)
class IntentState:
    """État d'intention transporté avec un message ou une sortie."""
    name: str | None = None
    status: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


def canonical_id(kind: str, channel: str, native_id: Any) -> str:
    """Construit un identifiant stable et sûr pour les handoffs cross-canal."""
    value = f"{channel}:{native_id}"
    return f"{kind}:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"


@dataclass(frozen=True)
class InboundMessage:
    """Message normalise emis par un adaptateur de channel."""

    channel: str
    payload: dict[str, Any]
    event_type: str = EventType.MESSAGE.value
    reply_to: str | None = None
    source: str | None = None
    priority: int = int(EventPriority.NORMAL)
    metadata: dict[str, Any] = field(default_factory=dict)
    message_id: str | None = None
    sender: str | None = None
    text: str | None = None
    received_at: datetime | None = None
    correlation_id: str | None = None
    conversation_id: str | None = None
    user_id: str | None = None
    message_thread_id: str | None = None
    thread_id: str | None = None
    parent_message_id: str | None = None
    principal: Principal | None = None
    conversation: ConversationThread | None = None
    intent: IntentState | None = None

    def __post_init__(self) -> None:
        # Les anciens adaptateurs mettent parfois l'identifiant dans payload.
        if self.message_id is None:
            candidate = self.payload.get("message_id")
            if candidate is not None:
                object.__setattr__(self, "message_id", str(candidate))
            else:
                object.__setattr__(self, "message_id", uuid.uuid4().hex)
        if self.received_at is None:
            object.__setattr__(self, "received_at", datetime.now(timezone.utc))
        if self.correlation_id is None:
            candidate = self.metadata.get("correlation_id")
            if candidate is not None:
                object.__setattr__(self, "correlation_id", str(candidate))
        for name in ("conversation_id", "user_id", "message_thread_id", "thread_id", "parent_message_id"):
            if getattr(self, name) is None:
                candidate = self.metadata.get(name) or self.payload.get(name)
                if candidate is not None:
                    object.__setattr__(self, name, str(candidate))
        if self.principal is None and self.user_id is not None:
            object.__setattr__(self, "principal", Principal(canonical_id("principal", self.channel, self.user_id), self.channel))
        if self.conversation is None and self.conversation_id is not None:
            object.__setattr__(self, "conversation", ConversationThread(canonical_id("conversation", self.channel, self.conversation_id), self.conversation_id, self.channel, self.message_thread_id))


@dataclass(frozen=True)
class AgentOutput:
    """Sortie du Core a router vers un channel."""

    content: str
    channel: str | None = None
    recipient: str | None = None
    event_id: str | None = None
    task_id: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    output_id: str | None = None
    correlation_id: str | None = None
    idempotency_key: str | None = None
    text: str | None = None
    conversation_id: str | None = None
    user_id: str | None = None
    message_thread_id: str | None = None
    thread_id: str | None = None
    parent_message_id: str | None = None
    principal: Principal | None = None
    conversation: ConversationThread | None = None
    intent: IntentState | None = None

    def __post_init__(self) -> None:
        if self.output_id is None:
            object.__setattr__(
                self, "output_id", self.event_id or uuid.uuid4().hex
            )
        if self.text is None:
            object.__setattr__(self, "text", self.content)
        elif not self.content:
            object.__setattr__(self, "content", self.text)
        if self.correlation_id is None:
            candidate = self.metadata.get("correlation_id")
            if candidate is not None:
                object.__setattr__(self, "correlation_id", str(candidate))
        if self.idempotency_key is None:
            candidate = self.metadata.get("idempotency_key")
            if candidate is not None:
                object.__setattr__(self, "idempotency_key", str(candidate))
        for name in ("conversation_id", "user_id", "message_thread_id", "thread_id", "parent_message_id"):
            if getattr(self, name) is None:
                candidate = self.metadata.get(name)
                if candidate is not None:
                    object.__setattr__(self, name, str(candidate))
        if self.principal is None and self.user_id is not None:
            object.__setattr__(self, "principal", Principal(canonical_id("principal", self.channel or str(self.metadata.get("channel", "unknown")), self.user_id), self.channel))
        if self.conversation is None and self.conversation_id is not None:
            channel = self.channel or str(self.metadata.get("channel", "unknown"))
            object.__setattr__(self, "conversation", ConversationThread(canonical_id("conversation", channel, self.conversation_id), self.conversation_id, channel, self.message_thread_id))


class ChannelAdapter(Protocol):
    """Contrat minimal pour Telegram, web, CLI, email, Discord, etc."""

    name: str

    def start(self, on_message: Callable[[InboundMessage], None]) -> None:
        ...

    def stop(self) -> None:
        ...

    def send(self, output: AgentOutput) -> Any:
        ...


class ChannelRouter:
    """Pont entre adaptateurs, EventHandler et sorties du runtime."""

    def __init__(
        self,
        event_handler: EventHandler,
        *,
        default_channel: str | None = None,
        ledger: CommunicationLedger | None = None,
        outbound_max_attempts: int = 8,
        outbound_retry_backoff: float = 0.5,
        outbound_poll_interval: float = 0.2,
        outbound_lease_seconds: float = 60.0,
    ) -> None:
        if outbound_max_attempts < 1:
            raise ValueError("outbound_max_attempts doit être positif.")
        if outbound_retry_backoff < 0 or outbound_poll_interval <= 0 or outbound_lease_seconds <= 0:
            raise ValueError("Les délais de livraison outbound sont invalides.")
        self.event_handler = event_handler
        self.default_channel = default_channel
        # The application owns the ledger connection.  The router only uses it
        # as a durable outbox and never closes it, which lets runtime shutdown
        # persist late outputs even after channel transports have quiesced.
        self.ledger = ledger
        self.outbound_max_attempts = int(outbound_max_attempts)
        self.outbound_retry_backoff = float(outbound_retry_backoff)
        self.outbound_poll_interval = float(outbound_poll_interval)
        self.outbound_lease_seconds = float(outbound_lease_seconds)
        self._adapters: dict[str, ChannelAdapter] = {}
        self._lock = threading.RLock()
        self._running = False
        self._outbound_stop = threading.Event()
        self._outbound_wakeup = threading.Event()
        self._outbound_worker: threading.Thread | None = None
        self._outbound_worker_id = f"channel-outbound-{uuid.uuid4().hex}"

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def adapters(self) -> dict[str, ChannelAdapter]:
        with self._lock:
            return dict(self._adapters)

    def register(self, adapter: ChannelAdapter) -> ChannelRouter:
        name = str(getattr(adapter, "name", "")).strip()
        if not name:
            raise ValueError("Un adaptateur doit definir un nom.")
        if not callable(getattr(adapter, "send", None)):
            raise TypeError("Un adaptateur doit exposer send().")
        if not callable(getattr(adapter, "start", None)) or not callable(getattr(adapter, "stop", None)):
            raise TypeError("Un adaptateur doit exposer start() et stop().")
        with self._lock:
            previous = self._adapters.get(name)
            self._adapters[name] = adapter
            running = self._running
        if running:
            try:
                adapter.start(self.receive)
            except Exception:
                with self._lock:
                    if self._adapters.get(name) is adapter:
                        if previous is None:
                            self._adapters.pop(name, None)
                        else:
                            self._adapters[name] = previous
                raise ChannelLifecycleError(f"Impossible de démarrer le channel : {name}") from None
            if previous is not None and previous is not adapter:
                try:
                    previous.stop()
                except Exception:
                    pass
        return self

    def unregister(self, name: str) -> None:
        with self._lock:
            adapter = self._adapters.pop(name, None)
        if adapter is not None and self._running:
            adapter.stop()

    @staticmethod
    def _event_message_id(message: InboundMessage) -> str | None:
        """Return the EventHandler identity for one provider-native message.

        Native message ids are only unique inside their provider namespace
        (for example Telegram ``42`` and email ``42`` are unrelated).  Keep
        that native value untouched on ``InboundMessage`` and in event metadata,
        but use a stable channel/source-scoped identity at the EventHandler
        boundary so its global ``message_id`` dedupe domain cannot conflate
        different providers.

        This deliberately does not synthesize or rewrite an idempotency key:
        explicit idempotency semantics remain a separate, caller-owned domain.
        """
        if message.message_id is None:
            return None
        parts = (
            str(message.channel).encode("utf-8"),
            str(message.source or message.channel).encode("utf-8"),
            str(message.message_id).encode("utf-8"),
        )
        material = b"".join(len(part).to_bytes(8, "big") + part for part in parts)
        return "channel-message:" + hashlib.sha256(material).hexdigest()

    def receive(self, message: InboundMessage, *, timeout: float | None = None) -> Event:
        """Publie un message normalise dans la file d'evenements."""
        if not isinstance(message, InboundMessage):
            raise TypeError("Le router attend un InboundMessage.")
        metadata = {
            **message.metadata,
            "channel": message.channel,
        }
        if message.reply_to is not None:
            metadata["reply_to"] = message.reply_to
        if message.message_id is not None:
            metadata["message_id"] = message.message_id
        if message.sender is not None:
            metadata["sender"] = message.sender
        if message.text is not None:
            metadata["text"] = message.text
        if message.received_at is not None:
            # EventHandler durable intentionally rejects non-JSON values.
            # InboundMessage, however, owns a few rich transport types
            # (datetime/dataclasses). Normalize only those known fields here
            # rather than weakening the durable store with a generic
            # ``default=str`` fallback that could hide invalid adapter data.
            metadata["received_at"] = message.received_at.isoformat()
        if message.correlation_id is not None:
            metadata["correlation_id"] = message.correlation_id
        for name in ("conversation_id", "user_id", "message_thread_id", "thread_id", "parent_message_id"):
            value = getattr(message, name)
            if value is not None:
                metadata[name] = value
        for name in ("principal", "conversation", "intent"):
            value = getattr(message, name)
            if value is not None:
                metadata[name] = asdict(value)
        return self.event_handler.publish(
            message.event_type,
            message.payload,
            priority=message.priority,
            source=message.source or message.channel,
            metadata=metadata,
            timeout=timeout,
            message_id=self._event_message_id(message),
            correlation_id=message.correlation_id,
        )

    def route(self, output: AgentOutput) -> Any:
        """Durably accept a runtime output before transport delivery.

        With a communication ledger, transport is deliberately detached from
        the runtime call stack: ``route`` commits the output first and the
        channel worker later claims/sends/ACKs it.  A transient SMTP/Telegram
        or HTTP failure therefore retries the transport, not the model run.

        The no-ledger path remains synchronous for legacy direct unit tests and
        embedders.  Remote transports without native idempotency remain
        at-least-once: a crash after the remote side accepted a message but
        before the local ACK can still produce a duplicate after recovery.
        """
        if not isinstance(output, AgentOutput):
            raise TypeError("Le router attend un AgentOutput.")
        channel = output.channel or output.metadata.get("channel")
        if not channel and output.conversation_id:
            channel = self._channel_for_conversation(output.conversation_id)
        channel = channel or self.default_channel
        if not channel:
            raise RuntimeError("Aucun channel cible pour la sortie Orion.")
        with self._lock:
            adapter = self._adapters.get(str(channel))
        if adapter is None:
            raise ChannelLifecycleError(f"Channel non configure : {channel}")
        if self.ledger is not None:
            payload = self._serialize_output(output)
            fingerprint_payload = self._fingerprint_output_payload(payload)
            delivery_id = self._delivery_identity(output)
            row_id, _is_new = self.ledger.record_outbound(
                channel=str(channel),
                payload=payload,
                output_id=delivery_id,
                idempotency_key=delivery_id,
                correlation_id=output.correlation_id,
                reply_to=output.recipient or output.metadata.get("reply_to"),
                max_attempts=self.outbound_max_attempts,
                fingerprint_payload=fingerprint_payload,
            )
            # Delivered duplicates are intentionally only re-observed in the
            # ledger.  claim(kind='outbound') cannot select them, so replaying
            # a runtime event does not send the same locally-ACKed output twice.
            self._outbound_wakeup.set()
            return row_id
        try:
            return adapter.send(output)
        except (TimeoutError, ConnectionError) as exc:
            raise ChannelLifecycleError(f"Channel indisponible : {channel}") from exc

    @staticmethod
    def _delivery_identity(output: AgentOutput) -> str:
        """Return a stable, non-secret id for one logical output slot.

        Runtime outputs historically reuse the inbound ``event_id`` as their
        ``output_id``.  Final and intermediate messages must therefore be
        namespaced separately.  Explicit output/idempotency ids remain the
        strongest identity.  Intermediate outputs without a sequence use their
        phase/content digest so multiple tool-progress messages do not collide.
        """
        if output.idempotency_key:
            logical = f"idempotency\0{output.idempotency_key}"
        elif output.event_id and output.output_id == output.event_id:
            if bool(output.metadata.get("intermediate", False)):
                sequence = output.metadata.get("seq", output.metadata.get("sequence"))
                if sequence is not None:
                    slot = f"seq:{sequence}"
                else:
                    phase = str(output.metadata.get("phase") or "progress")
                    content_digest = hashlib.sha256(
                        output.content.encode("utf-8", "replace")
                    ).hexdigest()
                    slot = f"{phase}:{content_digest}"
                logical = f"event\0{output.event_id}\0intermediate\0{slot}"
            else:
                logical = f"event\0{output.event_id}\0final"
        else:
            logical = f"output\0{output.output_id}"
        return "orion-outbound:" + hashlib.sha256(logical.encode("utf-8")).hexdigest()

    @staticmethod
    def _serialize_output(output: AgentOutput) -> dict[str, Any]:
        return {
            "version": 1,
            "content": output.content,
            "recipient": output.recipient,
            "event_id": output.event_id,
            "task_id": output.task_id,
            "metadata": dict(output.metadata),
            "output_id": output.output_id,
            "correlation_id": output.correlation_id,
            "text": output.text,
            "conversation_id": output.conversation_id,
            "user_id": output.user_id,
            "message_thread_id": output.message_thread_id,
            "thread_id": output.thread_id,
            "parent_message_id": output.parent_message_id,
            "principal": asdict(output.principal) if output.principal is not None else None,
            "conversation": asdict(output.conversation) if output.conversation is not None else None,
            "intent": asdict(output.intent) if output.intent is not None else None,
        }

    @staticmethod
    def _fingerprint_output_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
        """Remove display-only volatility from idempotency comparison."""
        canonical = dict(payload)
        metadata = canonical.get("metadata")
        if isinstance(metadata, Mapping):
            normalized_metadata = dict(metadata)
            # Runtime generates this timestamp immediately before route().  A
            # crash/replay of the same logical output must not conflict solely
            # because the display timestamp changed.
            normalized_metadata.pop("timestamp", None)
            canonical["metadata"] = normalized_metadata
        return canonical

    @staticmethod
    def _mapping_dataclass(value: Any, cls: type[Any]) -> Any:
        if not isinstance(value, Mapping):
            return None
        try:
            return cls(**dict(value))
        except (TypeError, ValueError):
            return None

    @classmethod
    def _deserialize_output(cls, row: Mapping[str, Any]) -> AgentOutput:
        payload = row.get("payload")
        if not isinstance(payload, Mapping) or payload.get("version") != 1:
            raise ValueError("invalid durable outbound payload")
        metadata = payload.get("metadata")
        if not isinstance(metadata, Mapping):
            raise ValueError("invalid durable outbound metadata")
        return AgentOutput(
            content=str(payload.get("content") or ""),
            channel=str(row.get("channel") or ""),
            recipient=(str(payload["recipient"]) if payload.get("recipient") is not None else None),
            event_id=(str(payload["event_id"]) if payload.get("event_id") is not None else None),
            task_id=(int(payload["task_id"]) if payload.get("task_id") is not None else None),
            metadata=dict(metadata),
            output_id=(str(payload["output_id"]) if payload.get("output_id") is not None else None),
            correlation_id=(str(payload["correlation_id"]) if payload.get("correlation_id") is not None else None),
            # Expose the durable delivery identity to transports that can pass
            # an idempotency token downstream (notably HTTP webhooks).
            idempotency_key=str(row.get("idempotency_key") or row.get("id") or ""),
            text=(str(payload["text"]) if payload.get("text") is not None else None),
            conversation_id=(str(payload["conversation_id"]) if payload.get("conversation_id") is not None else None),
            user_id=(str(payload["user_id"]) if payload.get("user_id") is not None else None),
            message_thread_id=(str(payload["message_thread_id"]) if payload.get("message_thread_id") is not None else None),
            thread_id=(str(payload["thread_id"]) if payload.get("thread_id") is not None else None),
            parent_message_id=(str(payload["parent_message_id"]) if payload.get("parent_message_id") is not None else None),
            principal=cls._mapping_dataclass(payload.get("principal"), Principal),
            conversation=cls._mapping_dataclass(payload.get("conversation"), ConversationThread),
            intent=cls._mapping_dataclass(payload.get("intent"), IntentState),
        )

    def _lease_heartbeat(self, row_id: str, done: threading.Event) -> None:
        ledger = self.ledger
        if ledger is None:
            return
        interval = max(0.01, min(5.0, self.outbound_lease_seconds / 3.0))
        while not done.wait(interval):
            try:
                if not ledger.renew_lease(
                    row_id,
                    worker_id=self._outbound_worker_id,
                    lease_seconds=self.outbound_lease_seconds,
                ):
                    return
            except Exception:
                # Losing the heartbeat never authorizes an ACK.  The fenced
                # lease will expire and another worker can conservatively retry.
                return

    def _deliver_claimed(self, row: Mapping[str, Any]) -> None:
        ledger = self.ledger
        if ledger is None:
            return
        row_id = str(row.get("id") or "")
        channel = str(row.get("channel") or "")
        if not row_id or not channel:
            return
        try:
            if not ledger.mark_processing(row_id, worker_id=self._outbound_worker_id):
                return
            output = self._deserialize_output(row)
        except Exception as exc:
            # Persist only the exception class, never transport/config strings
            # that could contain URLs, credentials or message content.
            ledger.fail(
                row_id,
                type(exc).__name__,
                worker_id=self._outbound_worker_id,
                backoff=self.outbound_retry_backoff,
            )
            return
        with self._lock:
            adapter = self._adapters.get(channel)
        if adapter is None:
            ledger.fail(
                row_id,
                "ChannelUnavailable",
                worker_id=self._outbound_worker_id,
                backoff=self.outbound_retry_backoff,
            )
            return

        heartbeat_done = threading.Event()
        heartbeat = threading.Thread(
            target=self._lease_heartbeat,
            args=(row_id, heartbeat_done),
            name="orion-channel-outbound-lease",
            daemon=True,
        )
        heartbeat.start()
        try:
            adapter.send(output)
        except Exception as exc:
            try:
                ledger.fail(
                    row_id,
                    type(exc).__name__,
                    worker_id=self._outbound_worker_id,
                    backoff=self.outbound_retry_backoff,
                )
            except Exception:
                pass
        else:
            try:
                ledger.ack(row_id, worker_id=self._outbound_worker_id)
            except Exception:
                # If ACK persistence fails, leave the leased row recoverable.
                # This can duplicate a remotely accepted message; transports
                # without native idempotency cannot eliminate that ambiguity.
                pass
        finally:
            heartbeat_done.set()
            heartbeat.join()

    def _outbound_loop(self) -> None:
        ledger = self.ledger
        if ledger is None:
            return
        try:
            ledger.recover_expired_leases()
        except Exception:
            pass
        while not self._outbound_stop.is_set():
            with self._lock:
                channels = tuple(self._adapters)
            claimed: dict[str, Any] | None = None
            for channel in channels:
                if self._outbound_stop.is_set():
                    return
                try:
                    claimed = ledger.claim(
                        worker_id=self._outbound_worker_id,
                        kind="outbound",
                        channel=channel,
                        lease_seconds=self.outbound_lease_seconds,
                    )
                except Exception:
                    claimed = None
                if claimed is not None:
                    break
            if claimed is not None:
                self._deliver_claimed(claimed)
                continue
            self._outbound_wakeup.wait(self.outbound_poll_interval)
            self._outbound_wakeup.clear()

    def _channel_for_conversation(self, conversation_id: str) -> str | None:
        """Best-effort routing from an explicitly identified conversation."""
        for name in self._adapters:
            if str(conversation_id).startswith(f"{name}:"):
                return name
        return None

    def start(self) -> ChannelRouter:
        with self._lock:
            if self._running:
                return self
            adapters = list(self._adapters.values())
        started: list[ChannelAdapter] = []
        try:
            for adapter in adapters:
                adapter.start(self.receive)
                started.append(adapter)
        except Exception as exc:
            failed = adapters[len(started)]
            try:
                failed.stop()
            except Exception:
                pass
            for adapter in reversed(started):
                try:
                    adapter.stop()
                except Exception:
                    pass
            with self._lock:
                self._running = False
            if isinstance(exc, ChannelLifecycleError):
                raise
            failed_name = getattr(adapters[len(started)], "name", "inconnu")
            raise ChannelLifecycleError(
                f"Impossible de démarrer le channel : {failed_name}"
            ) from exc
        with self._lock:
            self._running = True
            if self.ledger is not None:
                self._outbound_stop.clear()
                self._outbound_wakeup.clear()
                self._outbound_worker_id = f"channel-outbound-{uuid.uuid4().hex}"
                self._outbound_worker = threading.Thread(
                    target=self._outbound_loop,
                    name="orion-channel-outbound",
                    daemon=True,
                )
                self._outbound_worker.start()
        return self

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
            adapters = list(self._adapters.values())
            outbound_worker = self._outbound_worker
            self._outbound_worker = None
            self._outbound_stop.set()
            self._outbound_wakeup.set()
        if outbound_worker is not None and outbound_worker is not threading.current_thread():
            # Adapter transports in Orion have bounded I/O timeouts.  Wait for
            # the sender to leave its ledger lease before the application can
            # eventually close the shared SQLite connection.
            outbound_worker.join()
        for adapter in adapters:
            try:
                adapter.stop()
            except Exception:
                # L'arrêt est quiescent et idempotent : un adaptateur fautif
                # ne doit pas empêcher les autres de libérer leurs ressources.
                continue


__all__ = [
    "AgentOutput",
    "ChannelAdapter",
    "ChannelLifecycleError",
    "ChannelRouter",
    "InboundMessage",
    "Principal", "ConversationThread", "IntentState", "canonical_id",
]
