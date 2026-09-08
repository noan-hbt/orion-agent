"""Channel-scoped resolution of external identities.

Identifiers are deliberately namespaced by channel and scope.  In particular,
the same Telegram/Discord (or other) identifier can never accidentally point
at the same object, and group conversations are never inferred from a DM.
"""
from __future__ import annotations

from threading import RLock
from typing import Any
from context_registry import Principal, ConversationThread


class IdentityResolver:
    """Resolve and explicitly link channel identities.

    ``registry`` is optional and may implement ``get(key)`` and ``set(key,
    value)``.  The resolver remains usable with the in-memory registry when no
    compatible durable registry is supplied.
    """

    def __init__(self, registry: Any | None = None) -> None:
        self.registry = registry
        self._principals: dict[tuple[str, str, str], Principal] = {}
        self._threads: dict[tuple[str, str, str, str | None, str], ConversationThread] = {}
        self._links: dict[tuple[str, str], str] = {}
        self._lock = RLock()

    @staticmethod
    def _part(value: Any, name: str) -> str:
        value = str(value).strip() if value is not None else ""
        if not value:
            raise ValueError(f"{name} is required")
        return value

    def resolve_principal(self, channel: str, scope: str, external_user_id: Any) -> Principal:
        key = (self._part(channel, "channel"), self._part(scope, "scope"), self._part(external_user_id, "external_user_id"))
        with self._lock:
            if key not in self._principals:
                ident = "principal:" + ":".join(key)
                value = Principal(ident, key[1], {"channel": key[0], "external_user_id": key[2]})
                self._principals[key] = value
                if self.registry and callable(getattr(self.registry, "upsert_principal", None)):
                    self.registry.upsert_principal(value)
            return self._principals[key]

    def resolve_thread(self, channel: str, scope: str, external_chat_id: Any,
                       external_thread_id: Any = None, *, kind: str = "group") -> ConversationThread:
        channel, scope, chat = (self._part(channel, "channel"), self._part(scope, "scope"), self._part(external_chat_id, "external_chat_id"))
        thread = None if external_thread_id is None else self._part(external_thread_id, "external_thread_id")
        kind = self._part(kind, "kind").lower()
        if kind not in {"group", "dm"}:
            raise ValueError("kind must be 'group' or 'dm'")
        key = (channel, scope, chat, thread, kind)
        with self._lock:
            if key not in self._threads:
                suffix = ":".join((channel, scope, chat, thread or "root", kind))
                ident = "thread:" + suffix
                value = ConversationThread(ident, None, scope, {
                    "channel": channel, "external_chat_id": chat,
                    "external_thread_id": thread, "kind": kind,
                })
                self._threads[key] = value
                if self.registry and callable(getattr(self.registry, "upsert_thread", None)):
                    self.registry.upsert_thread(value)
            return self._threads[key]

    def link(self, principal: Principal, thread: ConversationThread) -> None:
        p_channel = principal.data.get("channel")
        t_channel = thread.data.get("channel")
        if (p_channel, principal.scope) != (t_channel, thread.scope):
            raise ValueError("principal and thread must share channel and scope")
        with self._lock:
            self._links[(principal.id, thread.id)] = thread.id
            if self.registry and callable(getattr(self.registry, "bind_channel", None)):
                self.registry.bind_channel(p_channel, thread.data["external_chat_id"], scope=principal.scope,
                                           principal_id=principal.id, thread_id=thread.id,
                                           data={"external_thread_id": thread.data.get("external_thread_id"), "kind": thread.data.get("kind")})

    def resolve(self, *, channel: str, scope: str, external_user_id: Any,
                external_chat_id: Any, external_thread_id: Any = None,
                kind: str = "group") -> tuple[Principal, ConversationThread]:
        principal = self.resolve_principal(channel, scope, external_user_id)
        thread = self.resolve_thread(channel, scope, external_chat_id, external_thread_id, kind=kind)
        self.link(principal, thread)
        return principal, thread


IdentityRegistry = IdentityResolver

__all__ = ["Principal", "ConversationThread", "IdentityResolver", "IdentityRegistry"]
