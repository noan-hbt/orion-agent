"""Holding off a restart while the owner is in the middle of something (D251).

Each update restarted Orion, which closed the browser and lost a hand-over or the tabs he was
working through (~8 restarts a day, Nov 2026). The core writes here until when it should not be
restarted: a few minutes after each of his messages, the length of a hand-over. The update
script (`deploy/orion-update`) reads it, waits, and groups what arrived meanwhile.

The file holds one number, seconds since the epoch, so a shell script can compare it.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

FILE = "busy-until"
_path: Path | None = None


def configure(data_dir: Path) -> None:
    """Where the core writes it; unset (tests, the CLI), `hold` does nothing."""
    global _path
    _path = data_dir / FILE


def hold(seconds: float) -> None:
    """No restart for `seconds` from now, unless a longer hold already stands."""
    if _path is None:
        return
    until = int(time.time() + seconds)
    try:
        if _path.exists() and int(_path.read_text().strip() or 0) >= until:
            return
        scratch = _path.with_suffix(".tmp")
        scratch.write_text(f"{until}\n")
        os.replace(scratch, _path)
    except (OSError, ValueError):
        # An update may restart Orion mid-conversation: a nuisance, never worth failing a turn.
        log.exception("busy.hold_failed", path=str(_path), until=until)
