"""Conversation-first terminal cockpit for Orion.

The adapter deliberately owns all terminal output.  Core/runtime code only sees
normalised ``InboundMessage`` objects through ``start``'s callback.
"""
from __future__ import annotations

import json
import sys
import threading
from dataclasses import dataclass
from typing import Any, Callable, TextIO

from channels import AgentOutput, InboundMessage

try:
    from prompt_toolkit.application import Application
    from prompt_toolkit.layout import Layout, HSplit, VSplit
    from prompt_toolkit.widgets import TextArea
    from prompt_toolkit.key_binding import KeyBindings
except ImportError:  # pragma: no cover
    Application = None

@dataclass(frozen=True)
class TranscriptEvent:
    kind: str
    text: str
    correlation_id: str | None = None


class CockpitCLIAdapter:
    name = "cli"

    def __init__(self, backend: Any, *, input: TextIO | None = None,
                 output: TextIO | None = None, prompt: str = "orion ❯ ") -> None:
        self.backend = backend
        self.input = input or sys.stdin
        self.output = output or sys.stdout
        self.prompt = prompt
        self._on_message: Callable[[InboundMessage], Any] | None = None
        self._running = False
        self._stop = threading.Event()
        self._write_lock = threading.RLock()
        self._correlation = 0
        self._exit_handler: Callable[[], Any] | None = None
        self.transcript: list[str] = []
        self.transcript_events: list[TranscriptEvent] = []
        self._app = None
        self._view = None

    @property
    def running(self) -> bool:
        return self._running

    def start(self, on_message: Callable[[InboundMessage], Any]) -> None:
        self._on_message = on_message
        self._running = True
        self._stop.clear()

    def stop(self) -> None:
        self._running = False
        self._stop.set()
        if self._app is not None:
            try: self._app.exit()
            except Exception: pass

    def set_exit_handler(self, handler: Callable[[], Any]) -> None:
        self._exit_handler = handler

    def _write(self, text: str) -> None:
        with self._write_lock:
            self.output.write(text)
            self.output.flush()

    def send(self, output: AgentOutput) -> AgentOutput:
        """Render an output atomically; safe to call from worker threads."""
        text = getattr(output, "text", None) or str(output)
        self.transcript.append(text)
        self.transcript_events.append(TranscriptEvent("assistant", text,
                                                     getattr(output, "correlation_id", None)))
        if self._view is not None:
            self._view.text = self._view.text + "\n● Orion\n" + text
            if self._app is not None: self._app.invalidate()
            return output
        self._write(f"● Orion\n{text}\n")
        return output

    def submit(self, text: str) -> InboundMessage:
        text = text.strip()
        self._correlation += 1
        message = InboundMessage(text=text, channel=self.name, source=self.name,
                                 correlation_id=f"cli-{self._correlation}", payload={"text": text})
        if self._on_message is not None:
            self._on_message(message)
        return message

    def _render_result(self, result: Any) -> None:
        if isinstance(result, dict):
            if result.get("error"):
                self._write(f"✗ {result['error']}\n")
                return
            title = result.get("title")
            data = result.get("data")
            if title:
                self._write(f"{title}\n")
            if data is not None:
                self._write((json.dumps(data, ensure_ascii=False, indent=2, default=str)
                             if isinstance(data, (dict, list)) else f"{data}") + "\n")
        elif result is not None:
            self._write(f"{result}\n")

    def _snapshot(self) -> dict[str, Any]:
        value = self.backend.snapshot()
        return value if isinstance(value, dict) else {"data": value}

    def _dashboard_text(self) -> str:
        s = self._snapshot()
        def lines(name: str) -> list[str]:
            value = s.get(name)
            if value is None: return [f"{name.upper()}: unavailable"]
            if isinstance(value, list):
                return [f"{name.upper()}: "] + [f"  {x}" for x in value]
            return [f"{name.upper()}: {value}"]
        return "\n".join(lines("runtime") + lines("tasks") + lines("agents") + lines("workspace"))

    def build_application(self):
        """Build the sole screen owner used for interactive terminals."""
        if Application is None: raise RuntimeError("prompt-toolkit unavailable")
        transcript = TextArea(text=self._dashboard_text(), read_only=True, scrollbar=True)
        self._view = transcript
        editor = TextArea(height=1, prompt=self.prompt, multiline=True)
        bindings = KeyBindings()
        self._alt_pending = False
        @bindings.add("enter")
        def _submit(event):
            if self._alt_pending:
                self._alt_pending = False
                event.current_buffer.insert_text("\n")
                return
            value = editor.text.strip(); editor.text = ""
            if value:
                if value.startswith("/"):
                    if not self._command(value): event.app.exit()
                else: self.submit(value)
                # Natural language is delivered to the runtime callback; only
                # explicit slash commands are handled by the cockpit backend.
            transcript.text = self._dashboard_text()
            event.app.invalidate()
        @bindings.add("c-c")
        def _stop(event): self.stop(); event.app.exit()
        @bindings.add("escape", "enter", eager=True)
        def _newline(event): event.current_buffer.insert_text("\n")
        @bindings.add("escape")
        def _escape(event):
            self._alt_pending = True
            event.current_buffer.insert_text("\n")
        @bindings.add("c-d")
        def _eof(event): self.stop()
        self._app = Application(layout=Layout(HSplit([transcript, editor]), focused_element=editor),
                            key_bindings=bindings, full_screen=True, mouse_support=False)
        return self._app

    def run_tui(self) -> None:
        app = self.build_application()
        self.start(self._on_message or (lambda _: None))
        try: app.run()
        finally: self.stop()

    def _command(self, line: str) -> bool:
        command, _, arg = line.partition(" ")
        if command in {"/exit", "/quit"}:
            self.stop(); return False
        if command == "/commands":
            self._write("/commands /status /dashboard /watch /exit\n"); return True
        if command in {"/status", "/dashboard", "/watch"}:
            snap = self._snapshot()
            self._render_result({"title": command[1:].upper(), "data": snap}); return True
        if command.startswith("/"):
            self._render_result(self.backend.execute(line)); return True
        return True

    def loop(self) -> None:
        if not self._running:
            self.start(lambda _: None)
        if (callable(getattr(self.input, "isatty", None)) and self.input.isatty()
                and callable(getattr(self.output, "isatty", None)) and self.output.isatty()
                and Application is not None):
            self.run_tui()
            return
        for raw in self.input:
            if self._stop.is_set(): break
            line = raw.rstrip("\r\n")
            if not line: continue
            if line.startswith("/") and not self._command(line): break
            if line.startswith("/"): continue
            self.submit(line)
            try:
                self._render_result(self.backend.execute(line))
            except Exception as exc:
                self._write(f"✗ {exc}\n")
        # Iteration ending is EOF (normal for pipes and StringIO), not a live
        # session.  Keep stop idempotent so callers can safely stop afterwards.
        self.stop()


OrionCockpitCLI = CockpitCLIAdapter
CLI = CockpitCLIAdapter

__all__ = ["CLI", "CockpitCLIAdapter", "OrionCockpitCLI", "TranscriptEvent"]
