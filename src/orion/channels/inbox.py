"""The owner's unified inbox: every channel drops messages here, one worker answers them.

Channels only translate (receive, deduplicate, journal; deliver). The inbox owns everything
that is the same whatever the channel:

- one queue, one worker, so turns on the owner's single thread never interleave;
- bursts of messages from the same channel are coalesced into one turn;
- messages that arrive while Orion works join the running turn between two steps, so it can
  change course ("non, laisse tomber") instead of finishing a plan the owner revoked;
- while Orion thinks, the channel shows it is working (read receipt, typing indicator);
- the reply goes back through the outlet the message asked for, unless Orion answers
  `SILENCE` (used for call transcripts that need no follow-up);
- proactive messages go to the first available outlet, in order of preference.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Protocol

import structlog

from orion.cognition.loop import StopReason
from orion.runtime import OWNER_THREAD, Interjection, Orion, TurnResult, is_silence

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Inbound:
    channel: str
    """Shown to the model in the message's context line (e.g. 'imessage', 'appel')."""
    message_ids: tuple[str, ...]
    """The channel's own ids, recorded so a restart knows the message was handled."""
    text: str
    reply_via: str | None = None
    """Outlet that answers; None means the channel itself."""
    allow_silence: bool = False
    """The message needs no answer unless Orion has something to add."""
    context: str = field(default="", compare=False)
    """Opaque per-channel data, e.g. the conversation to show typing in."""


class Outlet(Protocol):
    """A channel's owner-facing side."""

    name: str

    async def available(self) -> bool: ...

    async def deliver(self, text: str, correlation_id: str | None) -> None: ...

    def working(self, item: Inbound) -> AbstractAsyncContextManager[None]:
        """Shown while Orion handles `item`: read receipt, typing indicator…"""
        ...


@asynccontextmanager
async def nothing_to_show() -> AsyncGenerator[None]:
    yield


class OwnerInbox:
    def __init__(
        self,
        orion: Orion,
        coalesce_seconds: float = 1.5,
        on_turn: Callable[[TurnResult], Awaitable[None]] | None = None,
    ) -> None:
        self._orion = orion
        self._coalesce = coalesce_seconds
        self._on_turn = on_turn
        self._outlets: dict[str, Outlet] = {}
        self._queue: asyncio.Queue[Inbound] = asyncio.Queue()
        self._held: deque[Inbound] = deque()
        """Taken off the queue but kept for a later turn, in arrival order."""
        self._background: set[asyncio.Task[None]] = set()

    def register(self, outlet: Outlet) -> None:
        """Outlets are tried for proactive messages in registration order."""
        self._outlets[outlet.name] = outlet

    def submit(self, item: Inbound) -> None:
        self._queue.put_nowait(item)

    # -- notifier for the initiative engine ---------------------------------------------------

    async def available(self) -> bool:
        return await self._preferred() is not None

    async def notify(self, text: str, correlation_id: str) -> None:
        outlet = await self._preferred()
        if outlet is None:
            raise RuntimeError("no owner channel is available")
        await outlet.deliver(text, correlation_id)

    async def _preferred(self) -> Outlet | None:
        for outlet in self._outlets.values():
            if await outlet.available():
                return outlet
        return None

    # -- the worker ---------------------------------------------------------------------------

    async def run(self) -> None:
        while True:
            batch = [await self._next()]
            started = time.monotonic()
            while True:
                if self._held:
                    item = self._held.popleft()
                else:
                    try:
                        item = await asyncio.wait_for(self._queue.get(), self._coalesce)
                    except TimeoutError:
                        break
                if item.channel != batch[0].channel or item.allow_silence or batch[0].allow_silence:
                    self._held.appendleft(item)  # a different kind of message: its own turn
                    break
                batch.append(item)
            await self._handle(batch, waited=time.monotonic() - started)

    async def _next(self) -> Inbound:
        if self._held:
            return self._held.popleft()
        return await self._queue.get()

    def _arrived_meanwhile(self, first: Inbound) -> list[Inbound]:
        """Messages that can join the running turn: same channel, needing an answer, and not
        behind one that cannot (order is kept). Everything else waits for its own turn."""
        while True:
            try:
                self._held.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        joined: list[Inbound] = []
        while self._held:
            item = self._held[0]
            if item.channel != first.channel or item.allow_silence:
                break
            joined.append(self._held.popleft())
        return joined

    async def _handle(self, batch: list[Inbound], waited: float = 0.0) -> None:
        first = batch[0]
        started = time.monotonic()
        source = self._outlets.get(first.channel)
        reply = self._outlets.get(first.reply_via or first.channel)
        showing = source.working(batch[-1]) if source is not None else nothing_to_show()

        async def interjections() -> list[Interjection]:
            if first.allow_silence:
                return []
            return [
                Interjection(item.text, item.channel, item.message_ids)
                for item in self._arrived_meanwhile(first)
            ]

        async with showing:
            turn = await self._orion.handle_owner_message(
                OWNER_THREAD,
                "\n".join(item.text for item in batch if item.text),
                first.channel,
                refs=[i for item in batch for i in item.message_ids],
                interjections=interjections,
            )
        thought = time.monotonic() - started
        text = turn.text.strip()
        if turn.reason is not StopReason.COMPLETED:
            log.warning("inbox.turn_incomplete", reason=turn.reason, detail=turn.detail)
            text = (text + "\n\n" if text else "") + f"(je me suis arrêté : {turn.reason.value})"
        if first.allow_silence and is_silence(text):
            log.info("inbox.silence", channel=first.channel)
        elif text and reply is not None:
            await reply.deliver(text, turn.correlation_id)
        elif text:
            log.warning("inbox.no_outlet", channel=first.reply_via or first.channel)
        log.info(
            "inbox.turn",
            channel=first.channel,
            messages=len(batch),
            joined_during_turn=turn.interjections,
            coalesce_s=round(waited, 2),
            think_s=round(thought, 2),
            steps=turn.steps,
            tools_s=turn.tool_seconds,
            send_s=round(time.monotonic() - started - thought, 2),
            prompt_tokens=turn.usage.prompt_tokens,
            cached_tokens=turn.usage.cached_tokens,
            cost_usd=round(turn.usage.cost_usd, 5),
        )
        if self._on_turn is not None:
            self._spawn(self._on_turn(turn))

    def _spawn(self, coro: Awaitable[None]) -> None:
        async def guarded() -> None:
            try:
                await coro
            except Exception:
                log.exception("inbox.background_failed")

        task = asyncio.create_task(guarded())
        self._background.add(task)
        task.add_done_callback(self._background.discard)
