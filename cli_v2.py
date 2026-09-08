"""Second-generation Orion terminal client.

This module is deliberately independent from :mod:`cli_ui`: it is a small
adapter and transcript renderer built on the channel contracts.  It has no
optional UI dependencies, so it also works in pipes and in minimal installs.
"""
from __future__ import annotations

import json
import shutil
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, TextIO

from channels import AgentOutput, InboundMessage


@dataclass(frozen=True)
class TranscriptEvent:
    """Immutable item in the append-only terminal transcript."""
    kind: str
    text: str = ""
    event_id: str = ""
    correlation_id: str | None = None
    sequence: int = 0
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    state: str = "complete"
    metadata: dict[str, Any] = field(default_factory=dict)


class OrionCLIAdapter:
    """Channel adapter plus a dependency-free terminal view.

    ``send`` is safe to call from worker threads.  It is intentionally the
    only method that writes agent output, which prevents interleaved fragments.
    """
    name = "cli"

    def __init__(self, *, input: TextIO | None = None, output: TextIO | None = None,
                 mode: str = "terminal", banner: bool = True, width: int | None = None) -> None:
        if mode not in {"terminal", "plain", "json", "jsonl"}:
            raise ValueError("mode doit être terminal, plain, json ou jsonl")
        self.input, self.output = input or sys.stdin, output or sys.stdout
        self.mode, self.show_banner, self.width = mode, banner, width
        self._callback: Callable[[InboundMessage], Any] | None = None
        self._lock = threading.RLock()
        self._running = False
        # ``input(prompt)`` writes the prompt itself and the terminal echoes
        # the line.  Rendering the same line from ``submit`` therefore
        # creates the characteristic double ``Salut`` seen in the old CLI.
        # Keep the input protocol explicit so we can distinguish a real TTY
        # from a pipe/StringIO (where there is no terminal echo).
        self._prompt_active = False
        self._sequence = 0
        self.transcript: list[TranscriptEvent] = []
        self.providers: dict[str, Callable[[], Any]] = {}

    def start(self, on_message: Callable[[InboundMessage], Any]) -> None:
        self._callback = on_message
        self._running = True
        if self.show_banner and self.mode == "terminal":
            self._write(self._banner())

    def stop(self) -> None:
        self._running = False
        self._callback = None

    def set_provider(self, name: str, provider: Callable[[], Any]) -> None:
        self.providers[name] = provider

    def __getattr__(self, name: str) -> Any:
        if name.startswith("set_") and name.endswith("_provider"):
            key = name[4:-9]
            return lambda provider: self.set_provider(key, provider)
        raise AttributeError(name)

    def send(self, output: AgentOutput) -> TranscriptEvent:
        metadata = dict(output.metadata or {})
        intermediate = bool(metadata.get("intermediate"))
        error = bool(metadata.get("error"))
        event = TranscriptEvent(
            kind="error" if error else ("stream" if intermediate else "assistant"),
            text=str(output.content or output.text or ""),
            event_id=str(output.event_id or output.output_id or ""),
            correlation_id=output.correlation_id,
            sequence=int(metadata.get("seq", 0) or 0),
            state="failed" if error else ("streaming" if intermediate else "complete"),
            metadata=metadata,
        )
        with self._lock:
            self._sequence += 1
            if not event.sequence:
                event = TranscriptEvent(**{**event.__dict__, "sequence": self._sequence})
            self.transcript.append(event)
            self._render(event)
        return event

    def report_error(self, event: object, error: Exception) -> TranscriptEvent:
        """Rend une erreur du pipeline sans écrire directement depuis le core.

        ``orion_run`` utilise le même contrat de notification pour tous les
        adaptateurs. Garder l'erreur dans le transcript garantit qu'elle est
        sérialisée et verrouillée comme les sorties ordinaires.
        """
        metadata = {
            "error_type": type(error).__name__,
            "event_type": str(getattr(event, "type", "unknown")),
        }
        item = TranscriptEvent(
            kind="error",
            text=f"{type(error).__name__}: {error}",
            event_id=str(getattr(event, "id", "") or ""),
            correlation_id=getattr(event, "correlation_id", None),
            state="failed",
            metadata=metadata,
        )
        with self._lock:
            self._sequence += 1
            item = TranscriptEvent(**{**item.__dict__, "sequence": self._sequence})
            self.transcript.append(item)
            self._render(item)
        return item

    def submit(self, text: str, *, correlation_id: str | None = None) -> InboundMessage:
        text = text.strip()
        message = InboundMessage(channel="cli", source="cli", text=text,
                                 payload={"text": text}, reply_to="stdout",
                                 correlation_id=correlation_id or uuid.uuid4().hex)
        with self._lock:
            self.transcript.append(TranscriptEvent("user", text, correlation_id=message.correlation_id))
            # A real terminal already displayed ``❯ text`` while readline
            # was active.  Only the pipe/plain protocol needs us to append it
            # to the transcript.  Callers may still use submit() directly in
            # tests or integrations, hence this is based on the stream mode.
            if not self._is_interactive_tty:
                self._render(self.transcript[-1])
        if self._callback is None:
            raise RuntimeError("La CLI n'est pas démarrée")
        self._callback(message)
        return message

    def loop(self) -> None:
        """Read lines until EOF or ``/exit``. Commands never reach the core."""
        while self._running:
            try:
                self._prompt_active = self._is_interactive_tty
                if self._is_interactive_tty:
                    self._write("❯ ")
                line = self.input.readline()
            except (EOFError, KeyboardInterrupt):
                break
            finally:
                self._prompt_active = False
            if not line:
                break
            line = line.rstrip("\r\n")
            if line.strip() in {"/exit", "/quit"}:
                break
            if line.strip() == "/help":
                self._write("/help  aide    /status  état    /exit  quitter\n")
                continue
            if line.strip() == "/status":
                self._write(self._json(self._provider("status")) + "\n")
                continue
            if line.strip():
                self.submit(line)
        self.stop()

    @property
    def _is_interactive_tty(self) -> bool:
        """Whether input is echoed by a terminal (and not a test pipe)."""
        try:
            return bool(self.input.isatty() and self.output.isatty())
        except (AttributeError, OSError):
            return False

    def _provider(self, name: str) -> Any:
        try:
            return self.providers[name]()
        except Exception as exc:
            return {"error": str(exc)}

    def _banner(self) -> str:
        status = self._provider("status")
        model = status.get("model", "Orion") if isinstance(status, dict) else "Orion"
        ready = status.get("state", "ready") if isinstance(status, dict) else "ready"
        title = f" Orion · {model} · {ready} "
        width = self.width or shutil.get_terminal_size((80, 24)).columns
        inner = max(20, min(width - 2, 72))
        title = title[:inner]
        return f"╭{'─' * inner}╮\n│{title:<{inner}}│\n╰{'─' * inner}╯\n"

    def _render(self, event: TranscriptEvent) -> None:
        # Background replies must not be glued to the user's next prompt.
        # Move to a fresh line, render atomically, then restore the prompt.
        redraw_prompt = self._prompt_active and self._is_interactive_tty
        if redraw_prompt:
            self._write("\r\x1b[2K\n")
        if self.mode == "jsonl":
            self._write(self._json(event.__dict__) + "\n")
        elif self.mode == "json":
            self._write(self._json(event.__dict__) + "\n")
        elif event.kind == "user":
            self._write(f"❯ {event.text}\n")
        elif event.kind == "assistant":
            self._write(f"● Orion\n{event.text}\n")
        elif event.kind == "stream":
            self._write(f"… {event.text}\n")
        else:
            self._write(f"× {event.text}\n")
        if redraw_prompt and self._running:
            self._write("❯ ")

    def _json(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, default=str)

    def _write(self, text: str) -> None:
        self.output.write(text)
        self.output.flush()


CLIAdapterV2 = OrionCLIAdapter


def run(application: Any, *, adapter: OrionCLIAdapter | None = None) -> int:
    """Attach the adapter to an Orion application and run its input loop."""
    cli = adapter or OrionCLIAdapter()
    application.channels.register(cli)
    application.start()
    try:
        cli.loop()
    finally:
        application.stop()
    return 0


__all__ = ["CLIAdapterV2", "OrionCLIAdapter", "TranscriptEvent", "run"]
