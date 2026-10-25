"""MCP: `[modules.mcp]` in orion.toml (D95).

Thousands of tools already exist as MCP servers (GitHub, Notion, Home Assistant, Drive…). The
owner plugs one in (`[[modules.mcp.servers]]`, by message with his yes: it is his setting), and
its tools become Orion's, named `mcp_<server>_<tool>`, listed when Orion starts:

- a tool is an action on the world, put to the owner each time, unless he listed it in the
  server's `read_only` (then it is a read, allowed even after a web page);
- what a tool returns comes from elsewhere (`THIRD_PARTY`): it informs, never commands;
- a remote server's token is a secret: when Orion runs split, the guardian holds it and relays
  the calls to that server's address alone (`/v1/mcp/<name>`); the core never sees it;
- a server Orion runs itself (`command`) is set on the server only, never by message.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Any, ClassVar

import httpx
import structlog
from pydantic import BaseModel, ConfigDict

from orion.core.events import Trust
from orion.mcp.client import HttpTransport, McpClient, McpError, StdioTransport, Transport
from orion.mcp.settings import McpServer, McpSettings
from orion.modules.base import Module, Plate
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from orion.config import OrionConfig
    from orion.guardian.client import GuardianClient

log = structlog.get_logger(__name__)

START_TIMEOUT = 30.0
_UNSAFE = re.compile(r"[^a-zA-Z0-9_]")


def input_model(schema: dict[str, Any]) -> type[BaseModel]:
    """Any arguments, described to the model by the server's own JSON schema."""

    class McpArgs(BaseModel):
        model_config = ConfigDict(extra="allow")
        given: ClassVar[dict[str, Any]] = schema or {"type": "object", "properties": {}}

        @classmethod
        def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:  # pyright: ignore[reportIncompatibleMethodOverride]
            return cls.given

    return McpArgs


def mcp_tool(server: McpServer, client: McpClient, spec: dict[str, Any]) -> Tool:
    name = str(spec.get("name", ""))
    reads = "*" in server.read_only or name in server.read_only

    async def call(args: Any, _ctx: ToolContext) -> ToolResult:
        try:
            text, error = await client.call(name, args.model_dump())
        except McpError as exc:
            return ToolResult(f"{server.name}: {exc}", is_error=True)
        return ToolResult(text or "(rien)", is_error=error, trust=Trust.THIRD_PARTY)

    description = str(spec.get("description") or name)
    return Tool(
        name=_UNSAFE.sub("_", f"mcp_{server.name}_{name}")[:64],
        description=f"[{server.name}, MCP] {description}"[:1000],
        input_model=input_model(dict(spec.get("inputSchema") or {})),
        handler=call,
        effect=Effect.NONE if reads else Effect.EXTERNAL,
        reversibility=Reversibility.REVERSIBLE if reads else Reversibility.IRREVERSIBLE,
        concurrency_safe=reads,
        accepts_untrusted_context=reads,
    )


BASE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER")
"""What any program needs to run (find its binaries, its cache, its language)."""


def server_env(server: McpServer) -> dict[str, str]:
    """A command server's environment: the basics and the variables named for it, never the
    rest of Orion's (its provider keys, its bot token…)."""
    return {name: os.environ[name] for name in (*BASE_ENV, *server.env) if name in os.environ}


async def connect(server: McpServer, guardian: GuardianClient | None) -> McpClient:
    transport: Transport
    if server.command:
        transport = await StdioTransport.start(server.command, server_env(server))
    elif guardian is not None and server.token is not None:
        transport = HttpTransport(guardian.relay(), f"/v1/mcp/{server.name}")
    else:
        headers = (
            {"authorization": f"Bearer {server.token.resolve().get_secret_value()}"}
            if server.token is not None
            else {}
        )
        transport = HttpTransport(httpx.AsyncClient(headers=headers, timeout=60.0), server.url)
    client = McpClient(transport)
    try:
        await client.start()
    except BaseException:
        await client.aclose()  # a server that will not start is not left running
        raise
    return client


class McpModule(Module):
    name = "mcp"
    settings_model = McpSettings

    def __init__(self, config: OrionConfig) -> None:
        super().__init__(config)
        self.clients: list[McpClient] = []

    async def assemble(self, plate: Plate) -> list[Tool]:
        import asyncio

        settings: McpSettings = self.settings
        tools: list[Tool] = []
        taken: set[str] = set()
        for server in settings.servers:
            try:
                client = await asyncio.wait_for(connect(server, plate.guardian), START_TIMEOUT)
            except (McpError, OSError, LookupError, TimeoutError) as exc:
                # One server down must not keep Orion from starting: it runs without its tools.
                log.warning("mcp.server_unavailable", server=server.name, error=repr(exc))
                continue
            self.clients.append(client)  # closed with the module, whatever happens next
            try:
                specs = await asyncio.wait_for(client.tools(), START_TIMEOUT)
            except (McpError, OSError, TimeoutError) as exc:
                log.warning("mcp.server_unavailable", server=server.name, error=repr(exc))
                continue
            for spec in specs:
                if not spec.get("name"):
                    continue
                tool = mcp_tool(server, client, spec)
                if tool.name in taken:
                    # Two tools that read the same once made safe (`a-b`, `a.b`): the first
                    # keeps the name, the other is left out rather than stopping Orion.
                    log.warning("mcp.tool_name_taken", server=server.name, tool=spec.get("name"))
                    continue
                taken.add(tool.name)
                tools.append(tool)
            log.info("mcp.server", server=server.name, tools=len(specs))
        return tools

    async def close(self) -> None:
        for client in self.clients:
            await client.aclose()

    def doctor(self) -> list[str]:
        settings: McpSettings = self.settings
        return [f"{s.name}: {s.url or ' '.join(s.command)}" for s in settings.servers] or [
            "no server: add [[modules.mcp.servers]]"
        ]
