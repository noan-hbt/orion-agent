"""A small MCP client (modelcontextprotocol.io): JSON-RPC over streamable HTTP or stdio.

Only what Orion needs: initialize, list the tools, call one. No dependency: the HTTP side runs
on any httpx transport, so it can go through the guardian's socket when the guardian holds the
server's token (D95).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Protocol, cast

import httpx
import structlog

log = structlog.get_logger(__name__)

PROTOCOL = "2025-06-18"
CLIENT = {"name": "orion", "version": "1"}


class McpError(RuntimeError):
    pass


class Transport(Protocol):
    async def request(self, method: str, params: dict[str, Any]) -> Any: ...

    async def notify(self, method: str) -> None: ...

    async def aclose(self) -> None: ...


def _result(message: dict[str, Any]) -> Any:
    if "error" in message:
        error = cast(dict[str, Any], message["error"])
        raise McpError(f"{error.get('code')}: {error.get('message')}")
    return message.get("result")


class HttpTransport:
    def __init__(self, http: httpx.AsyncClient, url: str) -> None:
        self._http = http
        self._url = url
        self._session: str | None = None
        self._next = 0

    async def _post(self, body: dict[str, Any]) -> httpx.Response:
        headers = {"accept": "application/json, text/event-stream"}
        if self._session:
            headers["mcp-session-id"] = self._session
            headers["mcp-protocol-version"] = PROTOCOL
        try:
            response = await self._http.post(self._url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise McpError(f"unreachable: {exc!r}") from exc
        if response.status_code >= 400:
            raise McpError(f"HTTP {response.status_code}: {response.text[:300]}")
        self._session = response.headers.get("mcp-session-id", self._session)
        return response

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        self._next += 1
        body = {"jsonrpc": "2.0", "id": self._next, "method": method, "params": params}
        response = await self._post(body)
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            for line in response.text.splitlines():
                if not line.startswith("data:"):
                    continue
                message = cast(dict[str, Any], json.loads(line[5:].strip() or "{}"))
                if message.get("id") == self._next:
                    return _result(message)
            raise McpError(f"no answer to {method}")
        return _result(cast(dict[str, Any], response.json()))

    async def notify(self, method: str) -> None:
        await self._post({"jsonrpc": "2.0", "method": method})

    async def aclose(self) -> None:
        await self._http.aclose()


class StdioTransport:
    """A server Orion runs itself (`command`, set on the server only)."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self._next = 0
        self._lock = asyncio.Lock()

    @classmethod
    async def start(cls, command: list[str], env: dict[str, str]) -> StdioTransport:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            limit=8 * 1024 * 1024,
        )
        return cls(process)

    async def _write(self, body: dict[str, Any]) -> None:
        assert self._process.stdin is not None
        self._process.stdin.write(json.dumps(body).encode() + b"\n")
        await self._process.stdin.drain()

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        assert self._process.stdout is not None
        async with self._lock:
            self._next += 1
            await self._write(
                {"jsonrpc": "2.0", "id": self._next, "method": method, "params": params}
            )
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    raise McpError("the server stopped")
                try:
                    message = cast(dict[str, Any], json.loads(line))
                except json.JSONDecodeError:
                    continue  # a server's stray log line
                if message.get("id") == self._next:
                    return _result(message)

    async def notify(self, method: str) -> None:
        await self._write({"jsonrpc": "2.0", "method": method})

    async def aclose(self) -> None:
        if self._process.returncode is None:
            self._process.terminate()
            await self._process.wait()


class McpClient:
    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    async def start(self) -> None:
        await self.transport.request(
            "initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": CLIENT}
        )
        await self.transport.notify("notifications/initialized")

    async def tools(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            result = cast(
                dict[str, Any],
                await self.transport.request("tools/list", {"cursor": cursor} if cursor else {}),
            )
            found += cast(list[dict[str, Any]], result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                return found

    async def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """The tool's text, and whether it is an error."""
        result = cast(
            dict[str, Any],
            await self.transport.request("tools/call", {"name": name, "arguments": arguments}),
        )
        parts: list[str] = []
        for item in cast(list[dict[str, Any]], result.get("content") or []):
            if item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif item.get("type") == "resource":
                resource = cast(dict[str, Any], item.get("resource") or {})
                parts.append(str(resource.get("text", resource.get("uri", ""))))
            else:
                parts.append(f"[{item.get('type')}]")
        if not parts and result.get("structuredContent") is not None:
            parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
        return "\n".join(parts), bool(result.get("isError"))

    async def aclose(self) -> None:
        await self.transport.aclose()
