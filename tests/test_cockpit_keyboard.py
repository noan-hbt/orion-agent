"""Black-box keyboard contracts for the prompt-toolkit cockpit.

These tests intentionally inspect only the public application composition and
drive its key bindings through prompt-toolkit's own key processor.  They are
written as acceptance tests for the cockpit, not as implementation snapshots.
"""
from __future__ import annotations

import io
import asyncio
from types import SimpleNamespace

import pytest

from cli_cockpit import CockpitCLIAdapter
from prompt_toolkit.data_structures import Point
from prompt_toolkit.keys import Keys
from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType


class Backend:
    def snapshot(self):
        return {"runtime": "online", "tasks": [], "agents": [], "workspace": "repo"}

    def execute(self, text):
        return None


def _app(monkeypatch):
    pytest.importorskip("prompt_toolkit")
    import cli_cockpit
    from prompt_toolkit.output import DummyOutput
    real_application = cli_cockpit.Application
    monkeypatch.setattr(
        cli_cockpit,
        "Application",
        lambda *args, **kwargs: real_application(*args, output=DummyOutput(), **kwargs),
    )
    cli = CockpitCLIAdapter(Backend(), input=io.StringIO(), output=io.StringIO())
    app = cli.build_application()
    editor = app.layout.current_control.buffer
    return cli, app, editor


def _event(app):
    return SimpleNamespace(app=app, current_buffer=app.current_buffer)


def _process(app, *keys):
    async def run():
        app.loop = asyncio.get_running_loop()
        for key in keys:
            app.key_processor.feed(KeyPress(key))
        app.key_processor.process_keys()
        await asyncio.sleep(0)
    asyncio.run(run())


def test_enter_submits_and_clears_editor(monkeypatch):
    cli, app, editor = _app(monkeypatch)
    received = []
    cli.start(received.append)
    editor.text = "hello"
    _process(app, Keys.Enter)
    assert [m.text for m in received] == ["hello"]
    assert editor.text == ""


def test_alt_enter_inserts_newline_without_submitting(monkeypatch):
    cli, app, editor = _app(monkeypatch)
    received = []
    cli.start(received.append)
    editor.text = "line one"
    editor.cursor_position = len(editor.text)
    # Control-J is the portable PTK representation of Alt+Enter/linefeed.
    # (Escape+Enter is terminal-dependent and is not synthesized by PTK's
    # key processor on Windows.)
    _process(app, Keys.ControlJ)
    assert received == []
    assert editor.text == "line one\n"


def test_ctrl_j_inserts_newline_at_caret(monkeypatch):
    cli, app, editor = _app(monkeypatch)
    received = []
    cli.start(received.append)
    editor.text = "abc\ndef"
    editor.cursor_position = 2

    _process(app, Keys.ControlJ)

    assert received == []
    assert editor.text == "ab\nc\ndef"
    assert editor.cursor_position == 3


def test_dashboard_is_a_view_and_enter_returns_to_conversation(monkeypatch):
    cli, app, editor = _app(monkeypatch)
    received = []
    cli.start(received.append)
    editor.text = "/dashboard"
    _process(app, Keys.Enter)
    assert editor.text == ""
    editor.text = "back to chat"
    _process(app, Keys.Enter)
    assert [m.text for m in received] == ["back to chat"]


def test_ctrl_d_exits_cleanly_and_does_not_submit_draft(monkeypatch):
    cli, app, editor = _app(monkeypatch)
    cli.start(lambda _: pytest.fail("Ctrl+D must not submit"))
    editor.text = "draft preserved until explicit submit"
    _process(app, Keys.ControlD)
    assert cli._stop.is_set()


def test_ctrl_c_exits_cleanly_and_does_not_submit_draft(monkeypatch):
    cli, app, editor = _app(monkeypatch)
    cli.start(lambda _: pytest.fail("Ctrl+C must not submit"))
    editor.text = "draft preserved until explicit submit"

    _process(app, Keys.ControlC)

    assert cli._stop.is_set()
    assert editor.text == "draft preserved until explicit submit"


def test_scrolling_keeps_composer_focus_and_async_output_does_not_snap(monkeypatch):
    cli, app, editor = _app(monkeypatch)
    cli.start(lambda _: None)
    for index in range(40):
        cli.send(SimpleNamespace(text=f"answer-{index}", correlation_id=None))

    assert cli._follow_tail is True
    assert cli._view.buffer.cursor_position == len(cli._view.text)
    editor.text = "draft café"

    _process(app, Keys.PageUp)
    scrolled_cursor = cli._view.buffer.cursor_position
    assert app.layout.current_control.buffer is editor
    assert cli._follow_tail is False
    assert scrolled_cursor < len(cli._view.text)

    cli.send(SimpleNamespace(text="async — terminé ✅", correlation_id=None))
    assert editor.text == "draft café"
    assert app.layout.current_control.buffer is editor
    assert cli._follow_tail is False
    assert cli._view.buffer.cursor_position == scrolled_cursor

    _process(app, Keys.End)
    assert cli._follow_tail is True
    assert cli._view.buffer.cursor_position == len(cli._view.text)

    cli.send(SimpleNamespace(text="tail-output", correlation_id=None))
    assert cli._view.buffer.cursor_position == len(cli._view.text)
    assert editor.text == "draft café"


def test_home_end_and_ctrl_arrows_scroll_transcript_not_composer(monkeypatch):
    cli, app, editor = _app(monkeypatch)
    for index in range(20):
        cli.send(SimpleNamespace(text=f"line-{index}", correlation_id=None))
    editor.text = "keep me"

    _process(app, Keys.Home)
    assert cli._view.buffer.cursor_position == 0
    assert cli._follow_tail is False
    assert editor.text == "keep me"
    assert app.layout.current_control.buffer is editor

    _process(app, Keys.ControlDown)
    assert cli._view.buffer.cursor_position > 0
    assert editor.text == "keep me"

    _process(app, Keys.ControlUp)
    assert cli._follow_tail is False
    assert editor.text == "keep me"

    _process(app, Keys.End)
    assert cli._view.buffer.cursor_position == len(cli._view.text)
    assert cli._follow_tail is True


def test_mouse_wheel_scrolls_transcript_without_stealing_composer_focus(monkeypatch):
    cli, app, editor = _app(monkeypatch)
    cli.start(lambda _: None)
    for index in range(40):
        cli.send(SimpleNamespace(text=f"answer-{index}", correlation_id=None))

    editor.text = "draft stays here"
    assert cli._view.buffer.cursor_position == len(cli._view.text)
    assert cli._follow_tail is True

    cli._view.control.mouse_handler(
        MouseEvent(
            position=Point(x=0, y=0),
            event_type=MouseEventType.SCROLL_UP,
            button=MouseButton.NONE,
            modifiers=frozenset(),
        )
    )

    scrolled_cursor = cli._view.buffer.cursor_position
    assert scrolled_cursor < len(cli._view.text)
    assert cli._follow_tail is False
    assert editor.text == "draft stays here"
    assert app.layout.current_control.buffer is editor

    cli.send(SimpleNamespace(text="async-result", correlation_id=None))
    assert cli._view.buffer.cursor_position == scrolled_cursor
    assert cli._follow_tail is False

    for _ in range(100):
        cli._view.control.mouse_handler(
            MouseEvent(
                position=Point(x=0, y=0),
                event_type=MouseEventType.SCROLL_DOWN,
                button=MouseButton.NONE,
                modifiers=frozenset(),
            )
        )

    assert cli._follow_tail is True
    assert cli._view.buffer.cursor_position == len(cli._view.text)
    assert editor.text == "draft stays here"
