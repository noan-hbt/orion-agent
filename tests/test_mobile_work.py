"""Orion at work in the app (D133, D169): he can stop it."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from orion.channels.inbox import STOPPED, OwnerInbox
from orion.config import AgentConfig
from orion.context.compiler import ContextCompiler
from orion.core.journal import SqliteJournal
from orion.guardian.policy import Guardian
from orion.mobile.channel import MobileChannel
from orion.mobile.devices import Devices, session_key
from orion.mobile.server import mobile_app
from orion.runtime import Orion
from orion.tools.base import Effect, Tool, ToolContext, ToolRegistry, ToolResult
from tests.fake_passkey import FakePasskey
from tests.fakes import ScriptedProvider, call_tools
from tests.test_imessage import until
from tests.test_mobile_channel import URL, FakePusher, World, pair
from tests.test_telegram import Empty


async def test_he_stops_the_work_on_his_message(journal: SqliteJournal, tmp_path: Path) -> None:
    release = asyncio.Event()
    ran: list[str] = []

    async def search(_args: Any, _ctx: ToolContext) -> ToolResult:
        ran.append("search")
        await release.wait()
        return ToolResult("3 trains")

    tool = Tool(
        name="trains",
        description="Find trains",
        input_model=Empty,
        handler=search,
        effect=Effect.NONE,
    )
    provider = ScriptedProvider(
        call_tools(("trains", {}), cost=0.002), call_tools(("trains", {}), cost=0.002)
    )
    orion = Orion(
        journal=journal,
        provider=provider,
        registry=ToolRegistry([tool]),
        guardian=Guardian(),
        compiler=ContextCompiler("CONSTITUTION"),
        agent=AgentConfig(),
    )
    await orion.start()
    inbox = OwnerInbox(orion, coalesce_seconds=0.05)
    devices = Devices(journal, URL, session_key(tmp_path / "m"))
    channel = MobileChannel(journal, inbox, devices, tmp_path / "m")
    await channel.start()
    worker = asyncio.create_task(inbox.run())
    app = mobile_app(channel, None, secure_cookie=True, session_days=30)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=URL, headers={"origin": URL}
        ) as http:
            phone = FakePasskey(URL, "orion.example")
            await pair(World(channel, http, phone, provider, FakePusher(), devices))
            assert (await http.post("/app/api/stop")).json() == {"stopping": False}  # idle

            async with channel.hub.listen():
                await http.post("/app/api/send", json={"text": "un train pour Lyon ?"})
                await until(lambda: bool(ran))
                assert channel.work is not None and set(channel.work) == {"since", "stopping"}
                assert (await http.post("/app/api/stop")).json() == {"stopping": True}
                assert channel.work is not None and channel.work["stopping"]
                release.set()
                await until(lambda: any(s.who == "orion" for s in channel.state.said))
            assert channel.state.said[-1].text == STOPPED
            assert ran == ["search"]  # the second call never ran
            assert channel.work is None and not await channel.stopped()
    finally:
        worker.cancel()
