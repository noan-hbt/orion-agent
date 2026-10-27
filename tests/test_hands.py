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
    assert "bail-appartement.txt" in found.content and found.trust < found.trust.SYSTEM
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
    nul = await tools["hand_read_file"].handler(
        tools["hand_read_file"].parse(json.dumps({"device": "mac", "path": "bail\u0000.txt"})),
        ctx,
    )
    assert nul.is_error and "mac" in desk.connected  # refused; the hand is still there
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


def test_a_search_stops_walking_a_big_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A home folder full of caches answers fast instead of keeping the call waiting."""
    import orion.hands.hand as hand_module

    for i in range(30):
        (tmp_path / f"note-{i}.txt").write_text("x")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "bail.txt").write_text("x")
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache" / "bail.txt").write_text("x")
    (tmp_path / "zz").mkdir()
    (tmp_path / "zz" / "bail.txt").write_text("x")
    hand = Hand(HandConfig("http://x", "mac", "s", [str(tmp_path)]))

    found = hand.find_files("bail")
    assert found["content"] == str(tmp_path / "zz" / "bail.txt")  # caches are not walked

    monkeypatch.setattr(hand_module, "MAX_VISITED", 10)
    assert hand.find_files("bail") == {"content": "", "truncated": True}


async def test_a_pairing_code_is_used_once_however_it_is_typed(journal: SqliteJournal) -> None:
    desk = HandsDesk(journal)
    await desk.start()
    code = await desk.new_code("mac")
    assert await desk.pair(f"  {code}\n") is not None
    assert await desk.pair(code) is None
    other = await desk.new_code("pc")
    both = await asyncio.gather(desk.pair(other), desk.pair(other))
    assert sum(found is not None for found in both) == 1


def test_the_hands_secret_is_never_readable_by_others(tmp_path: Path) -> None:
    import os

    old = os.umask(0)
    try:
        path = tmp_path / "hand.json"
        HandConfig(server="https://x", device="mac", secret="s").save(path)
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(old)


async def test_the_browser_goes_out_through_his_machine_never_into_his_network(
    server: tuple[HandsDesk, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """D145: a site that refuses servers sees his own connection; his box, his NAS, never."""
    from orion.hands.exit import HomeExit

    desk, url = server
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(await reader.read(100))
        await writer.drain()
        writer.close()

    site = await asyncio.start_server(echo, "127.0.0.1", 0)
    port = int(site.sockets[0].getsockname()[1])
    hand = Hand(await pair(url, await desk.new_code("mac"), [], ["exit"]))
    hand.exit_ports = (port,)
    hand.public = lambda address: address == "127.0.0.1"  # here, « the web »
    running = asyncio.create_task(hand.session())
    await until(lambda: "mac" in desk.connected)
    relay = HomeExit(desk, ports=(port,))
    await relay.start()

    async def through(host: str) -> tuple[int, bytes]:
        reader, writer = await asyncio.open_connection("127.0.0.1", relay.port)
        writer.write(b"\x05\x01\x00")
        await reader.readexactly(2)
        name = host.encode()
        writer.write(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + port.to_bytes(2, "big"))
        answer = (await reader.readexactly(10))[1]
        said = b""
        if answer == 0:
            writer.write(b"bonjour")
            said = await reader.read(100)
        writer.close()
        return answer, said

    assert relay.through() == "mac"
    assert await through("127.0.0.1") == (0, b"bonjour")  # carried by his machine
    assert (await through("10.0.0.1"))[0] == 2  # his network: his machine refuses
    running.cancel()
    await until(lambda: "mac" not in desk.connected)
    assert relay.through() == ""
    assert (await through("127.0.0.1"))[0] == 4  # straight out, the server's guard holds
    await relay.aclose()
    site.close()
