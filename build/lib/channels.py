"""Abstraction multi-channel d'Orion.

Les adaptateurs traduisent leur protocole en ``InboundMessage`` et ne
connaissent pas le Core. Le router transforme ensuite les messages entrants en
evenements et distribue les sorties vers l'adaptateur cible.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import uuid
import hashlib
from typing import Any, Protocol

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

    def __init__(self, event_handler: EventHandler, *, default_channel: str | None = None) -> None:
        self.event_handler = event_handler
        self.default_channel = default_channel
        self._adapters: dict[str, ChannelAdapter] = {}
        self._lock = threading.RLock()
        self._running = False

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
            metadata["received_at"] = message.received_at
        if message.correlation_id is not None:
            metadata["correlation_id"] = message.correlation_id
        for name in ("conversation_id", "user_id", "message_thread_id", "thread_id", "parent_message_id"):
            value = getattr(message, name)
            if value is not None:
                metadata[name] = value
        for name in ("principal", "conversation", "intent"):
            value = getattr(message, name)
            if value is not None:
                metadata[name] = value
        return self.event_handler.publish(
            message.event_type,
            message.payload,
            priority=message.priority,
            source=message.source or message.channel,
            metadata=metadata,
            timeout=timeout,
            message_id=message.message_id,
            correlation_id=message.correlation_id,
        )

    def route(self, output: AgentOutput) -> Any:
        """Envoie une sortie au channel demande par l'evenement ou par defaut."""
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
        try:
            return adapter.send(output)
        except (TimeoutError, ConnectionError) as exc:
            raise ChannelLifecycleError(f"Channel indisponible : {channel}") from exc

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
        return self

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
            adapters = list(self._adapters.values())
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
