"""Acceptance contracts for the redesigned Orion terminal transcript.

The tests use tiny stream/console doubles instead of a real terminal.  This
keeps the contracts deterministic on CI (and on Windows), while exercising
both the prompt-toolkit boundary and the line-oriented fallback.
"""

from __future__ import annotations

import io
import threading
from contextlib import nullcontext

import pytest

import cli_ui
from cli_ui import CLIConsole


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


class FakePromptSession:
    def __init__(self, value: str = "une saisie") -> None:
        self.value = value
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def prompt(self, *args: object, **kwargs: object) -> str:
        self.calls.append((args, kwargs))
        return self.value


class FakePromptOutput:
    def __init__(self, columns: int) -> None:
        self.columns = columns

    def get_size(self):
        return type("Size", (), {"columns": self.columns})()


class CaptureConsole:
    """Rich-like sink retaining only visible values."""

    width = 80

    def __init__(self) -> None:
        self.values: list[str] = []

    def print(self, value: object = "", *args: object, **kwargs: object) -> None:
        # Rich's Panel/Text objects intentionally have an opaque ``str``;
        # expose their semantic content so the tests remain renderer-agnostic.
        title = getattr(value, "title", None)
        renderable = getattr(value, "renderable", None)
        plain = getattr(renderable, "plain", None)
        if title is not None or plain is not None:
            self.values.append(" ".join(str(part) for part in (title, plain) if part is not None))
        else:
            self.values.append(str(value))

    @property
    def text(self) -> str:
        return "\n".join(self.values)


def _tty_console(monkeypatch: pytest.MonkeyPatch, *, width: int = 80) -> tuple[CLIConsole, CaptureConsole]:
    monkeypatch.setattr(CLIConsole, "_build_session", lambda self, path: None)
    console = CLIConsole(
        output=FakeTTY(),
        input_stream=FakeTTY(),
        show_banner=False,
        history_path=None,
        render_markdown=False,
        show_timestamps=False,
    )
    sink = CaptureConsole()
    sink.width = width
    monkeypatch.setattr(console, "_console", lambda: sink)
    return console, sink


def test_prompt_toolkit_echoes_input_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Submitting a PTK buffer must not produce a second local user line."""
    monkeypatch.setattr(CLIConsole, "_build_session", lambda self, path: None)
    console = CLIConsole(output=FakeTTY(), input_stream=FakeTTY(), show_banner=False, history_path=None)
    session = FakePromptSession("une seule saisie")
    console._session = session
    monkeypatch.setattr(cli_ui, "patch_stdout", lambda **_: nullcontext())

    assert console.read() == "une seule saisie"
    assert len(session.calls) == 1
    assert console.output.getvalue() == ""


def test_fallback_input_is_not_duplicated_and_orion_is_one_block() -> None:
    """The StringIO path still emits one input and one final answer."""
    output = io.StringIO()
    console = CLIConsole(output=output, input_stream=io.StringIO("bonjour\n"), show_banner=False, history_path=None)
    request_id = console.requests.create("bonjour").request_id
    console.render_event({"kind": "request.queued", "request_id": request_id, "seq": 1, "text": "bonjour"})
    console.render_event({"kind": "request.succeeded", "request_id": request_id, "seq": 1, "text": "Réponse unique"})

    text = output.getvalue()
    assert text.count("bonjour") == 1
    assert text.count("Réponse unique") == 1


def test_activity_is_separate_from_orion_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    console, sink = _tty_console(monkeypatch)
    request_id = "req-activity"
    console.render_event({"kind": "request.running", "request_id": request_id, "seq": 1, "text": "", "meta": {"activity": "Recherche en cours"}})
    console.render_event({"kind": "request.succeeded", "request_id": request_id, "seq": 1, "text": "Voici le résultat"})
    assert "Recherche en cours" in sink.text
    assert "Voici le résultat" in sink.text
    assert sink.text.index("Recherche en cours") < sink.text.index("Voici le résultat")


def test_stream_and_final_answer_have_no_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    console, sink = _tty_console(monkeypatch)
    request_id = "req-stream"
    console.render_event({"kind": "request.streaming", "request_id": request_id, "seq": 1, "text": "Bon"})
    console.render_event({"kind": "request.streaming", "request_id": request_id, "seq": 2, "text": "jour"})
    console.render_event({"kind": "request.succeeded", "request_id": request_id, "seq": 1, "text": "Bonjour"})
    assert sink.text.count("Bonjour") == 1


def test_compact_banner_contains_identity_and_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    console, sink = _tty_console(monkeypatch)
    console.show_banner = True
    console.model = "anthropic/claude-sonnet"
    console.set_usage_provider({"known_cost_usd": 0.0126, "total_tokens": 6200, "started_calls": 1})
    console.banner()
    rendered = sink.text
    assert "Orion" in rendered
    assert "claude-sonnet" in rendered
    assert "$0.0126" in rendered
    assert "6.2k tok" in rendered or "6200 tok" in rendered


def test_assistant_heading_does_not_repeat_event_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real event timestamp must appear once in the Orion heading.

    This reproduces the visible ``Orion 15:42 · 15:42`` regression from the
    interactive CLI: ``_assistant_heading`` already includes the timestamp,
    while the response block currently appends it a second time.
    """
    console, sink = _tty_console(monkeypatch)
    console.show_timestamps = True
    console.render_event(
        {
            "kind": "request.succeeded",
            "request_id": "req-timestamp",
            "seq": 1,
            "timestamp": "2026-09-08T15:42:00+02:00",
            "text": "Réponse",
        }
    )
    heading = sink.values[0]
    assert heading == "● Orion 15:42"


def test_toolbar_stays_one_line_and_fits_narrow_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(CLIConsole, "_build_session", lambda self, path: None)
    console = CLIConsole(output=FakeTTY(), input_stream=FakeTTY(), show_banner=False, history_path=None)
    console._prompt_output = FakePromptOutput(20)
    toolbar = "".join(value for _, value in console._toolbar())
    assert "\n" not in toolbar
    assert len(toolbar) <= 20


def test_unicode_is_preserved_and_no_color_is_honored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    console, sink = _tty_console(monkeypatch)
    assert console.use_color is False
    console.render_event({"kind": "request.succeeded", "request_id": "req-unicode", "seq": 1, "text": "Café — terminé ✅"})
    assert "Café" in sink.text
    assert "✅" in sink.text
    assert "\x1b[" not in sink.text


def test_concurrent_events_are_serialized_without_losing_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    console, sink = _tty_console(monkeypatch)
    barrier = threading.Barrier(3)

    def emit(request_id: str, answer: str) -> None:
        barrier.wait()
        console.render_event({"kind": "request.succeeded", "request_id": request_id, "seq": 1, "text": answer})

    threads = [threading.Thread(target=emit, args=(f"req-{i}", f"Réponse {i}")) for i in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert "Réponse 0" in sink.text
    assert "Réponse 1" in sink.text
