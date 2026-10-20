"""Orion's hands: a real server, a real hand dialing out over a WebSocket."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import pytest
import websockets

from orion.core.journal import SqliteJournal
from orion.hands.desk import HandError, HandsDesk
from orion.hands.hand import Hand, HandConfig, Refused, pair
from orion.hands.module import hands_tools
from orion.tools.base import ToolContext
from tests.test_federation import free_port, serve_for_real


@pytest.fixture
async def server(journal: SqliteJournal) -> AsyncIterator[tuple[HandsDesk, str]]:
    desk = HandsDesk(journal)
    await desk.start()
    port = free_port()
    async with AsyncExitStack() as stack:
        await serve_for_real(stack, desk.app(), port)
        yield desk, f"http://127.0.0.1:{port}"


async def until(condition: Any) -> None:
    async with asyncio.timeout(5):
        while not condition():  # noqa: ASYNC110 - waiting on a real connection
            await asyncio.sleep(0.02)


async def test_a_paired_hand_does_only_what_its_machine_allows(
    server: tuple[HandsDesk, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    desk, url = server
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    shared = tmp_path / "Documents"
    shared.mkdir()
    (shared / "bail-appartement.txt").write_text("Loyer : 650 € charges comprises")
    (tmp_path / "secret.txt").write_text("mot de passe")

    with pytest.raises(Refused):
        await pair(url, "nope-nope-nope", [], [])
    code = await desk.new_code("mac")
    config = await pair(url, code, [str(shared)], ["notify", "find_files", "read_file"])
    assert config.device == "mac"
    with pytest.raises(Refused):
        await pair(url, code, [], [])  # once

    hand = Hand(config)
    running = asyncio.create_task(hand.session())
    await until(lambda: "mac" in desk.connected)
    tools = {t.name: t for t in hands_tools(desk)}
    ctx = ToolContext("owner", "c")

    found = await tools["hand_find_files"].handler(
        tools["hand_find_files"].parse('{"device": "mac", "query": "bail"}'), ctx
    )
    assert "bail-appartement.txt" in found.content
    path = str(shared / "bail-appartement.txt")
    read = await tools["hand_read_file"].handler(
        tools["hand_read_file"].parse(json.dumps({"device": "mac", "path": path})), ctx
    )
    assert "650 €" in read.content and read.trust < read.trust.SYSTEM
    escape = str(shared / ".." / "secret.txt")
    outside = await tools["hand_read_file"].handler(
        tools["hand_read_file"].parse(json.dumps({"device": "mac", "path": escape})), ctx
    )
    assert outside.is_error and "outside the allowed folders" in outside.content
    with pytest.raises(HandError, match="does not allow open_url"):
        await desk.call("mac", "open_url", {"url": "https://example.com"})
    listed = await tools["hands"].handler(tools["hands"].parse("{}"), ctx)
    assert "mac : en ligne" in listed.content

    await desk.revoke("mac")
    with pytest.raises(websockets.ConnectionClosed):
        await asyncio.wait_for(running, 5)
    with pytest.raises(HandError, match="offline"):
        await desk.call("mac", "notify", {"text": "x"})
    with pytest.raises(websockets.InvalidStatus):  # the old secret no longer opens anything
        await Hand(config).session()


async def test_an_unknown_secret_is_turned_away(server: tuple[HandsDesk, str]) -> None:
    desk, url = server
    stranger = HandConfig(url, "pc", "forged-secret", [], ["notify"])
    with pytest.raises(websockets.InvalidStatus):
        await Hand(stranger).session()
    with pytest.raises(Refused, match="no longer paired"):  # it stops, it does not hammer
        await Hand(stranger).run_forever()
    assert desk.connected == {}


async def test_the_live_voice_is_served_for_real(journal: SqliteJournal) -> None:
    from orion.channels.inbox import Inbound
    from orion.voice.live import PATH, LiveVoice
    from tests.fake_inkbox import OWNER

    items: list[Inbound] = []

    class Inbox:
        def submit(self, item: Inbound) -> None:
            items.append(item)

    async def signed(_rid: str, _ts: str, signature: str, _body: bytes) -> bool:
        return signature == "good"

    voice = LiveVoice(journal, Inbox(), OWNER, signed)  # type: ignore[arg-type]
    port = free_port()
    async with AsyncExitStack() as stack:
        await serve_for_real(stack, voice.app(), port)
        headers = {
            "X-Call-Context": json.dumps({"call_id": "c1", "phone_number": OWNER}),
            "X-Inkbox-Signature": "good",
        }
        async with websockets.connect(
            f"ws://127.0.0.1:{port}{PATH}", additional_headers=headers
        ) as call:
            assert call.response is not None
            assert call.response.headers["x-use-inkbox-text-to-speech"] == "true"
            await call.send(
                json.dumps(
                    {
                        "event": "transcript",
                        "text": "Salut Orion",
                        "is_final": True,
                        "turn_id": "t1",
                    }
                )
            )
            await until(lambda: bool(items))
            await voice.say("Salut !")
            spoken = json.loads(await call.recv())
            assert spoken == {
                "event": "text",
                "delta": "Salut !",
                "done": True,
                "turn_id": "orion-1",
            }
            await call.send(json.dumps({"event": "stop", "reason": "remote"}))
    assert items[0].text == "Salut Orion"
