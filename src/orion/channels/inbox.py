"""The owner's unified inbox: every channel drops messages here, one worker answers them.

Channels only translate (receive, deduplicate, journal; deliver). The inbox owns everything
that is the same whatever the channel:

- one queue, one worker, so turns on the owner's single thread never interleave;
- bursts of messages from the same channel are coalesced into one turn;
- messages that arrive while Orion works join the running turn between two steps, so it can
  change course ("non, laisse tomber") instead of finishing a plan the owner revoked;
- while Orion thinks, the channel shows it is working (read receipt, typing indicator);
- the reply goes back through the outlet the message asked for, unless Orion answers
  `SILENCE` (a call transcript that needs no follow-up, or a voice message already sent);
- proactive messages, files and questions for approval go where he last wrote from, or else
  to the first available outlet, in order of preference (D88): a channel is one module, and
  everything Orion sends goes through here, whatever the channel.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

import structlog

from orion.cognition.loop import StopReason
from orion.guardian.outbox import SendError
from orion.guardian.policy import ActionProposal, Approval
from orion.runtime import OWNER_THREAD, Interjection, Orion, Photo, TurnResult, is_silence

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Incoming:
    """A message from someone other than the owner, whatever the channel: what the
    secretary answers (D54)."""

    channel: str
    address: str
    """Their number (iMessage) or `telegram:<user id>`: how contacts are keyed."""
    conversation: str
    """Where to answer: the Inkbox conversation, or `telegram:<chat id>`."""
    message_id: str
    text: str


@dataclass(frozen=True)
class Attachment:
    """A file sent with a message, fetchable at `url` for a while, or by `fetch` when the
    channel must fetch it itself (a link that would carry its key)."""

    url: str
    content_type: str = ""
    size: int | None = None
    fetch: Callable[[], Awaitable[bytes]] | None = field(default=None, compare=False, repr=False)


Eyes = Callable[[Sequence[Attachment], str, str], Awaitable[list[Photo]]]
"""Turns attachments into what the model can read (the `photos` module), given the caption and
the source of the message they came with."""


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
    media: tuple[Attachment, ...] = ()
    sent_at: datetime | None = field(default=None, compare=False)
    """When the owner sent it, if the channel knows: a message that waited is said to have."""


class Outlet(Protocol):
    """A channel's owner-facing side: what every channel does."""

    name: str

    async def available(self) -> bool: ...

    async def deliver(self, text: str, correlation_id: str | None) -> None: ...

    def working(self, item: Inbound) -> AbstractAsyncContextManager[None]:
        """Shown while Orion handles `item`: read receipt, typing indicator…"""
        ...


@runtime_checkable
class FileOutlet(Outlet, Protocol):
    """A channel that can carry a file to the owner (a picture, a PDF, a voice message)."""

    async def deliver_file(self, path: Path, caption: str, correlation_id: str | None) -> None: ...


@runtime_checkable
class AskingOutlet(Outlet, Protocol):
    """A channel where the owner can say yes or no to an action (D26)."""

    async def approve(self, proposal: ActionProposal) -> Approval: ...


@asynccontextmanager
async def nothing_to_show() -> AsyncGenerator[None]:
    yield


class OwnerInbox:
    def __init__(
        self,
        orion: Orion,
        coalesce_seconds: float = 0.5,
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
        self.eyes: Eyes | None = None
        self.last_channel: str | None = None
        """The outlet he last wrote from: where Orion answers him first."""

    def register(self, outlet: Outlet) -> None:
        """Outlets are tried in registration order, after the one he last wrote from."""
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

    async def deliver_file(self, path: Path, caption: str, correlation_id: str | None) -> None:
        """A file for the owner, on a channel that carries files."""
        outlet = await self._preferred(lambda o: isinstance(o, FileOutlet))
        if not isinstance(outlet, FileOutlet):
            raise SendError("no owner channel can carry a file", retryable=False)
        await outlet.deliver_file(path, caption, correlation_id)

    async def approve(self, proposal: ActionProposal) -> Approval:
        """Put an action to the owner where he can answer it; nobody to ask means no."""
        outlet = await self._preferred(lambda o: isinstance(o, AskingOutlet))
        if not isinstance(outlet, AskingOutlet):
            log.warning("inbox.approval_unreachable", tool=proposal.tool.name)
            return Approval(False)
        return await outlet.approve(proposal)

    async def _preferred(self, can: Callable[[Outlet], bool] = lambda _: True) -> Outlet | None:
        """Where he last wrote from, if it is up; else the first outlet up, in order."""
        last = self._outlets.get(self.last_channel or "")
        ordered = [*([last] if last is not None else []), *self._outlets.values()]
        for outlet in ordered:
            if can(outlet) and await outlet.available():
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

    def _waiting(self, first: Inbound) -> bool:
        """Whether a message that would join the running turn has arrived (nothing is taken)."""
        while True:
            try:
                self._held.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return bool(self._held) and _joins(self._held[0], first)

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
            if not _joins(self._held[0], first):
                break
            joined.append(self._held.popleft())
        return joined

    async def _look(self, batch: list[Inbound]) -> list[Photo]:
        attachments = [a for item in batch for a in item.media]
        if not attachments:
            return []
        if self.eyes is None:
            return [Photo("", "(Une pièce jointe : tu ne peux pas voir les images.)")]
        caption = "\n".join(item.text for item in batch if item.text)
        return await self.eyes(attachments, caption, f"{batch[0].channel}:{OWNER_THREAD}")

    async def _handle(self, batch: list[Inbound], waited: float = 0.0) -> None:
        first = batch[0]
        started = time.monotonic()
        source = self._outlets.get(first.channel)
        reply = self._outlets.get(first.reply_via or first.channel)
        if reply is not None and not first.allow_silence:
            self.last_channel = reply.name
        showing = source.working(batch[-1]) if source is not None else nothing_to_show()

        async def interjections() -> list[Interjection]:
            if first.allow_silence:
                return []
            return [
                Interjection(item.text, item.channel, item.message_ids)
                for item in self._arrived_meanwhile(first)
            ]

        async def progress(text: str, correlation_id: str) -> None:
            if reply is not None and not first.allow_silence and not is_silence(text):
                await reply.deliver(text, correlation_id)

        async with showing:
            looked = await self._look(batch)
            # A voice message from his own number is him speaking: his words (D71).
            said = [item.text for item in batch if item.text]
            said += [f"(vocal) {p.description}" for p in looked if p.spoken]
            photos = [p for p in looked if not p.spoken]
            turn = await self._orion.handle_owner_message(
                OWNER_THREAD,
                "\n".join(said),
                first.channel,
                refs=[i for item in batch for i in item.message_ids],
                interjections=interjections,
                progress=progress,
                waiting=lambda: not first.allow_silence and self._waiting(first),
                photos=photos,
                sent_at=min((i.sent_at for i in batch if i.sent_at), default=None),
            )
        thought = time.monotonic() - started
        text = turn.text.strip()
        if turn.reason is not StopReason.COMPLETED:
            log.warning("inbox.turn_incomplete", reason=turn.reason, detail=turn.detail)
            if not text:  # a limit already gets a closing answer; this is for the rest
                text = f"(je me suis arrêté : {turn.reason.value})"
        if is_silence(text):
            # Allowed for transcripts; after a voice message or a file, it is all the answer
            # needed. The word itself is never his to read.
            log.info("inbox.silence", channel=first.channel, allowed=first.allow_silence)
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


def _joins(item: Inbound, first: Inbound) -> bool:
    """Whether a message that came during a turn can join it. One with a photo waits for its
    own turn: the photo has to be looked at first."""
    return item.channel == first.channel and not item.allow_silence and not item.media
