"""Voice messages both ways: his are transcribed as his words, Orion's are spoken (D71)."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

import pytest

from orion.channels.inbox import Attachment, OwnerInbox
from orion.config import OrionConfig
from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.files.module import FilesModule, FileStore
from orion.photos.module import KEPT, Photos, PhotoSettings
from orion.tools.base import ToolContext
from orion.vocal.audio import SPEECH_RATE
from orion.vocal.module import VocalSettings, vocal_tools
from tests.fakes import ScriptedProvider, reply
from tests.test_imessage import make_orion

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


async def a_voice_memo() -> bytes:
    """Half a second of tone, as an M4A, like an iPhone sends."""
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
        "-c:a", "aac", "-f", "adts", "pipe:1",
        stdout=asyncio.subprocess.PIPE,
    )  # fmt: skip
    out, _ = await process.communicate()
    return out


async def test_his_voice_message_becomes_his_own_words(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    memo = await a_voice_memo()
    provider = ScriptedProvider(
        reply("Rappelle-moi d'appeler l'assurance demain à 10 h."), reply("C'est noté.")
    )

    async def fetch(_attachment: Attachment) -> bytes:
        return memo

    photos = Photos(
        journal, provider, PhotoSettings(audio_model="ears"), tmp_path / "p", fetch, ["Noan"]
    )
    orion = make_orion(journal, provider)
    await orion.start()
    inbox = OwnerInbox(orion, coalesce_seconds=0.01)
    inbox.eyes = photos.look
    from orion.channels.inbox import Inbound

    inbox.submit(
        Inbound(
            "imessage",
            ("m1",),
            "",
            media=(Attachment("https://media.inkbox.test/Audio.caf", "audio/x-caf", None),),
        )
    )
    worker = asyncio.create_task(inbox.run())
    try:
        async with asyncio.timeout(5):
            while len(provider.requests) < 2:  # noqa: ASYNC110 - polling a test fake
                await asyncio.sleep(0.01)
    finally:
        worker.cancel()

    hear, turn = provider.requests
    assert hear.model == "ears" and "Noan" in hear.messages[0]["content"]
    part: dict[str, Any] = hear.messages[1]["content"][1]
    assert part["type"] == "input_audio" and part["input_audio"]["format"] == "mp3"
    said = turn.messages[-1]["content"]
    assert said.endswith("(vocal) Rappelle-moi d'appeler l'assurance demain à 10 h.")
    events = await journal.read()
    received = next(e for e in events if e.kind == "message.received")
    assert received.trust == Trust.OWNER  # his own words, from his own number
    assert received.payload["text"].startswith("(vocal) Rappelle-moi")
    kept = next(e for e in events if e.kind == KEPT)
    assert kept.payload["kind"] == "audio"  # the memo itself is kept


async def test_orion_answers_aloud_with_an_m4a(journal: SqliteJournal, tmp_path: Path) -> None:
    class Speaker:
        async def speak(self, model: str, text: str, voice: str) -> bytes:
            assert (model, voice) == ("mouth", "alloy")
            return b"\x00\x00" * SPEECH_RATE  # one second of silence

    files = FilesModule(OrionConfig())
    files.store = FileStore(journal, tmp_path / "files")
    sent: list[tuple[Path, str]] = []

    async def deliver(path: Path, caption: str, _cid: str | None) -> None:
        sent.append((path, caption))

    files.deliver = deliver
    (say,) = vocal_tools(files, Speaker(), VocalSettings(), "mouth")
    result = await say.handler(say.parse('{"text": "Il fait beau, sors."}'), ToolContext("o", "c"))
    assert "Vocal envoyé (1 s)" in result.content and "SILENCE" in result.content
    ((path, caption),) = sent
    assert path.suffix == ".m4a" and caption == ""
    assert path.read_bytes()[4:8] == b"ftyp"  # a real MP4 container, not raw sound
