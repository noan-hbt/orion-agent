"""Black-box acceptance checks for the conversation-first cockpit.

These tests deliberately exercise the adapter with real file-like streams (and
one concurrent producer), rather than asserting implementation details or
using a mocked renderer.
"""
import io
import os
import threading
import time
from types import SimpleNamespace
import pytest

from cli_cockpit import CockpitCLIAdapter
from cli_cockpit_backend import CockpitBackend
from channels import AgentOutput


def test_prompt_toolkit_real_input_output_and_async_redraw():
    """Exercise the actual PTK pipe/input session, when PTK is installed."""
    try:
        from prompt_toolkit.input import create_pipe_input
        from cli_ui import CLIConsole
    except ImportError:
        pytest.skip("prompt-toolkit unavailable")

    class TTYOutput(io.StringIO):
        def isatty(self):
            return True

    class TTYInput:
        def __init__(self, wrapped):
            self.wrapped = wrapped
        def isatty(self):
            return True
        def __getattr__(self, name):
            return getattr(self.wrapped, name)

    output = TTYOutput()
    with create_pipe_input() as pipe:
        console = CLIConsole(output=output, input_stream=TTYInput(pipe), show_banner=False,
                             history_path=None, render_markdown=False)
        if not console._interactive:
            pytest.skip("prompt-toolkit pipe input unavailable on this platform")
        result = []
        reader = threading.Thread(target=lambda: result.append(console.read()))
        reader.start()
        time.sleep(0.05)
        console.system("worker update")
        pipe.send_text("hello\n")
        reader.join(timeout=2)
        assert not reader.is_alive()
        assert result == ["hello"]


class _Runtime:
    state = "online"
    running = True


class _Tasks:
    def list(self):
        return [{"id": 42, "status": "running", "objective": "QA"}]


def _adapter(text=""):
    out = io.StringIO()
    backend = CockpitBackend({"runtime": _Runtime(), "task_store": _Tasks(), "workspace": "repo"})
    return CockpitCLIAdapter(backend, input=io.StringIO(text), output=out), out


def test_non_tty_commands_render_real_snapshot_and_unknown_error():
    cli, out = _adapter("/status\n/dashboard\n/watch\n/nope\n/exit\n")
    cli.loop()
    rendered = out.getvalue()
    assert "STATUS" in rendered and "DASHBOARD" in rendered and "WATCH" in rendered
    assert '"state": "online"' in rendered
    assert '"id": 42' in rendered
    assert "Commande inconnue" in rendered
    assert not cli.running


def test_natural_language_is_delivered_as_inbound_message():
    cli, _ = _adapter()
    received = []
    cli.start(received.append)
    message = cli.submit("  analyse le repo  ")
    assert message.text == "analyse le repo"
    assert received == [message]
    assert message.channel == "cli"
    assert message.correlation_id == "cli-1"


def test_help_is_a_recognized_user_command_not_an_unknown_command():
    """The interactive cockpit must expose a functional help entry point."""
    cli, out = _adapter("/help\n/exit\n")

    cli.loop()

    rendered = out.getvalue()
    assert "Commande inconnue" not in rendered
    # Help content may be redesigned, but it must at least advertise useful
    # commands rather than silently succeeding with an empty response.
    assert "/status" in rendered
    assert "/tools" in rendered


def test_help_command_argument_uses_targeted_backend_help():
    class HelpBackend:
        def __init__(self):
            self.executed = []

        def commands(self):
            return ("status", "tools")

        def execute(self, command):
            self.executed.append(command)
            assert command == "/help tools"
            return {
                "title": "help",
                "data": {
                    "command": "tools",
                    "usage": "/tools",
                    "description": "TARGETED TOOLS HELP",
                    "available": True,
                },
            }

    backend = HelpBackend()
    out = io.StringIO()
    cli = CockpitCLIAdapter(
        backend,
        input=io.StringIO("/help tools\n/exit\n"),
        output=out,
    )

    cli.loop()

    assert backend.executed == ["/help tools"]
    assert "TARGETED TOOLS HELP" in out.getvalue()


def test_tools_lists_tools_available_to_runtime_even_if_package_scan_is_empty():
    """/tools describes callable runtime tools, not merely packages on disk."""

    class RuntimeWithTools(_Runtime):
        def _tool_definitions(self):
            return [
                {
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "description": "search the web",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ]

    class EmptyPackageScan:
        def installed(self):
            return []

    application = SimpleNamespace(
        runtime=RuntimeWithTools(),
        tool_manager=EmptyPackageScan(),
    )
    out = io.StringIO()
    cli = CockpitCLIAdapter(
        CockpitBackend(application),
        input=io.StringIO("/tools\n/exit\n"),
        output=out,
    )

    cli.loop()

    rendered = out.getvalue()
    assert "Commande inconnue" not in rendered
    assert "Provider indisponible" not in rendered
    assert "web_search" in rendered


def test_natural_language_is_not_reparsed_as_a_backend_command():
    """Plain text is conversation input and must not emit command-parser noise."""
    class BackendProbe:
        def __init__(self):
            self.executed = []

        def execute(self, command):
            self.executed.append(command)
            raise AssertionError("natural text reached command backend")

    out = io.StringIO()
    received = []
    backend = BackendProbe()
    cli = CockpitCLIAdapter(
        backend,
        input=io.StringIO("analyse le repo\n/exit\n"),
        output=out,
    )
    cli.start(received.append)

    cli.loop()

    assert [message.text for message in received] == ["analyse le repo"]
    assert backend.executed == []
    rendered = out.getvalue()
    assert "Commande inconnue" not in rendered
    assert "/analyse" not in rendered


def test_clear_is_presentation_only_and_non_tty_safe(monkeypatch):
    class BackendProbe:
        def __init__(self):
            self.executed = []

        def execute(self, command):
            self.executed.append(command)
            raise AssertionError("/clear reached command backend")

    shell_calls = []
    monkeypatch.setattr(os, "system", lambda command: shell_calls.append(command) or 0)
    backend = BackendProbe()
    out = io.StringIO()
    cli = CockpitCLIAdapter(
        backend,
        input=io.StringIO("/clear\n/exit\n"),
        output=out,
    )
    cli.start(lambda _message: None)
    cli.submit("conversation history stays durable")
    before = tuple(cli.transcript_events)

    cli.loop()

    assert tuple(cli.transcript_events) == before
    assert backend.executed == []
    assert shell_calls == []
    assert "\x1b[" not in out.getvalue()


def test_output_can_arrive_while_input_loop_is_blocked():
    class BlockingInput:
        def __iter__(self):
            yield "/exit\n"

    out = io.StringIO()
    cli = CockpitCLIAdapter(CockpitBackend({}), input=BlockingInput(), output=out)
    cli.start(lambda _: None)
    worker = threading.Thread(target=cli.loop)
    worker.start()
    # send() is the same path used by asynchronous workers and must be safe.
    cli.send(AgentOutput(content="worker update"))
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert "worker update" in out.getvalue()


def test_eof_and_stop_are_clean_and_idempotent():
    cli, _ = _adapter("")
    cli.loop()  # EOF from a real non-TTY stream
    assert not cli.running
    cli.stop()
    cli.stop()
    assert not cli.running
