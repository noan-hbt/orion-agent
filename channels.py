"""Abstraction multi-channel d'Orion.

Les adaptateurs traduisent leur protocole en ``InboundMessage`` et ne
connaissent pas le Core. Le router transforme ensuite les messages entrants en
evenements et distribue les sorties vers l'adaptateur cible.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from event_handler import Event, EventHandler, EventPriority, EventType


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


@dataclass(frozen=True)
class AgentOutput:
    """Sortie du Core a router vers un channel."""

    content: str
    channel: str | None = None
    recipient: str | None = None
    event_id: str | None = None
    task_id: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


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
            self._adapters[name] = adapter
            if self._running:
                adapter.start(self.receive)
        return self

    def unregister(self, name: str) -> None:
        with self._lock:
            adapter = self._adapters.pop(name, None)
        if adapter is not None and self._running:
            adapter.stop()

    def receive(self, message: InboundMessage) -> Event:
        """Publie un message normalise dans la file d'evenements."""
        if not isinstance(message, InboundMessage):
            raise TypeError("Le router attend un InboundMessage.")
        metadata = {
            **message.metadata,
            "channel": message.channel,
        }
        if message.reply_to is not None:
            metadata["reply_to"] = message.reply_to
        return self.event_handler.publish(
            message.event_type,
            message.payload,
            priority=message.priority,
            source=message.source or message.channel,
            metadata=metadata,
        )

    def route(self, output: AgentOutput) -> Any:
        """Envoie une sortie au channel demande par l'evenement ou par defaut."""
        if not isinstance(output, AgentOutput):
            raise TypeError("Le router attend un AgentOutput.")
        channel = output.channel or output.metadata.get("channel") or self.default_channel
        if not channel:
            raise RuntimeError("Aucun channel cible pour la sortie Orion.")
        with self._lock:
            adapter = self._adapters.get(str(channel))
        if adapter is None:
            raise KeyError(f"Channel non configure : {channel}")
        return adapter.send(output)

    def start(self) -> ChannelRouter:
        with self._lock:
            if self._running:
                return self
            self._running = True
            adapters = list(self._adapters.values())
        for adapter in adapters:
            adapter.start(self.receive)
        return self

    def stop(self) -> None:
        with self._lock:
            self._running = False
            adapters = list(self._adapters.values())
        for adapter in adapters:
            adapter.stop()


__all__ = ["AgentOutput", "ChannelAdapter", "ChannelRouter", "InboundMessage"]
