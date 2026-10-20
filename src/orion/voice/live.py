"""Live calls (voice v1): Orion itself on the phone, through Inkbox's media stream.

Instead of Inkbox's hosted voice reading a brief, a call with the owner is streamed to Orion
over a WebSocket. Inkbox does the listening and the speaking; each thing the owner says arrives
as text, goes through the owner's inbox like any message (channel `voix`, one thread, the same
memory and tools), and Orion's answer is spoken back. So during the call Orion can look at the
agenda, set a reminder, search the web or remember something, for real.

Only the owner reaches it: the stream must be signed with the line's key (the guardian checks
it when Orion runs split) and the call must be with the owner's number. Calls to other people
stay with the hosted voice, under the owner's approval (`call_someone`).

    voice.live.started  voice.live.ended
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import structlog
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from orion.channels.imessage import normalize_number
from orion.channels.inbox import Inbound, OwnerInbox
from orion.core.events import NewEvent, Provenance, Trust
from orion.core.journal import EventStore

log = structlog.get_logger(__name__)

CHANNEL = "voix"
PATH = "/voice/live"
LIVE_STARTED = "voice.live.started"
LIVE_ENDED = "voice.live.ended"
FILLER_AFTER = 1.8
"""Seconds of thinking before Orion says it is looking (silence on a phone feels broken)."""
FILLER = "Je regarde."

Verify = Callable[[str, str, str, bytes], Awaitable[bool]]
"""(request id, timestamp, signature, signed bytes) → whether Inkbox signed it."""
Opening = Callable[[str], Awaitable[str | None]]
"""call id → what Orion says first when it placed the call (None: wait for the owner)."""


class LiveVoice:
    """The `voix` outlet: available while a live call is connected."""

    name = CHANNEL

    def __init__(
        self,
        journal: EventStore,
        inbox: OwnerInbox,
        owner_number: str,
        verify: Verify,
        opening: Opening | None = None,
    ) -> None:
        self._journal = journal
        self._inbox = inbox
        self._owner = normalize_number(owner_number)
        self._verify = verify
        self._opening = opening
        self._socket: WebSocket | None = None
        self._call_id: str | None = None
        self._turn = 0
        self._send_lock = asyncio.Lock()

    # -- outlet -------------------------------------------------------------------------------

    async def available(self) -> bool:
        """Only for answers to what was said on the call: proactive messages stay on iMessage."""
        return False

    async def deliver(self, text: str, correlation_id: str | None) -> None:
        await self.say(text)

    @asynccontextmanager
    async def working(self, item: Inbound) -> AsyncGenerator[None]:
        async def filler() -> None:
            await asyncio.sleep(FILLER_AFTER)
            await self.say(FILLER)

        task = asyncio.create_task(filler())
        try:
            yield
        finally:
            task.cancel()

    async def say(self, text: str) -> None:
        socket = self._socket
        if socket is None or not text.strip():
            log.info("voice.live_not_connected", dropped=len(text))
            return
        self._turn += 1
        turn = f"orion-{self._turn}"
        async with self._send_lock:
            await socket.send_text(
                json.dumps({"event": "text", "delta": text, "done": True, "turn_id": turn})
            )
            await socket.send_text(
                json.dumps(
                    {
                        "event": "transcript",
                        "party": "local",
                        "text": text,
                        "is_final": True,
                        "turn_id": turn,
                    }
                )
            )

    # -- the stream ---------------------------------------------------------------------------

    def app(self) -> Starlette:
        return Starlette(routes=[WebSocketRoute(PATH, self.stream)])

    async def _context(self, socket: WebSocket) -> dict[str, Any] | None:
        raw = socket.headers.get("x-call-context", "")
        signed = await self._verify(
            socket.headers.get("x-inkbox-request-id", ""),
            socket.headers.get("x-inkbox-timestamp", ""),
            socket.headers.get("x-inkbox-signature", ""),
            raw.encode(),
        )
        if not signed:
            log.warning("voice.live_unsigned")
            return None
        try:
            context: Any = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(context, dict):
            return None
        found: dict[str, Any] = context  # pyright: ignore[reportUnknownVariableType]
        if normalize_number(str(found.get("phone_number", ""))) != self._owner:
            log.warning("voice.live_not_the_owner")
            return None
        return found

    async def stream(self, socket: WebSocket) -> None:
        context = await self._context(socket)
        if context is None or self._socket is not None:
            await socket.close(code=1008)
            return
        call_id = str(context.get("call_id", ""))
        await socket.accept(
            headers=[
                (b"x-use-inkbox-text-to-speech", b"true"),
                (b"x-use-inkbox-speech-to-text", b"true"),
            ]
        )
        self._socket, self._call_id = socket, call_id
        await self._record(
            LIVE_STARTED, {"call_id": call_id, "direction": context.get("direction")}
        )
        log.info("voice.live_started", call_id=call_id)
        try:
            if self._opening is not None:
                opening = await self._opening(call_id)
                if opening:
                    await self.say(opening)
            await self._listen(socket, call_id)
        except WebSocketDisconnect:
            log.info("voice.live_disconnected", call_id=call_id)
        finally:
            self._socket = self._call_id = None
            await self._record(LIVE_ENDED, {"call_id": call_id})

    async def _listen(self, socket: WebSocket, call_id: str) -> None:
        while True:
            message: Any = json.loads(await socket.receive_text())
            event = str(message.get("event", ""))
            if event == "transcript" and message.get("is_final"):
                text = str(message.get("text", "")).strip()
                if text and message.get("party", "remote") == "remote":
                    turn = str(message.get("turn_id", ""))
                    self._inbox.submit(
                        Inbound(CHANNEL, (f"{call_id}:{turn}",), text, reply_via=CHANNEL)
                    )
            elif event == "barge_in":
                async with self._send_lock:
                    await socket.send_text(json.dumps({"event": "clear"}))
            elif event == "stop":
                log.info("voice.live_stopped", call_id=call_id, reason=message.get("reason"))
                return

    async def hang_up(self) -> None:
        if self._socket is not None:
            async with self._send_lock:
                await self._socket.send_text(
                    json.dumps({"event": "stop", "reason": "agent_hang_up"})
                )

    async def _record(self, kind: str, payload: dict[str, object]) -> None:
        await self._journal.append(
            [
                NewEvent(
                    kind=kind,
                    payload=payload,
                    provenance=Provenance(source=f"call:{payload['call_id']}", actor="orion:voice"),
                    trust=Trust.SYSTEM,
                )
            ]
        )
