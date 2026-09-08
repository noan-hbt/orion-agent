"""TTY-level regression contracts for the interactive CLI.

These tests deliberately use streams which advertise ``isatty()`` while
keeping prompt-toolkit behind a tiny stub.  They exercise the transcript
boundary without requiring a real terminal or a human-driven prompt.
"""

from __future__ import annotations

import io
import threading
from contextlib import nullcontext

import pytest

import cli_ui
from cli_ui import CLIConsole


class TTYBuffer(io.StringIO):
    """A deterministic fake terminal stream."""

    def isatty(self) -> bool:
        return True


class FakePromptSession:
    def __init__(self, answer: str = "test") -> None:
        self.answer = answer
        self.calls: list[object] = []

    def prompt(self, *args: object, **kwargs: object) -> str:
        self.calls.append((args, kwargs))
        return self.answer


class FakePromptOutput:
    def __init__(self, columns: int) -> None:
        self.columns = columns

    def get_size(self):
        return type("Size", (), {"columns": self.columns})()


class CaptureConsole:
    width = 80

    def __init__(self) -> None:
        self.lines: list[str] = []

    def print(self, value: object = "", *args: object, **kwargs: object) -> None:
        self.lines.append(str(value))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _tty_console(monkeypatch: pytest.MonkeyPatch, *, width: int = 80) -> tuple[CLIConsole, CaptureConsole]:
    output = TTYBuffer()
    # On Windows, prompt-toolkit's real Win32Output requires an attached
    # console.  The fake TTY below intentionally substitutes the session
    # factory; the prompt itself is tested with FakePromptSession separately.
    monkeypatch.setattr(CLIConsole, "_build_session", lambda self, path: None)
    console = CLIConsole(
        output=output,
        input_stream=TTYBuffer(),
        show_banner=False,
        history_path=None,
        render_markdown=False,
        show_timestamps=False,
    )
    capture = CaptureConsole()
    capture.width = width
    # Keep the test focused on transcript decisions, not Rich's terminal
    # backend and ANSI implementation.
    monkeypatch.setattr(console, "_console", lambda: capture)
    return console, capture


def test_prompt_toolkit_submit_is_read_once_without_echoing_a_second_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(CLIConsole, "_build_session", lambda self, path: None)
    console = CLIConsole(
        output=TTYBuffer(), input_stream=TTYBuffer(), show_banner=False, history_path=None
    )
    session = FakePromptSession("une seule saisie")
    console._session = session
    monkeypatch.setattr(cli_ui, "patch_stdout", lambda **_: nullcontext())

    assert console.read("❯ ") == "une seule saisie"
    assert len(session.calls) == 1
    # The prompt is rendered by prompt-toolkit; read() itself must not print
    # an extra copy of the input line.
    assert console.output.getvalue() == ""


def test_stream_fragments_and_final_response_are_rendered_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console, capture = _tty_console(monkeypatch)
    request_id = "request-stream-1"

    console.render_event({"kind": "request.streaming", "request_id": request_id, "seq": 1, "text": "Bon"})
    console.render_event({"kind": "request.streaming", "request_id": request_id, "seq": 2, "text": "jour"})
    console.render_event({"kind": "request.succeeded", "request_id": request_id, "seq": 1, "text": "Bonjour"})

    # The two fragments are assembled into one final visible answer.  The
    # terminal must not print the assembled answer a second time.
    assert capture.text.count("Bonjour") == 1
    assert not console._stream_open


def test_concurrent_activity_is_deduplicated_and_keeps_stream_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console, capture = _tty_console(monkeypatch)
    barrier = threading.Barrier(3)

    def emit() -> None:
        barrier.wait()
        console.assistant("sous-agent en cours", intermediate=True, request_id="req-a")

    threads = [threading.Thread(target=emit) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)

    console.render_event({"kind": "request.streaming", "request_id": "req-a", "seq": 1, "text": "résultat"})
    console.render_event({"kind": "request.streaming", "request_id": "req-b", "seq": 1, "text": "autre"})
    console.render_event({"kind": "request.succeeded", "request_id": "req-b", "seq": 1})

    assert capture.text.count("sous-agent en cours") == 1
    assert "résultat" in capture.text and "autre" in capture.text
    assert not console._stream_open


def test_toolbar_is_single_line_and_bounded_on_a_narrow_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(CLIConsole, "_build_session", lambda self, path: None)
    console = CLIConsole(
        output=TTYBuffer(), input_stream=TTYBuffer(), show_banner=False, history_path=None
    )
    console._prompt_output = FakePromptOutput(24)
    toolbar = console._toolbar()
    rendered = "".join(fragment for _, fragment in toolbar)

    assert "\n" not in rendered
    assert len(rendered) <= 24


def test_no_color_wins_over_color_preference_and_removes_ansi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    console, capture = _tty_console(monkeypatch)
    # _tty_console creates the object after NO_COLOR is normally read in the
    # constructor; make the assertion explicit for integrations passing True.
    monkeypatch.setattr(CLIConsole, "_build_session", lambda self, path: None)
    colored = CLIConsole(
        output=TTYBuffer(), input_stream=TTYBuffer(), use_color=True,
        show_banner=False, history_path=None, render_markdown=False,
    )
    assert colored.use_color is False
    console.render_event({"kind": "request.succeeded", "request_id": "req-color", "seq": 1, "text": "sans couleur"})
    assert "\x1b[" not in capture.text


def test_ascii_mode_avoids_unicode_glyphs_in_narrow_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORION_ASCII", "1")
    console, capture = _tty_console(monkeypatch, width=12)
    console.assistant("travail", intermediate=True, request_id="req-ascii")

    assert "·" not in capture.text
    assert "travail" in capture.text
