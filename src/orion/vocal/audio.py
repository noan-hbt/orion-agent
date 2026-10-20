"""Audio conversions with ffmpeg (installed on the server: `apt install ffmpeg`).

iPhone voice messages come as CAF or M4A, which models do not all take: they are turned into a
small mono MP3 first. Orion's own voice comes from the model as raw 16-bit PCM and leaves as an
M4A (AAC), which iMessage plays in the conversation.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

SPEECH_RATE = 24_000
"""Sample rate of the PCM a speech model sends back."""


class AudioError(RuntimeError):
    pass


async def _ffmpeg(arguments: list[str], data: bytes, expect_output: bool = True) -> bytes:
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise AudioError("ffmpeg is not installed on the server")
    process = await asyncio.create_subprocess_exec(
        binary,
        "-loglevel",
        "error",
        *arguments,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await process.communicate(data)
    if process.returncode != 0 or (expect_output and not out):
        raise AudioError(f"ffmpeg failed: {err.decode('utf-8', 'replace')[:300]}")
    return out


async def to_mp3(data: bytes) -> bytes:
    """Any audio file, as a 16 kHz mono MP3: small, and taken by audio models."""
    return await _ffmpeg(
        ["-i", "pipe:0", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "32k",
         "-f", "mp3", "pipe:1"],
        data,
    )  # fmt: skip


async def pcm_to_m4a(pcm: bytes, rate: int = SPEECH_RATE) -> bytes:
    """Raw 16-bit mono PCM, as an M4A iMessage plays in the conversation. Written to a file,
    not a pipe: a piped MP4 is fragmented, and not every player takes it."""
    with tempfile.TemporaryDirectory(prefix="orion-voice-") as folder:
        out = Path(folder) / "vocal.m4a"
        await _ffmpeg(
            ["-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "pipe:0", "-c:a", "aac",
             "-b:a", "48k", "-movflags", "+faststart", "-y", str(out)],
            pcm,
            expect_output=False,
        )  # fmt: skip
        return await asyncio.to_thread(out.read_bytes)


def seconds_of_pcm(pcm: bytes, rate: int = SPEECH_RATE) -> float:
    return len(pcm) / (2 * rate)
