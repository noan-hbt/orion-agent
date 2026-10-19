"""Outbox: every message that leaves Orion is journaled before it is sent.

    outbox.queued → (outbox.delivered | outbox.failed)

The outbox id is the idempotency key given to the channel, so a send interrupted by a crash
is retried on restart with the same key and the provider never delivers it twice. A send whose
outcome is a definite refusal (4xx) is not retried.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import structlog

from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner

log = structlog.get_logger(__name__)

QUEUED = "outbox.queued"
DELIVERED = "outbox.delivered"
FAILED = "outbox.failed"


class SendError(RuntimeError):
    def __init__(self, message: str, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class Transport(Protocol):
    """A channel's sending side. Must honour the idempotency key."""

    async def deliver(self, to: str, text: str, idempotency_key: str) -> str:
        """Send `text` to `to`; return the provider's message id."""
        ...


@dataclass(frozen=True)
class Pending:
    outbox_id: str
    channel: str
    to: str
    text: str
    correlation_id: str | None


class PendingProjection:
    """Sends queued but not settled: what to retry after a crash."""

    name = "outbox"

    def __init__(self) -> None:
        self.pending: dict[str, Pending] = {}
        self._seq = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.pending.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        if event.redacted:  # forgotten: as if it never happened
            self._seq = event.seq
            return
        payload: dict[str, Any] = event.payload
        if event.kind == QUEUED:
            outbox_id = str(payload["outbox_id"])
            self.pending[outbox_id] = Pending(
                outbox_id,
                str(payload["channel"]),
                str(payload["to"]),
                str(payload["text"]),
                event.correlation_id,
            )
        elif event.kind in (DELIVERED, FAILED):
            self.pending.pop(str(payload["outbox_id"]), None)
        self._seq = event.seq


class Outbox:
    def __init__(self, journal: EventStore, transports: dict[str, Transport]) -> None:
        self._journal = journal
        self._transports = transports
        self.projection = PendingProjection()
        self._runner = ProjectionRunner(journal, [self.projection])

    async def send(
        self,
        channel: str,
        to: str,
        text: str,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> str:
        """Queue, send, record the outcome. Returns the provider's message id."""
        outbox_id = f"o_{new_id()}"
        await self._append(
            QUEUED,
            {"outbox_id": outbox_id, "channel": channel, "to": to, "text": text},
            correlation_id,
            causation_id,
        )
        return await self._deliver(Pending(outbox_id, channel, to, text, correlation_id))

    async def resume(self) -> int:
        """Retry sends left unsettled by a crash, with their original idempotency keys."""
        await self._runner.catch_up()
        resumed = 0
        for item in list(self.projection.pending.values()):
            log.warning("outbox.resume", outbox_id=item.outbox_id, channel=item.channel)
            try:
                await self._deliver(item)
                resumed += 1
            except SendError:
                log.exception("outbox.resume_failed", outbox_id=item.outbox_id)
        return resumed

    async def _deliver(self, item: Pending) -> str:
        transport = self._transports.get(item.channel)
        if transport is None:
            await self._settle(FAILED, item, error=f"no transport for {item.channel}")
            raise SendError(f"no transport for channel {item.channel!r}", retryable=False)
        try:
            remote_id = await transport.deliver(item.to, item.text, item.outbox_id)
        except SendError as exc:
            if not exc.retryable:
                await self._settle(FAILED, item, error=str(exc))
            raise
        await self._settle(DELIVERED, item, remote_id=remote_id)
        return remote_id

    async def _settle(
        self, kind: str, item: Pending, remote_id: str | None = None, error: str | None = None
    ) -> None:
        payload: dict[str, object] = {"outbox_id": item.outbox_id, "channel": item.channel}
        if remote_id is not None:
            payload["remote_id"] = remote_id
        if error is not None:
            payload["error"] = error
        await self._append(kind, payload, item.correlation_id, None)

    async def _append(
        self,
        kind: str,
        payload: dict[str, object],
        correlation_id: str | None,
        causation_id: str | None,
    ) -> None:
        await self._journal.append(
            [
                NewEvent(
                    kind=kind,
                    payload=payload,
                    provenance=Provenance(source="orion:outbox", actor="orion:guardian"),
                    trust=Trust.SYSTEM,
                    correlation_id=correlation_id or new_id(),
                    causation_id=causation_id,
                )
            ]
        )
        await self._runner.catch_up()
