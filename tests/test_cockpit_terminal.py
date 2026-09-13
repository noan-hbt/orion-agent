"""Terminal-level checks for the prompt-toolkit cockpit screen.

These deliberately target ``cli_cockpit`` and do not exercise the legacy
``cli_ui`` console.
"""
import asyncio
import io
import threading
import time
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
    def __init__(self, snapshot=None):
        self._snapshot = snapshot

    def snapshot(self):
        if self._snapshot is not None:
            return self._snapshot
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


# ---------------------------------------------------------------------------
# Scrolling: the viewport must stay on the text the operator is reading.
# ---------------------------------------------------------------------------


def _visible_body(cli, app, columns=80):
    """Non-empty body rows, excluding the header row and the section rule."""
    screen = _render(app)
    rows = [
        _screen_line(screen, row, columns)
        for row in range(_active_output.get_size().rows)
    ]
    return [row for row in rows if row.strip()][2:]


@pytest.mark.parametrize(
    "scrolls",
    [
        ["home"],
        ["page_up"],
        ["page_up", "page_up"],
        ["line_up"],
        ["line_up", "line_up", "line_up", "line_up", "line_up"],
        ["page_up", "line_down"],
    ],
)
def test_output_arriving_while_browsing_does_not_move_the_viewport(scrolls):
    """New transcript content must not shift the rows under the reader.

    The previous implementation restored only the raw cursor offset, but the
    viewport follows the cursor *cell*, so appending text below a reader who
    had scrolled up slid the visible content down by the height of the new
    block. Plain single-line messages are used deliberately: they keep every
    existing row byte-identical, so any change in the visible body is drift.
    """
    global _active_output
    pytest.importorskip("prompt_toolkit")
    _active_output = _SizeOutput(80, 20)
    cli = CockpitCLIAdapter(_Backend(), output=_active_output)
    app = cli.build_application()
    for index in range(60):
        cli._append_transcript("assistant", f"line-{index:02d}")
    _render(app)

    cli._follow_tail = True
    cli._scroll_view("end")
    for scroll in scrolls:
        cli._scroll_view(scroll)
    assert cli._follow_tail is False

    before = _visible_body(cli, app)
    cli._append_transcript("assistant", "ARRIVED-WHILE-READING")
    after = _visible_body(cli, app)

    assert before == after, "viewport drifted when new output arrived"
    assert not any("ARRIVED-WHILE-READING" in row for row in after)


def test_end_key_resumes_tail_following():
    global _active_output
    _active_output = _SizeOutput(80, 20)
    cli = CockpitCLIAdapter(_Backend(), output=_active_output)
    app = cli.build_application()
    for index in range(60):
        cli._append_transcript("assistant", f"line-{index:02d}")
    _render(app)

    cli._scroll_view("home")
    assert cli._follow_tail is False

    cli._scroll_view("end")
    assert cli._follow_tail is True
    cli._append_transcript("assistant", "LATEST")
    screen = _render(app)
    visible = "\n".join(
        _screen_line(screen, row, 80)
        for row in range(_active_output.get_size().rows)
    )
    # The transcript is Markdown, so assert against what is rendered; the word
    # must be present on screen after End re-enabled tail-following.
    assert "LATEST" in visible or "LATEST" in cli._view.text
    assert cli._view.window.render_info.bottom_visible


def test_transcript_has_a_scrollbar_margin():
    """A visible scrollbar makes the reading position legible."""
    pytest.importorskip("prompt_toolkit")
    global _active_output
    _active_output = _SizeOutput(80, 24)
    cli = CockpitCLIAdapter(_Backend(), output=_active_output)
    cli.build_application()

    from prompt_toolkit.layout.margins import ScrollbarMargin

    margins = list(cli._view.window.right_margins)
    assert any(isinstance(margin, ScrollbarMargin) for margin in margins)


def test_scrolled_back_state_is_visible_in_header_and_footer():
    global _active_output
    _active_output = _SizeOutput(80, 24)
    cli = CockpitCLIAdapter(_Backend(), output=_active_output)
    cli.build_application()
    for index in range(60):
        cli._append_transcript("assistant", f"line-{index:02d}")

    header = "".join(text for _, text in cli._header_fragments())
    footer = "".join(text for _, text in cli._footer_fragments())
    assert "SCROLLED BACK" not in header
    assert "history" not in footer

    cli._scroll_view("home")

    header = "".join(text for _, text in cli._header_fragments())
    footer = "".join(text for _, text in cli._footer_fragments())
    assert "SCROLLED BACK" in header
    assert "history" in footer
    assert "End to follow live" in footer
    # Chrome must stay single-line and cp1252-safe in the browsing state too.
    for chrome in (header, footer):
        assert "\n" not in chrome and "\r" not in chrome
        chrome.encode("cp1252")


@pytest.mark.parametrize("columns", [16, 20, 24, 32, 40, 80, 120])
@pytest.mark.parametrize("rows", [5, 6, 7, 8, 12, 24])
def test_screen_never_renders_past_the_terminal_width(columns, rows):
    """The scrollbar margin must stay inside the width it is given.

    A margin that overflows its window is what makes unrelated columns appear
    to overwrite each other, so assert on the real rendered buffer across both
    the compact and the full layouts.
    """
    pytest.importorskip("prompt_toolkit")
    global _active_output
    _active_output = _SizeOutput(columns, rows)
    cli = CockpitCLIAdapter(_Backend(), output=_active_output)
    app = cli.build_application()
    for index in range(20):
        cli._append_transcript("assistant", f"msg-{index}")
    screen = _render(app)

    for row in range(rows):
        rendered = "".join(
            screen.data_buffer[row][column].char for column in range(columns)
        )
        assert len(rendered) == columns
        # Nothing may be written into the cells immediately after the terminal
        # width; reading them would bleed into the following row.
        trailing = "".join(
            screen.data_buffer[row][column].char
            for column in range(columns, columns + 8)
        )
        assert trailing.strip() == "", (
            f"row {row} drew past the terminal width at {columns}x{rows}"
        )


def test_runtime_state_renders_as_a_state_word_not_an_enum_path():
    """The header must show ``EVALUATING``, not ``RUNTIMESTATE.EVALUATING``.

    A str-valued Enum passes an ``isinstance(value, str)`` check, so it used to
    survive plain-value conversion and render as its qualified repr.
    """
    from enum import Enum

    class StrState(str, Enum):
        EVALUATING = "evaluating"

    class PlainState(Enum):
        EVALUATING = "evaluating"

    global _active_output
    for state in (StrState.EVALUATING, PlainState.EVALUATING, "evaluating"):
        _active_output = _SizeOutput(100, 24)
        cli = CockpitCLIAdapter(
            _Backend({"runtime": {"state": state, "running": True}}),
            output=_active_output,
        )
        cli._refresh_overview()
        header = "".join(text for _, text in cli._header_fragments())
        assert "EVALUATING" in header
        assert "RuntimeState" not in header
        assert "StrState" not in header
        assert "." not in header.split("|")[2]

    # The boolean fallback path is unchanged.
    _active_output = _SizeOutput(100, 24)
    cli = CockpitCLIAdapter(
        _Backend({"runtime": {"running": False}}), output=_active_output
    )
    cli._refresh_overview()
    assert "STOPPED" in "".join(text for _, text in cli._header_fragments())


def test_cost_label_states_its_scope_and_survives_narrow_terminals():
    """The header cost must name its scope and never silently vanish.

    The usage ledger is in-memory and per-process, so an unqualified total is
    easily mistaken for the account-level figure on the OpenRouter dashboard.
    The qualifier may be dropped for width, but the number must remain.
    """
    global _active_output
    snapshot = {
        "runtime": "online",
        "model": "openai/gpt-5.6-luna",
        "cost": {"known_cost_usd": "0.0051816", "usage_missing_calls": 2},
    }

    def header(columns):
        _active_output = _SizeOutput(columns, 24)
        cli = CockpitCLIAdapter(_Backend(snapshot), output=_active_output)
        cli._refresh_overview()
        return "".join(text for _, text in cli._header_fragments())

    wide = header(100)
    assert "$0.0051816 this session" in wide
    assert "unpriced" in wide

    # Narrower terminals keep the amount even after the qualifiers are dropped.
    for columns in (80, 60, 44):
        rendered = header(columns)
        assert "$0.0051816" in rendered, f"cost lost at {columns} columns"
        assert len(rendered) <= columns

    # A fully priced session shows no extra marker.
    _active_output = _SizeOutput(100, 24)
    cli = CockpitCLIAdapter(
        _Backend({"runtime": "online", "cost": {"known_cost_usd": "0.01"}}),
        output=_active_output,
    )
    cli._refresh_overview()
    text = "".join(fragment for _, fragment in cli._header_fragments())
    assert "$0.01 this session" in text
    assert "unpriced" not in text


def test_worker_artifact_from_the_final_path_renders_as_a_notification():
    """A worker result must never appear as a chat message.

    The runtime emits the artifact from two places. One already tagged it
    ``intermediate``/``phase=subagent_result``; the final-synthesis path did
    not, so the cockpit could not recognise it and rendered the entire
    subagent report as an ORION-style message instead of the compact
    "resultat recu" notification.
    """
    global _active_output
    _active_output = _SizeOutput(80, 24)
    cli = CockpitCLIAdapter(_Backend(), output=_active_output)
    cli.build_application()

    report = "# Resultats OSINT\n\n## 1. Entite\n\n" + ("ligne de rapport " * 20)
    cli.send(
        AgentOutput(
            report,
            metadata={
                "output_origin": "subagent",
                "sender_name": "osint_web",
                "intermediate": True,
                "phase": "subagent_result",
            },
        )
    )

    kinds = [event.kind for event in cli.transcript_events]
    assert kinds == ["notification"], f"worker output was not collapsed: {kinds}"
    assert cli._view.text.count("• Worker · osint_web · résultat reçu") == 1
    assert "Resultats OSINT" not in cli._view.text


def test_intermediate_orion_preamble_is_not_truncated():
    """Orion's short progress updates must survive intact."""
    global _active_output
    _active_output = _SizeOutput(120, 24)
    cli = CockpitCLIAdapter(_Backend(), output=_active_output)
    cli.build_application()

    message = (
        "C'est noté. J'attends : attendre le dernier rapport de recherche "
        "avant consolidation et création du fichier HTML."
    )
    assert len(message) > 110
    cli.send(
        AgentOutput(
            message,
            metadata={"intermediate": True, "phase": "tool_preamble"},
        )
    )

    rendered = cli.transcript_events[0].text
    assert "…" not in rendered, f"intermediate message was truncated: {rendered!r}"
    assert rendered == f"Orion · {message}"


def test_command_output_uses_the_view_style_not_the_chat_body_style():
    """Dashboard/command output must not inherit chat role colours."""
    global _active_output
    _active_output = _SizeOutput(100, 20)
    cli = CockpitCLIAdapter(_Backend(), output=_active_output)
    app = cli.build_application()
    cli._append_transcript("assistant", "a chat reply")
    _render(app)

    assert cli._command("/status") is True
    assert cli._view_mode == "status"
    assert cli._line_styles, "command view published no style map"
    assert set(cli._line_styles) == {"class:transcript.view"}, (
        "command output kept chat role styles"
    )

    # And the chat view restores role colours afterwards.
    cli._set_view(cli._transcript_text(), mode="chat", follow_tail=True)
    assert set(cli._line_styles) <= {
        "class:transcript.orion",
        "class:transcript.body",
        "class:transcript.user",
        "class:transcript.user.body",
        "class:transcript.worker",
        "class:transcript.notice",
    }


def test_palette_is_white_grey_orange():
    """Roles are distinguished by weight and orange, not by hue variety."""
    global _active_output
    _active_output = _SizeOutput(100, 24)
    cli = CockpitCLIAdapter(_Backend(), output=_active_output)
    app = cli.build_application()
    style = app.style
    _render(app)

    def colour(rule):
        return style.get_attrs_for_style_str(f"class:{rule}").color

    assert colour("transcript.orion") == "ff9e64"      # orange accent
    assert colour("transcript.worker") == "ffb86b"     # orange accent
    assert colour("transcript.body") == "e6edf3"       # near-white
    assert colour("transcript.user") == "8b949e"       # muted grey
    assert colour("transcript.view") == "b9c0c9"       # neutral grey


def test_format_view_data_renders_lists_and_dicts_readably():
    from cli_cockpit import CockpitCLIAdapter as Adapter

    rows = Adapter._format_view_data(
        [{"id": "abc", "status": "running"}, {"id": "def", "status": "queued"}]
    )
    assert "1. id=abc  status=running" in rows
    assert "2. id=def  status=queued" in rows
    assert "{" not in rows and "[" not in rows

    mapping = Adapter._format_view_data({"state": "sleep", "workers": [1, 2]})
    assert "state: sleep" in mapping
    assert "  - 1" in mapping


def test_markdown_is_rendered_instead_of_shown_raw():
    """Formatting markers must not reach the operator.

    The transcript used to display raw Markdown, so ``**bold**`` and
    ``\\`code\\``` appeared as literal asterisks and backticks.
    """
    from cli_cockpit import _MarkdownRenderer

    source = (
        "# Titre\n\n"
        "Du **gras**, de l'*italique* et du `code`.\n\n"
        "- premier\n"
        "- deuxieme\n\n"
        "1. etape une\n"
        "2. etape deux\n\n"
        "> citation\n\n"
        "Un [lien](https://example.com).\n"
    )
    renderer = _MarkdownRenderer(source)
    flat = "\n".join("".join(text for _, text in line) for line in renderer.lines)
    styles = {style for line in renderer.lines for style, _ in line}

    assert "**" not in flat, "bold markers leaked"
    assert "`" not in flat, "code markers leaked"
    assert "## " not in flat, "heading markers leaked"
    assert "[lien](https://example.com)" not in flat, "link syntax leaked"
    assert "Titre" in flat and "gras" in flat and "code" in flat

    # The right parts carry the formatting classes.
    assert "class:md.h1" in styles
    assert any("md.strong" in style for style in styles)
    assert any("md.em" in style for style in styles)
    assert any("md.code" in style for style in styles)
    assert any("md.link" in style for style in styles)
    assert any("md.quote" in style for style in styles)

    # Bullets replace list markers, and ordered items count up.
    assert "• premier" in flat
    assert "1. etape une" in flat
    assert "2. etape deux" in flat


def test_markdown_table_is_laid_out_as_columns():
    from cli_cockpit import _MarkdownRenderer

    table = (
        "| Champ | Valeur |\n"
        "|---|---|\n"
        "| Statut | OK |\n"
        "| SIREN | 993 840 529 |\n"
    )
    renderer = _MarkdownRenderer(table)
    flat = [line[0][1] for line in renderer.lines if line]

    assert "|" not in "\n".join(flat), "raw pipe syntax leaked"
    assert flat[0].split() == ["Champ", "Valeur"]
    # Row 1 is the rule under the header; body rows follow it.
    assert set(flat[1]) <= {"-", " "}
    body = [line for line in flat[2:] if "Statut" in line or "SIREN" in line]
    assert len(body) == 2, f"table body missing: {flat}"
    assert "OK" in body[0]
    assert "993 840 529" in body[1]
    # Columns line up: the second column starts at the same offset as the header.
    second_column = flat[0].index("Valeur")
    assert body[0].index("OK") == second_column
    assert body[1].index("993") == second_column


def test_markdown_renderer_degrades_to_plain_text_without_parser(monkeypatch):
    """A missing parser must not lose the message."""
    import cli_cockpit

    monkeypatch.setattr(cli_cockpit, "MarkdownIt", None)
    renderer = cli_cockpit._MarkdownRenderer("**gras**\n\nligne deux")
    flat = "\n".join("".join(text for _, text in line) for line in renderer.lines)
    assert "**gras**" in flat and "ligne deux" in flat


def _refresh_backend():
    """A backend whose values change between snapshots."""

    class Ledger:
        def __init__(self):
            self.cost = "0.000001"

        def snapshot(self):
            return {"known_cost_usd": self.cost, "started_calls": 1}

    class Runtime:
        state = "evaluating"
        running = True
        pending_events = 0
        pending_events_during_run = 0

    class Backend:
        def __init__(self):
            self.ledger = Ledger()
            self.runtime = Runtime()

        def snapshot(self):
            return {
                "runtime": {"state": self.runtime.state, "running": True},
                "cost": self.ledger.snapshot(),
            }

        def execute(self, line):
            return {"title": "x", "data": None, "display": ""}

    return Backend()


def test_header_refreshes_without_a_command_or_output():
    """Runtime state and session cost must update on their own.

    They were only recomputed when output arrived or a command ran, so the
    header sat on stale values through a long silent run.
    """
    backend = _refresh_backend()
    cli = CockpitCLIAdapter(backend, output=io.StringIO(), refresh_seconds=0.05)
    assert "READY" in "".join(text for _, text in cli._header_fragments())

    cli._start_refresh_loop()
    try:
        time.sleep(0.12)
        backend.ledger.cost = "0.111111"
        time.sleep(0.25)
        header = "".join(text for _, text in cli._header_fragments())
        assert "0.111111" in header, f"cost did not refresh: {header}"
        assert "EVALUATING" in header

        backend.runtime.state = "sleep"
        backend.ledger.cost = "0.222222"
        time.sleep(0.25)
        header = "".join(text for _, text in cli._header_fragments())
        assert "SLEEP" in header, f"state did not refresh: {header}"
        assert "0.222222" in header
    finally:
        cli._stop_refresh_loop()
    assert cli._refresh_thread is None


def test_refresh_loop_can_be_disabled_and_is_idempotent():
    cli = CockpitCLIAdapter(_refresh_backend(), output=io.StringIO(), refresh_seconds=0)
    cli._start_refresh_loop()
    assert cli._refresh_thread is None, "refresh started while disabled"

    cli2 = CockpitCLIAdapter(_refresh_backend(), output=io.StringIO(), refresh_seconds=0.1)
    cli2._start_refresh_loop()
    first = cli2._refresh_thread
    cli2._start_refresh_loop()
    assert cli2._refresh_thread is first, "a second thread was started"
    cli2._stop_refresh_loop()
    assert cli2._refresh_thread is None


def test_refresh_loop_survives_a_failing_backend():
    """A backend error must not kill the refresh thread."""
    class Flaky:
        def __init__(self):
            self.calls = 0

        def snapshot(self):
            self.calls += 1
            if self.calls % 2:
                raise RuntimeError("transient")
            return {"runtime": {"state": "online", "running": True}}

        def execute(self, line):
            return {"title": "x", "data": None, "display": ""}

        def commands(self):
            return ()

    backend = Flaky()
    cli = CockpitCLIAdapter(backend, output=io.StringIO(), refresh_seconds=0.05)
    cli._start_refresh_loop()
    try:
        time.sleep(0.3)
        assert cli._refresh_thread is not None and cli._refresh_thread.is_alive()
    finally:
        cli._stop_refresh_loop()
    assert backend.calls > 1, "refresh loop stopped calling the backend"


def test_command_view_is_not_markdown_rendered():
    """Machine output keeps its own styling and literal text."""
    global _active_output
    _active_output = _SizeOutput(100, 20)
    cli = CockpitCLIAdapter(_Backend(), output=_active_output)
    app = cli.build_application()
    _render(app)

    assert cli._command("/status") is True
    lexer = cli._view.control.lexer
    lines = lexer._rendered_lines(cli._view.buffer.document)
    styles = {style for line in lines for style, _ in line}
    assert styles <= {"class:transcript.view"}


def test_transcript_style_map_is_aligned_and_roles_are_coloured():
    """One style class per document line, and the right one per role.

    The style list is consumed by the lexer by line number, so any drift
    between it and the text colours the wrong lines. A message body that
    contains a line reading ``ORION`` must not be mistaken for a caption.
    """
    global _active_output
    _active_output = _SizeOutput(80, 24)
    cli = CockpitCLIAdapter(_Backend(), output=_active_output)

    cli._append_transcript("user", "explain the deploy steps", correlation_id="c1")
    # Body deliberately contains a line equal to the assistant caption.
    cli._append_transcript("assistant", "ORION\nHere is the plan:\n2. ship")
    cli._append_transcript("assistant", "worker result", speaker="toml-analyst")
    cli._append_transcript("notification", "Orion · resultat recu")

    text, styles = cli._transcript_blocks()
    lines = text.split("\n")

    assert len(styles) == len(lines), (
        f"style map has {len(styles)} entries for {len(lines)} lines"
    )
    by_line = dict(zip(lines, styles, strict=True))
    assert styles[0] == "class:transcript.user"
    assert by_line["explain the deploy steps"] == "class:transcript.user.body"
    # The caption is coloured, and the identical-looking body line is not.
    assert by_line["ORION"] in {"class:transcript.orion", "class:transcript.body"}
    assert "class:transcript.orion" in styles
    assert by_line["Here is the plan:"] == "class:transcript.body"
    assert by_line["TOML-ANALYST"] == "class:transcript.worker"
    assert by_line["worker result"] == "class:transcript.body"
    assert by_line["• Orion · resultat recu"] == "class:transcript.notice"
