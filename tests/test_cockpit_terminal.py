"""Terminal-level checks for the prompt-toolkit cockpit screen.

These deliberately target ``cli_cockpit`` and do not exercise the legacy
``cli_ui`` console.
"""
import io
import threading

import pytest

from channels import AgentOutput
from cli_cockpit import CockpitCLIAdapter


@pytest.fixture(autouse=True)
def _use_test_output(monkeypatch):
    """Avoid Win32Output probing: this runner has no attached ConPTY."""
    import prompt_toolkit.output.defaults as defaults
    monkeypatch.setattr(defaults, "create_output", lambda: _active_output)


_active_output = None


from prompt_toolkit.output import DummyOutput


class _SizeOutput(DummyOutput):
    def __init__(self, columns, rows):
        self._size = (columns, rows)

    def get_size(self):
        from prompt_toolkit.data_structures import Size
        return Size(rows=self._size[1], columns=self._size[0])

class _Backend:
    def snapshot(self):
        return {"runtime": "online", "tasks": [], "agents": [], "workspace": "test"}


@pytest.mark.parametrize("columns", [40, 80, 120])
def test_cockpit_builds_ptk_screen_at_terminal_width(columns):
    pytest.importorskip("prompt_toolkit")
    global _active_output
    _active_output = _SizeOutput(columns, 24)
    cli = CockpitCLIAdapter(_Backend(), output=_active_output)
    app = cli.build_application()
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
    assert cli.prompt == "orion ❯ "


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
