"""Black-box acceptance checks for the conversation-first cockpit.

These tests deliberately exercise the adapter with real file-like streams (and
one concurrent producer), rather than asserting implementation details or
using a mocked renderer.
"""
import io
import threading
import time
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
