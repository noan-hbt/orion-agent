"""An iMessage line: what the iMessage channel needs from whoever carries the messages (D88).

Inkbox is one provider; Blooio, BlueBubbles (a Mac of one's own) or another would be another
class with these methods, chosen by `[channels.imessage] provider`. The channel itself (who is
the owner, bubbles, approvals, tapbacks, requeues) stays the same. A provider maps its answers
to the models below (they came from Inkbox, and say nothing Inkbox-specific).
"""

from __future__ import annotations

from typing import Protocol

from orion.channels.inkbox import IMessage, LineError

__all__ = ["IMessage", "IMessageLine", "LineError"]


class IMessageLine(Protocol):
    async def unread(self, identity_id: str | None = None, limit: int = 50) -> list[IMessage]:
        """Inbound messages not yet marked read, for polling."""
        ...

    async def recent(
        self, conversation_id: str, identity_id: str | None = None, limit: int = 20
    ) -> list[IMessage]:
        """Latest messages of a conversation, newest first (tapbacks are read on them)."""
        ...

    async def message(self, message_id: str, identity_id: str | None = None) -> IMessage:
        """One message, with fresh links to its attachments."""
        ...

    async def send(
        self,
        conversation_id: str,
        text: str,
        idempotency_key: str,
        identity_id: str | None = None,
        media_url: str | None = None,
    ) -> IMessage:
        """Must honour the idempotency key: a retried send is delivered once."""
        ...

    async def upload(self, name: str, data: bytes, content_type: str) -> str:
        """A file kept for a send; returns its URL for `send`."""
        ...

    async def typing(self, conversation_id: str) -> None: ...

    async def mark_read(self, conversation_id: str) -> None: ...
