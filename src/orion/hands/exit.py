"""The browser goes out through the owner's own connection (D145).

Many sites refuse a server's address outright (DataDome, Akamai): « Accès temporairement
restreint », whatever the browser looks like. The owner's machine, where `orion hand` runs, has
the address his own browsing comes from. When he lets it (`orion hand exit on`), Orion's browser
goes out through it: a SOCKS5 relay on the server (127.0.0.1 only) hands each connection the
browser opens to the machine, which opens it itself and carries the bytes back over a WebSocket
it dials (`/hands/exit/<id>`). The machine refuses anything but the web's ports and any address
of its own network, checked after its own name lookup.

When no machine lets it out (asleep, offline, not allowed), the relay goes straight from the
server, as before: the browser never breaks for it.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
import struct

import structlog
from starlette.websockets import WebSocketDisconnect

from orion.hands.desk import HandError, HandsDesk, Stream
from orion.web.fetch import FetchError, check_public

log = structlog.get_logger(__name__)

PORTS = (80, 443)
"""The web's ports: nothing else goes out, through the owner's machine or not."""
CHUNK = 64 * 1024


class HomeExit:
    """The SOCKS5 relay the browser is pointed at."""

    def __init__(self, desk: HandsDesk, ports: tuple[int, ...] = PORTS) -> None:
        self._desk = desk
        self._ports = ports
        self._server: asyncio.Server | None = None
        self.port = 0

    def through(self) -> str:
        """The machine the browser goes out through right now, or "" (straight out)."""
        return self._desk.exit_device() or ""

    @property
    def proxy(self) -> dict[str, str]:
        return {"server": f"socks5://127.0.0.1:{self.port}"}

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = int(self._server.sockets[0].getsockname()[1])
        log.info("hands.exit_relay", port=self.port)

    async def aclose(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            target = await _handshake(reader, writer)
            if target is None:
                return
            host, port = target
            if port not in self._ports:
                await _reply(writer, 0x02)  # not allowed
                return
            if self._desk.exit_device() is not None:
                try:
                    stream = await self._desk.open_exit(host, port)
                except HandError as exc:
                    log.info("hands.exit_refused", host=host, error=str(exc))
                    await _reply(writer, 0x02)
                    return
                await _reply(writer, 0x00)
                await _carry_home(reader, writer, stream)
                return
            try:
                await check_public(host, port)
                remote_reader, remote_writer = await asyncio.open_connection(host, port)
            except (FetchError, OSError) as exc:
                log.info("hands.exit_direct_failed", host=host, error=str(exc))
                await _reply(writer, 0x04)  # host unreachable
                return
            await _reply(writer, 0x00)
            await _carry(reader, writer, remote_reader, remote_writer)
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        finally:
            with contextlib.suppress(ConnectionError):
                writer.close()


async def _handshake(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> tuple[str, int] | None:
    """SOCKS5, no authentication, CONNECT only: where the browser wants to go."""
    version, methods = await reader.readexactly(2)
    await reader.readexactly(methods)
    if version != 5:
        return None
    writer.write(b"\x05\x00")
    await writer.drain()
    version, command, _reserved, kind = await reader.readexactly(4)
    if version != 5 or command != 1:
        await _reply(writer, 0x07)  # command not supported
        return None
    if kind == 1:
        host = str(ipaddress.IPv4Address(await reader.readexactly(4)))
    elif kind == 4:
        host = str(ipaddress.IPv6Address(await reader.readexactly(16)))
    elif kind == 3:
        size = (await reader.readexactly(1))[0]
        host = (await reader.readexactly(size)).decode("idna")
    else:
        await _reply(writer, 0x08)  # address type not supported
        return None
    (port,) = struct.unpack("!H", await reader.readexactly(2))
    return host, port


async def _reply(writer: asyncio.StreamWriter, code: int) -> None:
    writer.write(bytes([5, code, 0, 1]) + socket.inet_aton("0.0.0.0") + b"\x00\x00")
    await writer.drain()


async def _carry(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    remote_reader: asyncio.StreamReader,
    remote_writer: asyncio.StreamWriter,
) -> None:
    async def pump(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
        with contextlib.suppress(ConnectionError):
            while data := await source.read(CHUNK):
                sink.write(data)
                await sink.drain()
        with contextlib.suppress(ConnectionError, OSError):
            sink.close()

    await asyncio.gather(pump(reader, remote_writer), pump(remote_reader, writer))


async def _carry_home(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, stream: Stream
) -> None:
    async def out() -> None:
        with contextlib.suppress(ConnectionError, RuntimeError, WebSocketDisconnect):
            while data := await reader.read(CHUNK):
                await stream.socket.send_bytes(data)
            await stream.socket.close()

    async def back() -> None:
        with contextlib.suppress(ConnectionError, RuntimeError, WebSocketDisconnect):
            while True:
                writer.write(await stream.socket.receive_bytes())
                await writer.drain()
        with contextlib.suppress(ConnectionError, OSError):
            writer.close()

    try:
        sending, receiving = asyncio.ensure_future(out()), asyncio.ensure_future(back())
        await asyncio.wait({sending, receiving}, return_when=asyncio.FIRST_COMPLETED)
        for task in (sending, receiving):
            task.cancel()
    finally:
        stream.done.set()
