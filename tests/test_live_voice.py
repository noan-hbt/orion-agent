"""Voice v1: Orion itself on the phone, through Inkbox's media stream."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orion.channels.inbox import Inbound
from orion.channels.inkbox import InkboxError
from orion.core.journal import SqliteJournal
from orion.voice.live import CHANNEL, FILLER, LIVE_ENDED, LIVE_STARTED, LiveVoice
from tests.fake_guardian import guardian_config, running_guardian
from tests.fake_inkbox import OWNER

LIVE_URL = "wss://orion.test/voice/live"


class Socket:
    def __init__(
        self,
        context: dict[str, Any],
        signed: bool = True,
        script: list[dict[str, Any]] | None = None,
    ) -> None:
        self.headers = {
            "x-call-context": json.dumps(context),
            "x-inkbox-signature": "good" if signed else "bad",
        }
        self.script = list(script or [])
        self.sent: list[dict[str, Any]] = []
        self.accepted: list[tuple[bytes, bytes]] | None = None
        self.closed: int | None = None

    async def accept(self, headers: list[tuple[bytes, bytes]]) -> None:
        self.accepted = headers

    async def close(self, code: int) -> None:
        self.closed = code

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def receive_text(self) -> str:
        return json.dumps(self.script.pop(0))


class Inbox:
    def __init__(self) -> None:
        self.items: list[Inbound] = []

    def submit(self, item: Inbound) -> None:
        self.items.append(item)


async def signed(_rid: str, _ts: str, signature: str, body: bytes) -> bool:
    return signature == "good" and b"call_id" in body


def live(journal: SqliteJournal, inbox: Inbox, opening: Any = None) -> LiveVoice:
    return LiveVoice(journal, inbox, OWNER, signed, opening)  # type: ignore[arg-type]


async def test_only_a_signed_call_with_the_owner_gets_in(journal: SqliteJournal) -> None:
    voice = live(journal, Inbox())
    forged = Socket({"call_id": "c1", "phone_number": OWNER}, signed=False)
    await voice.stream(forged)  # type: ignore[arg-type]
    assert forged.closed == 1008 and forged.accepted is None
    stranger = Socket({"call_id": "c2", "phone_number": "+33699999999"})
    await voice.stream(stranger)  # type: ignore[arg-type]
    assert stranger.closed == 1008 and stranger.accepted is None


async def test_what_the_owner_says_goes_to_orion_and_the_answer_is_spoken(
    journal: SqliteJournal,
) -> None:
    inbox = Inbox()

    async def opening(call_id: str) -> str | None:
        return "Allô, c'est Orion. Ton rendez-vous est dans une heure."

    socket = Socket(
        {"call_id": "c1", "phone_number": OWNER, "direction": "outbound"},
        script=[
            {"event": "start", "stream_id": "s"},
            {"event": "transcript", "text": "Ajoute", "is_final": False, "turn_id": "t1"},
            {
                "event": "transcript",
                "text": "Ajoute dentiste jeudi",
                "is_final": True,
                "turn_id": "t1",
            },
            {"event": "barge_in", "trigger": "interim_transcript", "text": "attends"},
            {"event": "stop", "reason": "remote"},
        ],
    )
    voice = live(journal, inbox, opening)
    await voice.stream(socket)  # type: ignore[arg-type]
    assert (b"x-use-inkbox-text-to-speech", b"true") in (socket.accepted or [])
    assert [i.text for i in inbox.items] == ["Ajoute dentiste jeudi"]
    assert inbox.items[0].channel == CHANNEL and inbox.items[0].reply_via == CHANNEL
    events = [m["event"] for m in socket.sent]
    assert events == ["text", "transcript", "clear"]  # the opening, its record, the barge-in
    assert socket.sent[0]["delta"].startswith("Allô, c'est Orion.")
    kinds = [e.kind for e in await journal.read()]
    assert kinds == [LIVE_STARTED, LIVE_ENDED]
    assert not await voice.available()  # proactive messages never go to a phone line


async def test_a_slow_answer_gets_a_filler(
    journal: SqliteJournal, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    import orion.voice.live as module

    monkeypatch.setattr(module, "FILLER_AFTER", 0.01)
    voice = live(journal, Inbox())
    socket = Socket({"call_id": "c1", "phone_number": OWNER})
    voice._socket = socket  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage]
    item = Inbound(CHANNEL, ("c1:t1",), "quoi ?")
    async with voice.working(item):
        await asyncio.sleep(0.05)
    await voice.deliver("Jeudi à 9 h.", None)
    spoken = [m["delta"] for m in socket.sent if m["event"] == "text"]
    assert spoken == [FILLER, "Jeudi à 9 h."]


async def test_the_guardian_streams_calls_only_to_orion_and_only_with_the_owner(
    tmp_path: Path,
) -> None:
    config = guardian_config(tmp_path, voice={"enabled": True, "live": True, "live_url": LIVE_URL})
    async with running_guardian(config) as g:
        g.fake.inbound("salut")
        core = g.client.inkbox(max_attempts=1)
        with pytest.raises(InkboxError, match="only to Orion's own live voice"):
            await core._request(  # pyright: ignore[reportPrivateUsage]
                "POST",
                "/phone/place-call",
                json={
                    "to_number": OWNER,
                    "mode": "client_websocket",
                    "client_websocket_url": "wss://evil.example/listen",
                },
            )
        with pytest.raises(InkboxError, match="only the owner's calls are led live"):
            await core.place_call("+33622222222", "x", live_url=LIVE_URL)
        await core.place_call(OWNER, "rappel", live_url=LIVE_URL)
        await core.aclose()
