"""The brain's side of the hands (D1): pairing, connections, calls.

A hand is `orion hand`, running on one of the owner's machines. It dials out to the server (the
owner opens no port), presents the secret it got when paired, and says what it can do. The
brain calls it with typed actions, never a shell; the hand checks each one against what its
owner allowed on that machine (which folders, which actions). A hand that is offline is simply
unavailable: tools say so, nothing waits.

Pairing: `orion hands pair <device>` on the server gives a one-time code (ten minutes); on the
machine, `orion hand pair <server> <code>` trades it for the device's secret, kept there. Only
hashes are journaled. `orion hands revoke <device>` cuts a machine off.

    hands.code.created  hands.paired  hands.revoked
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner

log = structlog.get_logger(__name__)

CODE_CREATED = "hands.code.created"
PAIRED = "hands.paired"
REVOKED = "hands.revoked"
CODE_TTL = timedelta(minutes=10)
CALL_TIMEOUT = 30.0
ACTIONS = ("notify", "open_url", "find_files", "read_file", "get_file", "exit")
EXIT_TIMEOUT = 10.0
"""How long a machine may take to open a way out for the browser (`orion.hands.exit`)."""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class HandError(RuntimeError):
    """The hand is offline, refused, or failed."""


@dataclass
class HandsState:
    name: str = "hands"
    codes: dict[str, tuple[str, datetime]] = field(default_factory=dict[str, tuple[str, datetime]])
    """code hash → (device, expiry), while unused."""
    devices: dict[str, str] = field(default_factory=dict[str, str])
    """device → secret hash."""
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.codes.clear()
        self.devices.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted:
            return
        payload: dict[str, Any] = event.payload
        if event.kind == CODE_CREATED:
            self.codes[str(payload["hash"])] = (
                str(payload["device"]),
                datetime.fromisoformat(str(payload["expires_at"])),
            )
        elif event.kind == PAIRED:
            self.codes.pop(str(payload["code_hash"]), None)
            self.devices[str(payload["device"])] = str(payload["secret_hash"])
        elif event.kind == REVOKED:
            self.devices.pop(str(payload["device"]), None)


@dataclass
class Connection:
    device: str
    socket: WebSocket
    actions: tuple[str, ...]
    roots: tuple[str, ...]
    pending: dict[str, asyncio.Future[dict[str, Any]]] = field(
        default_factory=dict[str, asyncio.Future[dict[str, Any]]]
    )
    exits: dict[str, asyncio.Future[Stream]] = field(
        default_factory=dict[str, asyncio.Future["Stream"]]
    )
    """Ways out asked of this machine, until it dials back for them."""


@dataclass
class Stream:
    """One TCP connection the owner's machine opened for the browser, carried by a WebSocket
    it dialled back on (`/hands/exit/<id>`): bytes in, bytes out, until either side ends."""

    socket: WebSocket
    done: asyncio.Event = field(default_factory=asyncio.Event)


class HandsDesk:
    def __init__(self, journal: EventStore) -> None:
        self._journal = journal
        self.state = HandsState()
        self._runner = ProjectionRunner(journal, [self.state])
        self.connected: dict[str, Connection] = {}
        self._pairing = asyncio.Lock()

    async def start(self) -> None:
        await self._runner.catch_up()

    # -- pairing ------------------------------------------------------------------------------

    async def new_code(self, device: str, now: datetime | None = None) -> str:
        code = "-".join(secrets.token_hex(2) for _ in range(3))
        expires = (now or datetime.now(UTC)) + CODE_TTL
        await self._record(
            CODE_CREATED,
            {"hash": _hash(code), "device": device, "expires_at": expires.isoformat()},
        )
        return code

    async def pair(self, code: str, now: datetime | None = None) -> tuple[str, str] | None:
        """(device, secret) for a valid code, once."""
        code = code.strip()  # the same code, typed with a space, is still used up
        async with self._pairing:  # two machines with one code at once: only one pairs
            await self._runner.catch_up()
            found = self.state.codes.get(_hash(code))
            if found is None or (now or datetime.now(UTC)) > found[1]:
                return None
            device, secret = found[0], secrets.token_urlsafe(32)
            await self._record(
                PAIRED, {"device": device, "secret_hash": _hash(secret), "code_hash": _hash(code)}
            )
            return device, secret

    async def revoke(self, device: str) -> None:
        await self._record(REVOKED, {"device": device})
        connection = self.connected.pop(device, None)
        if connection is not None:
            await connection.socket.close(code=1008)

    def _device_for(self, secret: str) -> str | None:
        digest = _hash(secret)
        for device, known in self.state.devices.items():
            if secrets.compare_digest(known, digest):
                return device
        return None

    # -- calls --------------------------------------------------------------------------------

    async def call(self, device: str, action: str, args: dict[str, Any]) -> dict[str, Any]:
        connection = self.connected.get(device)
        if connection is None:
            raise HandError(f"{device} is offline")
        if action not in connection.actions:
            raise HandError(f"{device} does not allow {action}")
        call_id = secrets.token_hex(8)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        connection.pending[call_id] = future
        try:
            await connection.socket.send_text(
                json.dumps({"type": "call", "id": call_id, "action": action, "args": args})
            )
            result = await asyncio.wait_for(future, CALL_TIMEOUT)
        except TimeoutError as exc:
            raise HandError(f"{device} did not answer") from exc
        finally:
            connection.pending.pop(call_id, None)
        if not result.get("ok"):
            raise HandError(str(result.get("error", "refused")))
        return result

    def exit_device(self) -> str | None:
        """A machine online that lets the browser go out through it."""
        return next((d for d, c in sorted(self.connected.items()) if "exit" in c.actions), None)

    async def open_exit(self, host: str, port: int) -> Stream:
        """A connection to host:port from the owner's machine, which checks it itself."""
        device = self.exit_device()
        connection = self.connected.get(device) if device else None
        if device is None or connection is None:
            raise HandError("no machine lets the browser out")
        stream_id = secrets.token_hex(12)
        future: asyncio.Future[Stream] = asyncio.get_running_loop().create_future()
        connection.exits[stream_id] = future
        try:
            await connection.socket.send_text(
                json.dumps({"type": "exit", "stream": stream_id, "host": host, "port": port})
            )
            return await asyncio.wait_for(future, EXIT_TIMEOUT)
        except TimeoutError as exc:
            raise HandError(f"{device} did not open the way to {host}") from exc
        finally:
            connection.exits.pop(stream_id, None)

    # -- the web side -------------------------------------------------------------------------

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/hands/pair", self._pair, methods=["POST"]),
                WebSocketRoute("/hands/connect", self.connect),
                WebSocketRoute("/hands/exit/{stream}", self.exit),
            ]
        )

    async def _pair(self, request: Request) -> Response:
        try:
            body: Any = json.loads(await request.body())
            code = str(body["code"])
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "malformed"}, status_code=400)
        paired = await self.pair(code)
        if paired is None:
            log.warning("hands.pair_refused")
            return JSONResponse({"error": "unknown or expired code"}, status_code=403)
        log.info("hands.paired", device=paired[0])
        return JSONResponse({"device": paired[0], "secret": paired[1]})

    async def connect(self, socket: WebSocket) -> None:
        await self._runner.catch_up()
        auth = socket.headers.get("authorization", "")
        device = self._device_for(auth.removeprefix("Bearer ").strip()) if auth else None
        if device is None:
            await socket.close(code=1008)
            return
        await socket.accept()
        try:
            hello: Any = json.loads(await socket.receive_text())
            actions = tuple(a for a in hello.get("actions", []) if a in ACTIONS)
            roots = tuple(str(r) for r in hello.get("roots", []))[:20]
        except (ValueError, AttributeError, WebSocketDisconnect):
            await socket.close(code=1003)
            return
        old = self.connected.get(device)
        if old is not None:
            await old.socket.close(code=1000)
        connection = Connection(device, socket, actions, roots)
        self.connected[device] = connection
        log.info("hands.connected", device=device, actions=actions)
        try:
            while True:
                message: Any = json.loads(await socket.receive_text())
                if message.get("type") == "result":
                    future = connection.pending.get(str(message.get("id")))
                    if future is not None and not future.done():
                        future.set_result(dict(message))
                elif message.get("type") == "exit_refused":
                    way = connection.exits.get(str(message.get("stream")))
                    if way is not None and not way.done():
                        way.set_exception(HandError(str(message.get("error", "refused"))))
        except (WebSocketDisconnect, ValueError):
            log.info("hands.disconnected", device=device)
        finally:
            if self.connected.get(device) is connection:
                del self.connected[device]
            for future in connection.pending.values():
                if not future.done():
                    future.set_exception(HandError(f"{device} went offline"))
            for way in connection.exits.values():
                if not way.done():
                    way.set_exception(HandError(f"{device} went offline"))

    async def exit(self, socket: WebSocket) -> None:
        """A machine dialling back with the connection it opened for the browser."""
        await self._runner.catch_up()
        auth = socket.headers.get("authorization", "")
        device = self._device_for(auth.removeprefix("Bearer ").strip()) if auth else None
        connection = self.connected.get(device) if device else None
        way = connection.exits.get(socket.path_params["stream"]) if connection else None
        if way is None or way.done():
            await socket.close(code=1008)
            return
        await socket.accept()
        stream = Stream(socket)
        way.set_result(stream)
        await stream.done.wait()  # the browser's side carries the bytes, then lets go

    async def _record(self, kind: str, payload: dict[str, object]) -> None:
        await self._journal.append(
            [
                NewEvent(
                    kind=kind,
                    payload=payload,
                    provenance=Provenance(source="hands", actor="owner:main"),
                    trust=Trust.OWNER,
                )
            ]
        )
        await self._runner.catch_up()
