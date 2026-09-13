"""Regression: a non-TTY cockpit session must respond to ``stop()``.

Before the fix ``loop()`` iterated ``self.input`` directly, so the stop flag --
which is all the SIGINT/SIGTERM handlers set -- was only observed *between*
lines.  On a pipe that stays open without producing another line the process
could not be interrupted at all and had to be SIGKILLed.
"""
from __future__ import annotations

import io
import threading
import time

from cli_cockpit import CockpitCLIAdapter


class _BlockingInput:
    """Stands in for a live pipe: iterable, never yields, never ends."""

    def __init__(self) -> None:
        self.release = threading.Event()

    def isatty(self) -> bool:  # noqa: D102 - forces the non-TUI path
        return False

    def __iter__(self):
        # Block until the test releases us, mimicking an idle open pipe.
        self.release.wait(30.0)
        return iter(())


class _Output(io.StringIO):
    def isatty(self) -> bool:  # noqa: D102 - forces the non-TUI path
        return False


def test_non_tty_loop_exits_promptly_when_stopped() -> None:
    source = _BlockingInput()
    adapter = CockpitCLIAdapter(
        backend=None, input=source, output=_Output(), prompt="> "
    )
    # ``loop`` starts the adapter itself; a no-op message callback is fine
    # because no line is ever delivered.
    finished = threading.Event()

    def run() -> None:
        adapter.loop()
        finished.set()

    worker = threading.Thread(target=run, daemon=True)
    started = time.monotonic()
    worker.start()
    # Let the loop install its stdin reader and reach the wait.
    time.sleep(0.35)
    assert not finished.is_set(), "loop should still be waiting for input"

    adapter.stop()

    assert finished.wait(5.0), (
        "non-TTY loop did not exit after stop(); interrupt is still blocked"
    )
    elapsed = time.monotonic() - started
    # The poll interval is 0.2s; allow generous slack for a loaded machine.
    assert elapsed < 4.0, f"stop() took {elapsed:.2f}s to take effect"
    source.release.set()


def test_non_tty_loop_consumes_lines_then_ends_on_eof() -> None:
    adapter = CockpitCLIAdapter(
        backend=None, input=io.StringIO("/exit\n"), output=_Output(), prompt="> "
    )
    adapter.loop()
    assert adapter._stop.is_set()
