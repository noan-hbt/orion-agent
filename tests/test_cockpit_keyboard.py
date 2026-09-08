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
from prompt_toolkit.keys import Keys
from prompt_toolkit.key_binding.key_processor import KeyPress


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
    # Control-J is the portable PTK representation of Alt+Enter/linefeed.
    # (Escape+Enter is terminal-dependent and is not synthesized by PTK's
    # key processor on Windows.)
    _process(app, Keys.ControlJ)
    assert received == []
    assert editor.text == "line one\n"


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
