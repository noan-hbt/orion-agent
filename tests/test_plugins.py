"""What others made, plugged in: MCP servers, skills and the public catalog (D95)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from orion.core.events import Trust
from orion.mcp.client import HttpTransport, McpClient
from orion.mcp.module import mcp_tool
from orion.mcp.settings import McpServer
from orion.skills.module import CATALOG, Skill, Skills, skill_tool
from orion.tools.base import Effect, ToolContext

CTX = ToolContext("owner", "c")


def fake_server() -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content)
        method = body["method"]
        result: Any
        if "id" not in body:
            return httpx.Response(202)
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {}}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result},
                                  headers={"mcp-session-id": "s1"})  # fmt: skip
        assert request.headers["mcp-session-id"] == "s1"
        if method == "tools/list":
            result = {"tools": [
                {"name": "search", "description": "Search notes",
                 "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}},
                {"name": "create-page", "description": "Create a page", "inputSchema": {"type": "object"}},
            ]}  # fmt: skip
        else:
            q = body["params"]["arguments"].get("q")
            result = {"content": [{"type": "text", "text": f"3 notes for {q}"}]}
        # Answered as an event stream, as many servers do.
        data = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result})
        return httpx.Response(200, text=f"event: message\ndata: {data}\n\n",
                              headers={"content-type": "text/event-stream"})  # fmt: skip

    return httpx.MockTransport(handle)


async def test_an_mcp_servers_tools_become_orions_and_only_reads_go_unasked() -> None:
    server = McpServer(name="notes", url="https://x/mcp", read_only=["search"])
    client = McpClient(HttpTransport(httpx.AsyncClient(transport=fake_server()), server.url))
    await client.start()
    search, create = [mcp_tool(server, client, spec) for spec in await client.tools()]
    assert (search.name, create.name) == ("mcp_notes_search", "mcp_notes_create_page")
    assert search.spec().parameters["properties"] == {"q": {"type": "string"}}
    assert search.effect is Effect.NONE and search.accepts_untrusted_context
    assert create.effect is Effect.EXTERNAL  # every other tool: the owner says yes first
    found = await search.handler(search.parse('{"q": "loyer"}'), CTX)
    assert found.content == "3 notes for loyer" and found.trust is Trust.THIRD_PARTY
    await client.aclose()


async def test_skills_are_kept_read_and_installed_from_the_catalog(tmp_path: Path) -> None:
    skills = Skills(tmp_path / "skills", CATALOG, [])
    tool = skill_tool(skills)

    async def run(**args: object) -> Any:
        return await tool.handler(tool.parse(json.dumps(args)), CTX)

    write = {"name": "factures", "description": "Faire une facture", "text": "Numéroter, dater."}
    assert tool.effect_for(tool.parse(json.dumps({"action": "write", **write}))) is Effect.EXTERNAL
    await run(action="write", **write)
    assert "factures : Faire une facture" in (await run(action="list")).content
    read = await run(action="read", name="factures")
    assert "Numéroter" in read.content and read.trust is Trust.SYSTEM

    catalog = (await run(action="catalog")).content
    assert "[skill] mail-pro" in catalog and "[mcp] github" in catalog
    await run(action="install", name="mail-pro")
    assert skills.get("mail-pro") is not None
    plug = (await run(action="install", name="github")).content
    assert "modules.mcp.servers" in plug and "GITHUB_MCP_TOKEN" in plug

    # From a catalog outside Orion's own: read as outside content.
    skills.write(Skill("ailleurs", "x", "fais ceci", source="https://exemple.org/s.md"))
    assert (await run(action="read", name="ailleurs")).trust is Trust.THIRD_PARTY
