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
    return app.renderer.last_rendered_screen


def _screen_line(screen, row, columns):
    return "".join(screen.data_buffer[row][column].char for column in range(columns)).rstrip()


@pytest.mark.parametrize("columns", [20, 24, 40, 80, 120, 160])
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


@pytest.mark.parametrize("rows", [5, 6, 7])
@pytest.mark.parametrize("columns", [20, 24, 40, 80, 120, 160])
def test_low_terminal_heights_use_compact_layout_without_window_too_small(columns, rows):
    global _active_output
    _active_output = _SizeOutput(columns, rows)
    cli = CockpitCLIAdapter(_Backend(), output=_active_output)
    app = cli.build_application()

    screen = _render(app)
    first_line = _screen_line(screen, 0, columns)

    assert "Window too small" not in first_line
    assert "ORION" in first_line
    assert app.layout.current_control is not cli._view.control


@pytest.mark.parametrize("columns", [20, 24, 40, 80, 120, 160])
def test_header_footer_adapt_to_width_without_partial_tokens(columns):
    global _active_output
    _active_output = _SizeOutput(columns, 12)
    cli = CockpitCLIAdapter(_Backend(), output=_active_output)
    app = cli.build_application()

    screen = _render(app)
    header = _screen_line(screen, 0, columns)
    footer = _screen_line(screen, 11, columns)

    assert len(header) <= columns
    assert len(footer) <= columns
    assert "ORION" in header
    assert "CHAT" in header
    assert "PgUp/Dn" in footer
    assert not footer.endswith(("PgUp/", "PgUp/D", "PgUp/Dn sc", "/hel"))


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


def test_real_prompt_toolkit_eof_exits_tui_cleanly():
    from prompt_toolkit.input import create_pipe_input

    with create_pipe_input() as pipe:
        cli = CockpitCLIAdapter(_Backend(), input=pipe, output=DummyOutput())
        pipe.close()

        cli.run_tui()

    assert cli._stop.is_set()
    assert not cli.running


def test_resize_across_compact_and_wide_layout_preserves_focus_and_draft():
    global _active_output
    _active_output = _SizeOutput(80, 12)
    cli = CockpitCLIAdapter(_Backend(), output=_active_output)
    app = cli.build_application()
    editor = app.layout.current_control
    editor.text = "draft café"
    editor.cursor_position = len(editor.text)

    for columns, rows in ((20, 5), (160, 24), (24, 7), (80, 12)):
        _active_output._size = (columns, rows)
        screen = _render(app)
        assert "Window too small" not in _screen_line(screen, 0, columns)
        assert app.layout.current_control is editor
        assert editor.text == "draft café"


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


def test_page_down_moves_a_rendered_page_and_can_return_toward_tail():
    global _active_output
    _active_output = _SizeOutput(40, 12)
    cli = CockpitCLIAdapter(_Backend(), output=_active_output)
    app = cli.build_application()
    for index in range(100):
        cli.send(AgentOutput(f"message-{index}: " + ("wrapped text " * 4)))

    _render(app)
    cli._scroll_view("page_up")
    _render(app)
    cli._scroll_view("page_up")
    _render(app)
    first_before = cli._view.window.render_info.first_visible_line()

    cli._scroll_view("page_down")
    _render(app)
    first_after = cli._view.window.render_info.first_visible_line()

    assert first_after >= first_before + 2
    assert cli._follow_tail is False

    for _ in range(50):
        cli._scroll_view("page_down")
        _render(app)
        if cli._follow_tail:
            break
    assert cli._follow_tail is True
    assert cli._view.window.render_info.bottom_visible


@pytest.mark.parametrize("command", ["/status", "/dashboard", "/watch"])
def test_dashboard_views_with_long_history_open_on_recent_content(command):
    global _active_output
    _active_output = _SizeOutput(40, 12)
    cli = CockpitCLIAdapter(_Backend(), output=_active_output)
    app = cli.build_application()
    for index in range(60):
        cli.send(AgentOutput(f"old-message-{index:02d}"))

    assert cli._command(command)
    screen = _render(app)
    visible = "\n".join(_screen_line(screen, row, 40) for row in range(12))

    assert cli._view_mode == command[1:]
    assert cli._view.text.startswith("ORION")
    assert "RUNTIME" in cli._view.text
    assert "old-message-00" not in cli._view.text
    assert "ORION" in visible
    assert "old-message-00" not in visible


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
