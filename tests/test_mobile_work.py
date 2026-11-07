"""Orion at work in the app (D133, D169): he can stop it."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from orion.channels.inbox import STOPPED, OwnerInbox
from orion.cognition import loop
from orion.config import AgentConfig
from orion.context.compiler import ContextCompiler
from orion.core.journal import SqliteJournal
from orion.guardian.policy import Guardian
from orion.mobile.channel import STOP_COMPLETED, STOP_FAILED, STOP_REQUESTED, MobileChannel
from orion.mobile.devices import Devices, session_key
from orion.mobile.server import mobile_app
from orion.runtime import Orion
from orion.tools.base import Effect, Tool, ToolContext, ToolRegistry, ToolResult
from orion.tools.outcome import Outcome
from tests.fake_passkey import FakePasskey
from tests.fakes import ScriptedProvider, call_tools, reply
from tests.test_imessage import until
from tests.test_mobile_channel import URL, FakePusher, World, pair
from tests.test_telegram import Empty


async def test_he_stops_the_work_on_his_message(journal: SqliteJournal, tmp_path: Path) -> None:
    """ORION-32: on 06/11 his Stop left a `browser open` running 14 more minutes, until a
    restart; his next message waited. A call that never answers is cut at once."""
    ran: list[str] = []

    async def search(_args: Any, _ctx: ToolContext) -> ToolResult:
        ran.append("search")
        await asyncio.Event().wait()  # hangs, like the browser that day
        return ToolResult("3 trains")

    tool = Tool(
        name="trains",
        description="Find trains",
        input_model=Empty,
        handler=search,
        effect=Effect.NONE,
    )
    provider = ScriptedProvider(call_tools(("trains", {}), cost=0.002), reply("Oui, je suis là."))
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
                await until(lambda: any(s.who == "orion" for s in channel.state.said))
            assert channel.state.said[-1].text == STOPPED
            assert ran == ["search"]
            assert channel.work is None and not await channel.stopped()
            kinds = [e.kind for e in await journal.read(0, 10_000)]
            assert kinds.index(STOP_REQUESTED) < kinds.index(STOP_COMPLETED)

            await http.post("/app/api/send", json={"text": "tu es là ?"})
            await until(lambda: channel.state.said[-1].text == "Oui, je suis là.")
    finally:
        worker.cancel()


async def test_a_call_that_will_not_stop_is_not_said_stopped(
    journal: SqliteJournal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ORION-32 review: a click that ignores the cut and goes on (the order goes through after
    the grace) must not be journaled stopped, and is UNCERTAIN, not « nothing happened »."""
    monkeypatch.setattr(loop, "STOP_GRACE_SECONDS", 0.2)
    ran: list[str] = []
    let_go = asyncio.Event()

    async def click(_args: Any, _ctx: ToolContext) -> ToolResult:
        ran.append("click")
        while True:
            try:
                await let_go.wait()
                break
            except asyncio.CancelledError:
                continue  # a handler that will not let go
        ran.append("ordered")  # the server took the order, after the stop
        return ToolResult("Commande passée")

    tool = Tool(
        name="shop_click",
        description="Click",
        input_model=Empty,
        handler=click,
        effect=Effect.NONE,
        acts=True,
    )
    provider = ScriptedProvider(call_tools(("shop_click", {}), cost=0.002), reply("ok"))
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

    async def kinds() -> list[str]:
        return [e.kind for e in await journal.read(0, 10_000)]

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=URL, headers={"origin": URL}
        ) as http:
            await pair(
                World(
                    channel,
                    http,
                    FakePasskey(URL, "orion.example"),
                    provider,
                    FakePusher(),
                    devices,
                )
            )
            await http.post("/app/api/send", json={"text": "commande-le"})
            await until(lambda: bool(ran))
            await http.post("/app/api/stop")
            await until(lambda: any(s.who == "orion" for s in channel.state.said))
            assert STOP_FAILED in await kinds()
            assert STOP_COMPLETED not in await kinds()
            assert channel.work is not None and channel.work["stopping"]
            executed = [e for e in await journal.read(0, 10_000) if e.kind == "tool.executed"]
            assert executed[-1].payload.get("outcome") == Outcome.UNCERTAIN.value

            let_go.set()
            await until(lambda: "ordered" in ran)
            await until(lambda: channel.work is None)
            assert STOP_COMPLETED in await kinds()
            assert loop.stray_calls() == []
    finally:
        worker.cancel()
