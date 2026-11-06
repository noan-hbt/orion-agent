"""Updates wait while the owner is in the middle of something (D251)."""

from __future__ import annotations

import time
from pathlib import Path

from orion.core import busy


def test_a_hold_says_until_when_and_a_shorter_one_does_not_cut_it(tmp_path: Path) -> None:
    busy.configure(tmp_path)
    try:
        busy.hold(30 * 60)  # a hand-over
        busy.hold(10 * 60)  # then one of his messages
        until = int((tmp_path / busy.FILE).read_text())
        assert until >= time.time() + 29 * 60
    finally:
        busy._path = None  # pyright: ignore[reportPrivateUsage]
