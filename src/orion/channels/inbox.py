"""The owner's unified inbox: every channel drops messages here, one worker answers them.

Channels only translate (receive, deduplicate, journal; deliver). The inbox owns everything
that is the same whatever the channel:

- one queue, one worker, so turns on the owner's single thread never interleave;
- bursts of messages from the same channel are coalesced into one turn;
- messages that arrive while Orion works join the running turn between two steps, so it can
  change course ("non, laisse tomber") instead of finishing a plan the owner revoked;
- while Orion thinks, the channel shows it is working (read receipt, typing indicator);
- what Orion says of his work as it goes is a message of its own, except where the work is
  shown live (the app), where it is shown with it (D143);
- the reply goes back through the outlet the message asked for, unless Orion answers
  `SILENCE` (a call transcript that needs no follow-up, or a voice message already sent);
- proactive messages, files and questions for approval go where he last wrote from, or else
  to the first available outlet, in order of preference (D88): a channel is one module, and
  everything Orion sends goes through here, whatever the channel;
- a channel that does not draw Orion's structured pieces (D126) gets them as plain text.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

import structlog

from orion.channels.markup import plain
from orion.cognition.loop import StopReason
from orion.guardian.outbox import SendError
from orion.guardian.policy import ActionProposal, Approval
from orion.runtime import (
    OWNER_THREAD,
    Interjection,
    Orion,
    Photo,
    TurnResult,
    is_silence,
)

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
    """A channel's owner-facing side: what every channel does. One that draws Orion's
    structured pieces (choices, cards; D126) says so with `rich = True`."""

    name: str

    async def available(self) -> bool: ...

    async def deliver(self, text: str, correlation_id: str | None, quiet: bool = False) -> None:
        """`quiet`: sent without ringing (what he says while still working, D169)."""
        ...

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


@runtime_checkable
class StoppableOutlet(Outlet, Protocol):
    """A channel where he can call off the work on his message (the app)."""

    async def stopped(self) -> bool:
        """Whether he asked to stop the work on his message."""
        ...


STOPPED = "D'accord, j'arrête là."


@asynccontextmanager
async def nothing_to_show() -> AsyncGenerator[None]:
    yield


def shown(outlet: Outlet, text: str) -> str:
    """The text as this outlet can show it: pieces it cannot draw become plain text. A
    `<suite/>` left in it (a caption) is a paragraph break."""
    text = NEXT.sub("\n\n", text).strip()
    return text if getattr(outlet, "rich", False) else plain(text)


NEXT = re.compile(r"\s*<suite\s*/>\s*")
"""Where Orion's words become another message: two things to say, said apart (D163)."""
MAX_MESSAGES = 3


def messages_of(text: str) -> list[str]:
    """His words as the messages he meant: cut at each `<suite/>`, never more than
    `MAX_MESSAGES` (the rest stays in the last one)."""
    parts = [p.strip() for p in NEXT.split(text) if p.strip()]
    if len(parts) > MAX_MESSAGES:
        parts = [*parts[: MAX_MESSAGES - 1], "\n\n".join(parts[MAX_MESSAGES - 1 :])]
    return parts


def beat(text: str, seconds: float) -> float:
    """The pause before a message that follows another, as if typed: longer for more words,
    never more than three beats."""
    return min(3 * seconds, seconds * (0.6 + len(text) / 120))


class OwnerInbox:
    def __init__(
        self,
        orion: Orion,
        coalesce_seconds: float = 0.5,
        on_turn: Callable[[TurnResult], Awaitable[None]] | None = None,
        beat_seconds: float = 1.0,
    ) -> None:
        self._orion = orion
        self._coalesce = coalesce_seconds
        self._beat = beat_seconds
        """The pause between two of his messages (`beat`)."""
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
        await self._say(outlet, text, correlation_id, None)

    async def _say(
        self,
        outlet: Outlet,
        text: str,
        correlation_id: str | None,
        item: Inbound | None,
        quiet: bool = False,
    ) -> None:
        """His words as the messages he meant, each after a pause as if typed (shown typing
        on the channel when it answers a message of his)."""
        for i, message in enumerate(messages_of(text)):
            if i:
                async with outlet.working(item) if item is not None else nothing_to_show():
                    await asyncio.sleep(beat(message, self._beat))
            await outlet.deliver(shown(outlet, message), correlation_id, quiet=quiet)

    async def deliver_file(self, path: Path, caption: str, correlation_id: str | None) -> None:
        """A file for the owner, on a channel that carries files."""
        outlet = await self._preferred(lambda o: isinstance(o, FileOutlet))
        if not isinstance(outlet, FileOutlet):
            raise SendError("no owner channel can carry a file", retryable=False)
        await outlet.deliver_file(path, shown(outlet, caption), correlation_id)

    async def approve(self, proposal: ActionProposal) -> Approval:
        """Put an action to the owner where he can answer it; nobody to ask means no."""
        outlet = await self._preferred(lambda o: isinstance(o, AskingOutlet))
        if not isinstance(outlet, AskingOutlet):
            log.warning("inbox.approval_unreachable", tool=proposal.tool.name)
            return Approval(False, reached=False)
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
            try:
                await self._handle(batch, waited=time.monotonic() - started)
            except Exception:
                # One failed turn (a provider bug, a reply that could not leave) must not stop
                # the owner's next messages from being answered. The batch stays unanswered in
                # its channel's journal, and is offered again at the next start.
                log.exception(
                    "inbox.turn_failed",
                    channel=batch[0].channel,
                    messages=len(batch),
                    refs=[i for item in batch for i in item.message_ids],
                )
                await self._say_it_failed(batch[0])

    async def _say_it_failed(self, first: Inbound) -> None:
        reply = self._outlets.get(first.reply_via or first.channel)
        if reply is None or first.allow_silence:
            return
        try:
            await reply.deliver("(Je n'ai pas pu te répondre : une erreur de mon côté.)", None)
        except Exception:
            log.exception("inbox.failure_notice_failed", channel=reply.name)

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
            if reply is None or first.allow_silence or is_silence(text):
                return
            # A message of its own, but only the last one of the turn rings (D169).
            await self._say(reply, text, correlation_id, None, quiet=True)

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
                halted=source.stopped if isinstance(source, StoppableOutlet) else None,
            )
        thought = time.monotonic() - started
        text = turn.text.strip()
        if turn.reason is StopReason.HALTED and not text:
            text = STOPPED
        elif turn.reason is not StopReason.COMPLETED:
            log.warning("inbox.turn_incomplete", reason=turn.reason, detail=turn.detail)
            if not text:  # a limit already gets a closing answer; this is for the rest
                text = f"(je me suis arrêté : {turn.reason.value})"
        if is_silence(text):
            # Allowed for transcripts; after a voice message or a file, it is all the answer
            # needed. The word itself is never his to read.
            log.info("inbox.silence", channel=first.channel, allowed=first.allow_silence)
        elif text and reply is not None:
            await self._say(reply, text, turn.correlation_id, batch[-1])
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
