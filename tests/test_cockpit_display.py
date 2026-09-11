"""Acceptance contracts for the human-facing cockpit display.

These tests intentionally assert readable output rather than implementation
details of prompt-toolkit widgets.
"""

import asyncio
import io

from cli_cockpit import CockpitCLIAdapter
from channels import AgentOutput
from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.keys import Keys


class DisplayBackend:
    def __init__(self):
        self.refreshes = 0

    def snapshot(self):
        self.refreshes += 1
        return {
            "runtime": {"state": "online", "running": True},
            "tasks": [{"id": 7, "status": "running"}],
            "agents": ["worker-a"],
            "workspace": "repo",
        }

    def execute(self, command):
        return {"title": command.lstrip("/").upper(), "data": {"ok": True}}


def _enter(app):
    async def drive():
        app.loop = asyncio.get_running_loop()
        app.key_processor.feed(KeyPress(Keys.Enter))
        app.key_processor.process_keys()
        await asyncio.sleep(0)

    asyncio.run(drive())


def test_dashboard_has_compact_banner_and_readable_sections():
    backend = DisplayBackend()
    cli = CockpitCLIAdapter(backend, output=io.StringIO())
    text = cli._dashboard_text()
    assert text.startswith("ORION")
    for section in ("RUNTIME", "TASKS", "AGENTS", "WORKSPACE"):
        assert section in text
    assert "{'state':" not in text
    assert '"state": "online"' not in text
    assert "state: online" in text.lower()


def test_three_assistant_messages_remain_in_transcript_after_returning_from_dashboard(
    monkeypatch,
):
    import cli_cockpit
    from prompt_toolkit.output import DummyOutput

    real_application = cli_cockpit.Application
    monkeypatch.setattr(
        cli_cockpit,
        "Application",
        lambda *args, **kwargs: real_application(*args, output=DummyOutput(), **kwargs),
    )
    cli = CockpitCLIAdapter(DisplayBackend(), output=io.StringIO())
    app = cli.build_application()
    cli.start(lambda _: None)
    for n in range(1, 4):
        cli.send(AgentOutput(content=f"answer-{n}"))
    editor = app.layout.current_control.buffer
    editor.text = "/dashboard"
    _enter(app)
    rendered = cli._view.text
    assert all(f"answer-{n}" in rendered for n in range(1, 4))


def test_assistant_outputs_are_distinct_rendered_entries():
    out = io.StringIO()
    cli = CockpitCLIAdapter(DisplayBackend(), output=out)
    cli.send(AgentOutput(content="first"))
    cli.send(AgentOutput(content="second"))
    rendered = out.getvalue()
    assert rendered.count("Orion") == 2
    assert rendered.index("first") < rendered.index("second")
    assert cli.transcript == ["first", "second"]


def test_dashboard_command_is_explicit_and_watch_refreshes_snapshot():
    backend = DisplayBackend()
    out = io.StringIO()
    cli = CockpitCLIAdapter(backend, output=out)
    cli.loop_input = None
    assert cli._command("/dashboard")
    assert cli._command("/watch")
    rendered = out.getvalue()
    assert "DASHBOARD" in rendered and "WATCH" in rendered
    assert backend.refreshes >= 2


def test_help_dashboard_clear_transitions_preserve_history_and_clean_view(monkeypatch):
    import cli_cockpit
    from prompt_toolkit.output import DummyOutput

    real_application = cli_cockpit.Application
    monkeypatch.setattr(
        cli_cockpit,
        "Application",
        lambda *args, **kwargs: real_application(*args, output=DummyOutput(), **kwargs),
    )
    cli = CockpitCLIAdapter(DisplayBackend(), output=io.StringIO())
    app = cli.build_application()
    cli.start(lambda _: None)
    cli.send(AgentOutput(content="réponse — unicode ✅"))
    editor = app.layout.current_control.buffer

    editor.text = "/help"
    _enter(app)
    assert cli._view_mode == "help"
    assert cli._view.text.startswith("ORION COMMAND PALETTE")

    editor.text = "/dashboard"
    _enter(app)
    assert cli._view_mode == "dashboard"
    assert "RUNTIME" in cli._view.text

    editor.text = "/clear"
    _enter(app)
    assert cli._view_mode == "chat"
    assert cli._view.text == ""
    assert cli._follow_tail is True
    assert any(event.text == "réponse — unicode ✅" for event in cli.transcript_events)

    cli.send(AgentOutput(content="after-clear"))
    assert "after-clear" in cli._view.text
    assert "réponse — unicode ✅" not in cli._view.text
