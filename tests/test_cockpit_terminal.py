"""Terminal-level checks for the prompt-toolkit cockpit screen.

These deliberately target ``cli_cockpit`` and do not exercise the legacy
``cli_ui`` console.
"""
import asyncio
import io
import threading
from types import SimpleNamespace

import pytest
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.application.current import set_app

from channels import AgentOutput
from cli_cockpit import CockpitCLIAdapter


@pytest.fixture(autouse=True)
def _use_test_output(monkeypatch):
    """Avoid Win32Output probing: this runner has no attached ConPTY."""
    import prompt_toolkit.output.defaults as defaults
    monkeypatch.setattr(defaults, "create_output", lambda: _active_output)


_active_output = None


class _SizeOutput(DummyOutput):
    def __init__(self, columns, rows):
        self._size = (columns, rows)

    def get_size(self):
        from prompt_toolkit.data_structures import Size
        return Size(rows=self._size[1], columns=self._size[0])

class _Backend:
    def snapshot(self):
        return {"runtime": "online", "tasks": [], "agents": [], "workspace": "test"}


def _render(app):
    async def render():
        app.loop = asyncio.get_running_loop()
        with set_app(app):
            app.renderer.render(app, app.layout)
            await asyncio.sleep(0)

    asyncio.run(render())


@pytest.mark.parametrize("columns", [24, 40, 80, 120, 160])
def test_cockpit_builds_ptk_screen_at_terminal_width(columns):
    pytest.importorskip("prompt_toolkit")
    global _active_output
    _active_output = _SizeOutput(columns, 24)
    cli = CockpitCLIAdapter(_Backend(), output=_active_output)
    app = cli.build_application()
    _render(app)
    assert app.full_screen
    assert _active_output.get_size().columns == columns
    assert cli._view is not None


def test_async_output_during_focused_edit_preserves_unicode_and_single_prompt():
    pytest.importorskip("prompt_toolkit")
    global _active_output
    _active_output = _SizeOutput(80, 24)
    cli = CockpitCLIAdapter(_Backend(), output=_active_output)
    app = cli.build_application()
    editor = app.layout.current_control
    editor.text = "analyse Café"
    cli.send(AgentOutput("worker — terminé ✅"))
    assert editor.text == "analyse Café"
    assert cli._view.text.count("worker — terminé ✅") == 1
    assert cli.prompt == "orion > "


def test_tool_preamble_renders_native_progress_as_notification_not_second_orion_message():
    global _active_output
    _active_output = _SizeOutput(80, 24)
    cli = CockpitCLIAdapter(_Backend(), output=_active_output)
    cli.build_application()

    cli.send(
        AgentOutput(
            "Je vais lancer plusieurs recherches.",
            correlation_id="cli-7",
            metadata={"intermediate": True, "phase": "tool_preamble"},
        )
    )

    assert "• Orion · Je vais lancer plusieurs recherches." in cli._view.text


def test_generated_chrome_is_single_line_and_cp1252_safe():
    global _active_output
    _active_output = _SizeOutput(40, 24)
    cli = CockpitCLIAdapter(_Backend(), output=_active_output)
    cli.build_application()

    header = "".join(text for _, text in cli._header_fragments())
    footer = "".join(text for _, text in cli._footer_fragments())
    for chrome in (header, footer, cli.prompt, "Conversation", "Message", "-"):
        assert "\n" not in chrome and "\r" not in chrome
        chrome.encode("cp1252")


def test_build_application_reuses_injected_prompt_toolkit_output():
    """Do not probe a second Win32 console when the caller already has PTK IO."""
    injected = _SizeOutput(80, 24)
    cli = CockpitCLIAdapter(_Backend(), output=injected)

    app = cli.build_application()

    assert app.output is injected


def test_non_tty_output_degrades_safely_on_strict_cp1252_stream():
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    cli = CockpitCLIAdapter(_Backend(), input=io.StringIO(), output=stream)

    cli.send(AgentOutput("unicode response: café ✅"))
    cli._render_result({"title": "test", "data": None, "error": "échec ✅"})
    stream.flush()

    rendered = raw.getvalue().decode("cp1252")
    assert "* Orion" in rendered
    assert "café" in rendered
    assert "ERROR: échec" in rendered
    assert "?" in rendered  # unrepresentable emoji was replaced, not fatal


def test_non_tty_worker_identity_degrades_safely_on_strict_cp1252_stream():
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    cli = CockpitCLIAdapter(_Backend(), input=io.StringIO(), output=stream)

    cli.send(
        AgentOutput(
            "résultat café ✅",
            metadata={
                "output_origin": "subagent",
                "sender_name": "analyste-✅",
            },
        )
    )
    stream.flush()

    rendered = raw.getvalue().decode("cp1252")
    assert "* Worker · analyste-?" in rendered
    assert "résultat café ?" in rendered
    assert "Orion" not in rendered


def test_rendered_scroll_stays_put_across_async_append_then_end_follows_tail():
    global _active_output
    _active_output = _SizeOutput(80, 24)
    cli = CockpitCLIAdapter(_Backend(), output=_active_output)
    app = cli.build_application()
    for index in range(60):
        cli.send(AgentOutput(f"message-{index}: " + ("long text " * 5)))

    _render(app)
    assert cli._view.window.render_info.bottom_visible

    cli._scroll_view("page_up")
    _render(app)
    assert cli._follow_tail is False
    assert not cli._view.window.render_info.bottom_visible
    cursor_before = cli._view.buffer.cursor_position
    first_visible_before = cli._view.window.render_info.first_visible_line()

    cli.send(AgentOutput("async — unicode ✅"))
    _render(app)
    assert cli._view.buffer.cursor_position == cursor_before
    assert cli._view.window.render_info.first_visible_line() == first_visible_before
    assert not cli._view.window.render_info.bottom_visible

    cli._scroll_view("end")
    _render(app)
    assert cli._follow_tail is True
    assert cli._view.window.render_info.bottom_visible


def test_worker_output_while_scrolled_preserves_view_and_worker_heading():
    global _active_output
    _active_output = _SizeOutput(80, 24)
    cli = CockpitCLIAdapter(_Backend(), output=_active_output)
    app = cli.build_application()
    for index in range(60):
        cli.send(AgentOutput(f"message-{index}: " + ("long text " * 5)))

    _render(app)
    cli._scroll_view("page_up")
    _render(app)
    cursor_before = cli._view.buffer.cursor_position
    first_visible_before = cli._view.window.render_info.first_visible_line()

    cli.send(
        AgentOutput(
            "worker artifact",
            metadata={
                "output_origin": "subagent",
                "sender_name": "toml-analyst",
            },
        )
    )
    _render(app)

    assert cli._view.buffer.cursor_position == cursor_before
    assert cli._view.window.render_info.first_visible_line() == first_visible_before
    assert cli._follow_tail is False
    assert cli._view.text.count("WORKER · TOML-ANALYST") == 1


def test_conversational_worker_result_renders_as_notification_while_scrolled():
    global _active_output
    _active_output = _SizeOutput(80, 24)
    cli = CockpitCLIAdapter(_Backend(), output=_active_output)
    app = cli.build_application()
    for index in range(60):
        cli.send(AgentOutput(f"message-{index}: " + ("long text " * 5)))

    _render(app)
    cli._scroll_view("page_up")
    _render(app)
    cursor_before = cli._view.buffer.cursor_position
    first_visible_before = cli._view.window.render_info.first_visible_line()

    cli.send(
        AgentOutput(
            "raw worker artifact",
            metadata={
                "output_origin": "subagent",
                "sender_name": "toml-analyst",
                "intermediate": True,
                "phase": "subagent_result",
            },
        )
    )
    _render(app)

    assert cli._view.buffer.cursor_position == cursor_before
    assert cli._view.window.render_info.first_visible_line() == first_visible_before
    assert cli._follow_tail is False
    assert cli._view.text.count("• Worker · toml-analyst · résultat reçu") == 1
    assert "raw worker artifact" not in cli._view.text


def test_concurrent_sends_are_serialized_in_ptk_transcript():
    pytest.importorskip("prompt_toolkit")
    global _active_output
    _active_output = _SizeOutput(80, 24)
    cli = CockpitCLIAdapter(_Backend(), output=_active_output)
    cli.build_application()
    threads = [threading.Thread(target=cli.send, args=(AgentOutput(str(i)),)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(cli.transcript_events) == 20
    lines = cli._view.text.splitlines()
    assert all(any(line == str(i) for line in lines) for i in range(20))


def test_running_tui_marshals_background_refresh_onto_ptk_loop():
    global _active_output
    _active_output = _SizeOutput(80, 24)
    cli = CockpitCLIAdapter(_Backend(), output=_active_output)
    real_app = cli.build_application()

    scheduled = []
    fake_loop = SimpleNamespace(call_soon_threadsafe=lambda callback: scheduled.append(callback))
    fake_app = SimpleNamespace(is_running=True, loop=fake_loop, invalidate=lambda: None)
    cli._app = fake_app

    before = cli._view.text
    worker = threading.Thread(target=cli.send, args=(AgentOutput("background result"),))
    worker.start()
    worker.join()

    assert cli.transcript_events[-1].text == "background result"
    assert cli._view.text == before
    assert len(scheduled) == 1
    scheduled[0]()
    assert "background result" in cli._view.text

    cli._app = real_app


def test_tui_report_error_uses_transcript_instead_of_direct_terminal_write():
    global _active_output
    _active_output = _SizeOutput(80, 24)
    cli = CockpitCLIAdapter(_Backend(), output=_active_output)
    cli.build_application()

    event = SimpleNamespace(type="subagent.completed", id="event-123")
    cli.report_error(event, RuntimeError("raw provider detail must not be printed"))

    assert cli.transcript_events[-1].speaker == "SYSTEM"
    assert "subagent.completed" in cli.transcript_events[-1].text
    assert "RuntimeError" in cli.transcript_events[-1].text
    assert "raw provider detail" not in cli.transcript_events[-1].text
