"""Conversation-first terminal cockpit for Orion.

The adapter deliberately owns all terminal output.  Core/runtime code only sees
normalised ``InboundMessage`` objects through ``start``'s callback.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from contextlib import contextmanager
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable, TextIO

from channels import AgentOutput, InboundMessage


@contextmanager
def _suppress_native_stderr():
    """Keep native child-process diagnostics out of the full-screen TUI.

    Chromium and a few other native helpers write directly to inherited file
    descriptor 2, bypassing ``sys.stderr`` and prompt_toolkit.  While the
    alternate screen is active those bytes corrupt cursor positioning and can
    splice unrelated transcript lines together.  Runtime errors are already
    surfaced through ``report_error``; suppress only the native stderr handle
    for the duration of the TUI and restore it immediately afterwards.
    """
    saved_fd: int | None = None
    null_fd: int | None = None
    stderr_fd: int | None = None
    try:
        stderr_fd = int(sys.stderr.fileno())
        try:
            sys.stderr.flush()
        except Exception:
            pass
        saved_fd = os.dup(stderr_fd)
        null_fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(null_fd, stderr_fd)
    except (AttributeError, OSError, TypeError, ValueError):
        if saved_fd is not None:
            try:
                os.close(saved_fd)
            except OSError:
                pass
        saved_fd = None
        stderr_fd = None
    try:
        yield
    finally:
        if saved_fd is not None and stderr_fd is not None:
            try:
                os.dup2(saved_fd, stderr_fd)
            except OSError:
                pass
            try:
                os.close(saved_fd)
            except OSError:
                pass
        if null_fd is not None:
            try:
                os.close(null_fd)
            except OSError:
                pass

try:
    from prompt_toolkit.application import Application
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.document import Document
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.input import Input as PromptToolkitInput
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.layout import HSplit, Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.mouse_events import MouseEventType
    from prompt_toolkit.styles import Style
    from prompt_toolkit.output import Output as PromptToolkitOutput
    from prompt_toolkit.widgets import TextArea
except ImportError:  # pragma: no cover
    Application = None
    PromptToolkitInput = None
    PromptToolkitOutput = None


@dataclass(frozen=True)
class TranscriptEvent:
    kind: str
    text: str
    correlation_id: str | None = None
    speaker: str | None = None


class CockpitCLIAdapter:
    name = "cli"

    def __init__(
        self,
        backend: Any,
        *,
        input: TextIO | None = None,
        output: TextIO | None = None,
        prompt: str = "orion > ",
    ) -> None:
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
        self._view_mode = "chat"
        self._follow_tail = True
        self._visible_transcript_start = 0
        self._last_snapshot: dict[str, Any] = {}
        self._pending_approvals_count: int | None = None

    def _transcript_text(self) -> str:
        with self._write_lock:
            events = tuple(self.transcript_events[self._visible_transcript_start :])
        blocks: list[str] = []
        for event in events:
            if event.kind == "notification":
                blocks.append(f"• {event.text}")
                continue
            if event.kind == "user":
                heading = "YOU"
                if event.correlation_id:
                    heading += f"  ·  {event.correlation_id}"
            elif event.speaker:
                heading = event.speaker.upper()
            else:
                heading = "ORION"
            blocks.append(f"{heading}\n{event.text}")
        return "\n\n".join(blocks)

    def _replace_view_text(self, text: str, *, cursor_position: int | None = None) -> None:
        """Replace TUI content without accidentally resetting its viewport.

        ``TextArea.text = ...`` always recreates the document with the cursor at
        position zero. For a transcript this makes asynchronous output fight the
        operator's scroll position. Keep the cursor where it was while browsing,
        and move it to the end only while tail-follow is enabled.
        """
        if self._view is None:
            return
        if cursor_position is None:
            cursor_position = (
                len(text)
                if self._follow_tail
                else min(self._view.buffer.cursor_position, len(text))
            )
        self._view.document = Document(
            text,
            cursor_position=max(0, min(cursor_position, len(text))),
        )

    def _set_view(
        self,
        text: str,
        *,
        mode: str,
        follow_tail: bool = False,
        cursor_position: int | None = None,
    ) -> None:
        self._view_mode = mode
        self._follow_tail = follow_tail
        if self._view is not None:
            if cursor_position is None:
                cursor_position = len(text) if follow_tail else 0
            self._replace_view_text(text, cursor_position=cursor_position)
            if self._app is not None:
                self._app.invalidate()

    def _scroll_view(self, direction: str) -> None:
        """Scroll the transcript/view while leaving keyboard focus in composer."""
        if self._view is None:
            return
        buffer = self._view.buffer
        text = buffer.text
        if direction == "home":
            buffer.cursor_position = 0
            self._view.window.vertical_scroll = 0
            self._view.window.vertical_scroll_2 = 0
            if self._view_mode == "chat":
                self._follow_tail = False
        elif direction == "end":
            buffer.cursor_position = len(text)
            if self._view_mode == "chat":
                self._follow_tail = True
        elif direction in {"page_up", "page_down"}:
            info = self._view.window.render_info
            if info is not None and info.visible_line_to_row_col:
                if direction == "page_up":
                    # Mirror prompt-toolkit's page-up semantics, but operate on
                    # the transcript buffer while focus remains in the composer.
                    line_index = max(
                        0,
                        min(
                            info.first_visible_line(),
                            buffer.document.cursor_position_row - 1,
                        ),
                    )
                    buffer.cursor_position = buffer.document.translate_row_col_to_index(
                        line_index, 0
                    )
                    self._view.window.vertical_scroll = 0
                    self._view.window.vertical_scroll_2 = 0
                    if self._view_mode == "chat":
                        self._follow_tail = False
                elif info.bottom_visible or info.last_visible_line() >= buffer.document.line_count - 1:
                    buffer.cursor_position = len(text)
                    if self._view_mode == "chat":
                        self._follow_tail = True
                else:
                    # Move by the rendered page, not by one character.  The
                    # previous implementation mixed screen-row coordinates and
                    # buffer offsets, so PgDn often advanced a single character.
                    line_index = min(
                        buffer.document.line_count - 1,
                        max(
                            info.last_visible_line(),
                            self._view.window.vertical_scroll + 1,
                        ),
                    )
                    self._view.window.vertical_scroll = line_index
                    self._view.window.vertical_scroll_2 = 0
                    buffer.cursor_position = buffer.document.translate_row_col_to_index(
                        line_index, 0
                    )
                    if self._view_mode == "chat":
                        self._follow_tail = False
            else:
                count = 10
                if direction == "page_up":
                    buffer.cursor_up(count=count)
                    if self._view_mode == "chat":
                        self._follow_tail = False
                else:
                    buffer.cursor_down(count=count)
                    if buffer.document.cursor_position_row >= buffer.document.line_count - 1:
                        buffer.cursor_position = len(text)
                        if self._view_mode == "chat":
                            self._follow_tail = True
                    elif self._view_mode == "chat":
                        self._follow_tail = False
        elif direction == "line_up":
            buffer.cursor_up()
            if self._view_mode == "chat":
                self._follow_tail = False
        elif direction == "line_down":
            buffer.cursor_down()
            if buffer.document.cursor_position_row >= buffer.document.line_count - 1:
                buffer.cursor_position = len(text)
                if self._view_mode == "chat":
                    self._follow_tail = True
            elif self._view_mode == "chat":
                self._follow_tail = False
        if self._app is not None:
            self._app.invalidate()

    def _command_names(self) -> tuple[str, ...]:
        names: list[str] = []
        provider = getattr(self.backend, "commands", None)
        if callable(provider):
            try:
                values = provider()
            except Exception:
                values = ()
            for value in values or ():
                name = str(value).strip().lstrip("/").lower()
                if name and name not in names:
                    names.append(name)
        return tuple(names)

    @staticmethod
    def _local_help_rows() -> tuple[dict[str, Any], ...]:
        return (
            {
                "command": "clear",
                "usage": "/clear",
                "description": "Clear the visible transcript without deleting conversation history.",
                "available": True,
            },
            {
                "command": "exit",
                "usage": "/exit",
                "description": "Close the cockpit cleanly.",
                "available": True,
            },
            {
                "command": "quit",
                "usage": "/quit",
                "description": "Alias of /exit.",
                "available": True,
            },
        )

    def _structured_help(self, target: str | None = None) -> dict[str, Any] | None:
        """Read the backend's public help contract without guessing capabilities."""
        # ``commands`` is the feature probe for the structured cockpit backend.
        # Legacy/dummy backends may implement execute() for unrelated commands;
        # do not send them a synthetic /help call.
        if not callable(getattr(self.backend, "commands", None)):
            return None
        execute = getattr(self.backend, "execute", None)
        if not callable(execute):
            return None
        request = "/help" if not target else f"/help {target.lstrip('/')}"
        try:
            result = execute(request)
        except Exception:
            return None
        if not isinstance(result, Mapping):
            return None
        data = result.get("data")
        if target is None:
            if isinstance(data, Mapping) and isinstance(data.get("commands"), list):
                return dict(result)
            return None
        if isinstance(data, Mapping) and all(
            key in data for key in ("usage", "description", "available")
        ):
            return dict(result)
        if result.get("error"):
            return dict(result)
        return None

    def _help_text(self, target: str | None = None) -> str:
        target = target.strip().lstrip("/").lower() if target else None
        local_rows = {row["command"]: row for row in self._local_help_rows()}
        if target in local_rows:
            row = local_rows[target]
            return "\n".join(
                (
                    f"HELP  {row['usage']}",
                    "",
                    f"Status: {'available' if row['available'] else 'unavailable'}",
                    str(row["description"]),
                )
            )

        structured = self._structured_help(target)
        if target is not None:
            if structured is not None:
                if structured.get("error"):
                    return f"HELP  /{target}\n\n{structured['error']}"
                data = structured.get("data")
                if isinstance(data, Mapping):
                    status = "available" if bool(data.get("available")) else "unavailable"
                    return "\n".join(
                        (
                            f"HELP  {data.get('usage') or '/' + target}",
                            "",
                            f"Status: {status}",
                            str(data.get("description") or "No description."),
                        )
                    )
            # Minimal legacy fallback: advertise only names the backend itself
            # reports rather than inventing arguments or capability state.
            if target in self._command_names():
                return f"HELP  /{target}\n\nStatus: available\nBackend command."
            return f"HELP  /{target}\n\nUnknown command: /{target}"

        command_rows: list[dict[str, Any]] = []
        note = None
        if structured is not None:
            data = structured.get("data")
            if isinstance(data, Mapping):
                for item in data.get("commands", []):
                    if not isinstance(item, Mapping):
                        continue
                    command = str(item.get("command") or "").strip().lstrip("/")
                    usage = str(item.get("usage") or (f"/{command}" if command else ""))
                    if not command or not usage:
                        continue
                    command_rows.append(
                        {
                            "command": command,
                            "usage": usage,
                            "description": str(item.get("description") or ""),
                            "available": bool(item.get("available")),
                        }
                    )
                note = data.get("note")
        if not command_rows:
            # Keep useful help for old test doubles/integrations that expose
            # only the original command-name registry.
            fallback = (
                "help",
                "commands",
                "status",
                "dashboard",
                "watch",
                *self._command_names(),
            )
            for name in dict.fromkeys(fallback):
                command_rows.append(
                    {
                        "command": name,
                        "usage": f"/{name}",
                        "description": "Cockpit command.",
                        "available": True,
                    }
                )

        existing = {row["command"] for row in command_rows}
        command_rows.extend(row for row in self._local_help_rows() if row["command"] not in existing)
        width = min(34, max((len(str(row["usage"])) for row in command_rows), default=8))
        rows = [
            "ORION COMMAND PALETTE",
            "",
            "Type naturally and press Enter to talk to Orion. Ctrl+J inserts a newline.",
            "Use /help <command> for details. Slash commands never become conversation input.",
            "",
            "Commands",
        ]
        for row in command_rows:
            usage = str(row["usage"])
            status = "available" if row["available"] else "unavailable"
            rows.append(f"  {usage.ljust(width)}  [{status}]  {row['description']}")
        if note:
            rows.extend(("", str(note)))
        return "\n".join(rows)

    @staticmethod
    def _compact(value: Any, limit: int = 36) -> str:
        text = " ".join(str(value).split())
        if len(text) <= limit:
            return text
        return text[: max(1, limit - 1)] + "…"

    @staticmethod
    def _runtime_label(snapshot: Mapping[str, Any]) -> str | None:
        value = snapshot.get("runtime")
        if isinstance(value, Mapping):
            state = value.get("state")
            if state is not None:
                return str(state).upper()
            running = value.get("running")
            if running is not None:
                return "ONLINE" if bool(running) else "STOPPED"
        if value is not None:
            return str(value).upper()
        return None

    @staticmethod
    def _model_label(snapshot: Mapping[str, Any]) -> str | None:
        value = snapshot.get("model")
        if isinstance(value, Mapping):
            for key in ("model", "name", "id"):
                if value.get(key):
                    return str(value[key])
            return None
        return str(value) if value not in (None, "") else None

    @staticmethod
    def _event_label(snapshot: Mapping[str, Any]) -> str | None:
        value = snapshot.get("events")
        if not isinstance(value, Mapping):
            return None
        durable = value.get("durable_enabled", value.get("durable"))
        queued = value.get("queued")
        parts: list[str] = []
        if durable is not None:
            parts.append("durable" if bool(durable) else "memory")
        if isinstance(queued, int):
            parts.append(f"q:{queued}")
        return " ".join(parts) or None

    @staticmethod
    def _cost_label(snapshot: Mapping[str, Any]) -> str | None:
        value = snapshot.get("cost")
        if value is None:
            value = snapshot.get("usage")
        if isinstance(value, Mapping):
            for key in ("known_cost_usd", "total_cost_usd", "cost_usd"):
                if value.get(key) is not None:
                    return f"${value[key]}"
            if value.get("total") is not None:
                currency = str(value.get("currency") or "").upper()
                prefix = "$" if currency in {"", "USD"} else f"{currency} "
                return f"{prefix}{value['total']}"
            return None
        return str(value) if isinstance(value, (int, float)) else None

    @staticmethod
    def _approval_count_from_snapshot(snapshot: Mapping[str, Any]) -> int | None:
        value = snapshot.get("approvals")
        if isinstance(value, list):
            return len(value)
        if isinstance(value, Mapping):
            pending = value.get("pending")
            if isinstance(pending, list):
                return len(pending)
            for key in ("pending_count", "count"):
                if isinstance(value.get(key), int):
                    return int(value[key])
        return None

    def _refresh_overview(self) -> dict[str, Any]:
        """Refresh safe header/dashboard data without doing work during redraws."""
        try:
            snapshot = self._snapshot()
        except Exception:
            snapshot = self._last_snapshot
        count = self._approval_count_from_snapshot(snapshot)
        if count is None and "approve" in self._command_names():
            execute = getattr(self.backend, "execute", None)
            if callable(execute):
                try:
                    result = execute("/approve")
                    data = result.get("data") if isinstance(result, Mapping) else None
                    if isinstance(data, list):
                        count = len(data)
                except Exception:
                    pass
        self._pending_approvals_count = count
        return snapshot

    def _header_fragments(self):
        snapshot = self._last_snapshot
        state = self._runtime_label(snapshot) or ("ONLINE" if self._running else "READY")
        mode = self._view_mode.upper()
        with self._write_lock:
            count = len(self.transcript_events)
        details: list[tuple[str, str]] = [
            ("class:header.mode", mode),
            ("class:header.state", self._compact(state, 16)),
            ("class:header.meta", f"{count} event{'s' if count != 1 else ''}"),
        ]
        model = self._model_label(snapshot)
        events = self._event_label(snapshot)
        cost = self._cost_label(snapshot)
        if self._pending_approvals_count is not None:
            details.append(("class:header.meta", f"approvals:{self._pending_approvals_count}"))
        if events:
            details.append(("class:header.meta", f"events:{self._compact(events, 18)}"))
        if model:
            details.append(("class:header.meta", self._compact(model, 28)))
        if cost:
            details.append(("class:header.meta", self._compact(cost, 16)))
        fragments: list[tuple[str, str]] = [("class:header.brand", " ORION ")]
        used = len(" ORION ")
        width = self._terminal_columns()
        for style, text in details:
            separator = " | "
            if used + len(separator) + len(text) > width:
                continue
            fragments.append(("class:header.sep", separator))
            fragments.append((style, text))
            used += len(separator) + len(text)
        return FormattedText(fragments)

    def _footer_fragments(self):
        groups = (
            (("class:footer.key", " PgUp/Dn "), ("class:footer", "scroll")),
            (("class:footer.key", " Enter "), ("class:footer", "send")),
            (("class:footer.key", " End "), ("class:footer", "tail")),
            (("class:footer.key", " /help "), ("class:footer", "help")),
        )
        width = self._terminal_columns()
        fragments: list[tuple[str, str]] = []
        used = 0
        for group in groups:
            group_width = sum(len(text) for _, text in group)
            spacer = 2 if fragments else 0
            if used + spacer + group_width > width:
                continue
            if spacer:
                fragments.append(("class:footer", "  "))
                used += spacer
            fragments.extend(group)
            used += group_width
        return FormattedText(fragments)

    def _terminal_columns(self, default: int = 80) -> int:
        output = getattr(self._app, "output", None) or self.output
        get_size = getattr(output, "get_size", None)
        if callable(get_size):
            try:
                return max(1, int(get_size().columns))
            except Exception:
                pass
        return default

    def _terminal_rows(self, default: int = 24) -> int:
        output = getattr(self._app, "output", None) or self.output
        get_size = getattr(output, "get_size", None)
        if callable(get_size):
            try:
                return max(1, int(get_size().rows))
            except Exception:
                pass
        return default

    def _append_transcript(
        self,
        kind: str,
        text: str,
        correlation_id=None,
        *,
        speaker: str | None = None,
    ) -> None:
        with self._write_lock:
            self.transcript.append(str(text))
            self.transcript_events.append(
                TranscriptEvent(kind, str(text), correlation_id, speaker)
            )
        self._refresh_chat_view_threadsafe()

    def _refresh_chat_view(self) -> None:
        """Refresh prompt_toolkit-owned widgets on its event-loop thread."""
        with self._write_lock:
            if self._view is None or self._view_mode != "chat":
                return
            text = self._transcript_text()
        self._replace_view_text(text)
        if self._app is not None:
            self._app.invalidate()

    def _refresh_chat_view_threadsafe(self) -> None:
        """Schedule a redraw safely when outputs arrive from worker threads.

        ChannelRouter and EventHandler callbacks are allowed to invoke ``send``
        away from the foreground prompt_toolkit loop.  Mutating a Buffer while
        the renderer is drawing it can corrupt individual terminal cells even
        when Python-side list writes are protected by a lock.  Keep transcript
        storage synchronous, but marshal widget mutation onto PTK's loop.
        """
        app = self._app
        loop = getattr(app, "loop", None) if app is not None else None
        if app is not None and bool(getattr(app, "is_running", False)) and loop is not None:
            call_soon = getattr(loop, "call_soon_threadsafe", None)
            if callable(call_soon):
                try:
                    call_soon(self._refresh_chat_view)
                    return
                except RuntimeError:
                    # The UI loop may be shutting down between the checks.
                    pass
        self._refresh_chat_view()

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
            try:
                self._app.exit()
            except Exception:
                pass

    def set_exit_handler(self, handler: Callable[[], Any]) -> None:
        self._exit_handler = handler

    def report_error(self, event: object, error: Exception) -> None:
        """Surface runtime errors without writing behind prompt_toolkit.

        Direct stderr writes while a full-screen Application owns the terminal
        race with its cursor movement/redraw sequences and can visibly splice
        characters from unrelated messages.  Keep the diagnostic bounded and
        route it through the same transcript renderer instead.
        """
        event_type = str(getattr(event, "type", "unknown"))
        event_id = str(getattr(event, "id", "unknown"))
        error_name = type(error).__name__
        text = f"Erreur interne · {event_type} · {error_name} · {event_id}"
        if self._view is not None:
            self._append_transcript("system", text, speaker="SYSTEM")
            return
        self._write(f"[orion:error] {text}\n")

    def _write(self, text: str) -> None:
        with self._write_lock:
            try:
                self.output.write(text)
            except UnicodeEncodeError:
                # Some Windows/redirected streams still expose a strict legacy
                # codec (commonly cp1252).  Terminal chrome must never make the
                # CLI crash; preserve all representable content and replace only
                # glyphs the active stream cannot encode.
                encoding = getattr(self.output, "encoding", None) or "ascii"
                try:
                    safe = text.encode(encoding, errors="replace").decode(
                        encoding, errors="replace"
                    )
                except LookupError:
                    safe = text.encode("ascii", errors="replace").decode("ascii")
                self.output.write(safe)
            self.output.flush()

    def _stream_marker(self, preferred: str, fallback: str) -> str:
        """Use rich chrome when the stream codec supports it, ASCII otherwise."""
        encoding = getattr(self.output, "encoding", None)
        if not encoding:
            return preferred
        try:
            preferred.encode(encoding)
        except (LookupError, UnicodeEncodeError):
            return fallback
        return preferred

    @staticmethod
    def _worker_speaker(output: AgentOutput) -> str | None:
        metadata = getattr(output, "metadata", {})
        if not isinstance(metadata, Mapping) or metadata.get("output_origin") != "subagent":
            return None
        raw_name = (
            metadata.get("sender_name")
            or metadata.get("agent_name")
            or metadata.get("subagent_id")
            or metadata.get("agent_id")
            or "subagent"
        )
        name = " ".join(str(raw_name).split())[:48] or "subagent"
        return f"Worker · {name}"

    @classmethod
    def _worker_notification(cls, output: AgentOutput) -> str | None:
        """Return the compact CLI notification for a worker result Orion will synthesize.

        Conversational delegations emit the raw worker result as an intermediate
        runtime output so Orion can immediately wake and continue.  That artifact
        is useful operationally but should not look like a second chat message in
        the cockpit.  Standalone worker outputs remain normal messages because no
        Orion synthesis is guaranteed to follow them.
        """
        metadata = getattr(output, "metadata", {})
        if not isinstance(metadata, Mapping):
            return None
        if not bool(metadata.get("intermediate", False)):
            return None
        if metadata.get("phase") != "subagent_result":
            return None
        worker_speaker = cls._worker_speaker(output)
        if worker_speaker is None:
            return None
        return f"{worker_speaker} · résultat reçu"

    @classmethod
    def _intermediate_notification(cls, output: AgentOutput) -> str | None:
        """Compact non-final runtime chatter into one-line cockpit notices."""
        metadata = getattr(output, "metadata", {})
        if not isinstance(metadata, Mapping) or not bool(metadata.get("intermediate", False)):
            return None
        worker = cls._worker_notification(output)
        if worker is not None:
            return worker
        phase = str(metadata.get("phase") or "")
        text = " ".join(str(getattr(output, "text", None) or output.content).split())
        if not text:
            return "Orion · traitement en cours"
        # Assistant content emitted alongside tool calls is Orion's native
        # progress update. Keep the actual phrase visible, but bound accidental
        # verbose preambles so they cannot become a second full answer.
        limit = 140 if phase == "tool_preamble" else 180
        if len(text) > limit:
            text = text[: limit - 1].rstrip() + "…"
        return f"Orion · {text}"

    def _is_duplicate_transcript_event(
        self,
        kind: str,
        text: str,
        correlation_id: str | None,
        *,
        speaker: str | None = None,
    ) -> bool:
        """Suppress only strict consecutive replays of the same logical slot."""
        with self._write_lock:
            if not self.transcript_events:
                return False
            last = self.transcript_events[-1]
            return (
                last.kind == kind
                and last.text == text
                and last.correlation_id == correlation_id
                and last.speaker == speaker
            )

    def send(self, output: AgentOutput) -> AgentOutput:
        """Render an output atomically; safe to call from worker threads."""
        text = getattr(output, "text", None) or str(output)
        worker_speaker = self._worker_speaker(output)
        correlation_id = getattr(output, "correlation_id", None)
        notification = self._intermediate_notification(output)
        if notification:
            if self._is_duplicate_transcript_event(
                "notification", notification, correlation_id
            ):
                return output
            self._append_transcript(
                "notification",
                notification,
                correlation_id,
            )
            if self._view is not None:
                return output
            marker = self._stream_marker("●", "*")
            self._write(f"{marker} {notification}\n")
            return output
        event_kind = "worker" if worker_speaker else "assistant"
        if self._is_duplicate_transcript_event(
            event_kind,
            text,
            correlation_id,
            speaker=worker_speaker,
        ):
            return output
        self._append_transcript(
            event_kind,
            text,
            correlation_id,
            speaker=worker_speaker,
        )
        if self._view is not None:
            # prompt-toolkit owns the terminal while the full-screen cockpit is
            # active.  Writing to stdout here corrupts/redraws the screen; the
            # transcript update above is the sole TUI rendering path.
            return output
        if worker_speaker:
            marker = self._stream_marker("●", "*")
            self._write(f"{marker} {worker_speaker}\n{text}\n")
        else:
            self._write(f"{self._stream_marker('● Orion', '* Orion')}\n{text}\n")
        return output

    def submit(self, text: str) -> InboundMessage:
        text = text.strip()
        self._correlation += 1
        message = InboundMessage(
            text=text,
            channel=self.name,
            source=self.name,
            correlation_id=f"cli-{self._correlation}",
            payload={"text": text},
        )
        self._append_transcript("user", text, message.correlation_id)
        if self._on_message is not None:
            self._on_message(message)
        return message

    def _render_result(self, result: Any) -> None:
        if self._view is not None:
            if isinstance(result, dict) and result.get("error"):
                rendered = "ERROR\n" + str(result["error"])
            elif result is not None:
                title = result.get("title") if isinstance(result, dict) else None
                data = (
                    result.get("data", result) if isinstance(result, dict) else result
                )
                body = (
                    json.dumps(data, ensure_ascii=False, indent=2, default=str)
                    if isinstance(data, (dict, list))
                    else str(data)
                )
                rendered = f"{str(title).upper()}\n\n{body}" if title else body
            else:
                rendered = ""
            self._set_view(rendered, mode="command")
            return
        if isinstance(result, dict):
            if result.get("error"):
                marker = self._stream_marker("✗", "ERROR:")
                self._write(f"{marker} {result['error']}\n")
                return
            title = result.get("title")
            data = result.get("data")
            if title:
                self._write(f"{title}\n")
            if data is not None:
                self._write(
                    (
                        json.dumps(data, ensure_ascii=False, indent=2, default=str)
                        if isinstance(data, (dict, list))
                        else f"{data}"
                    )
                    + "\n"
                )
        elif result is not None:
            self._write(f"{result}\n")

    def _snapshot(self) -> dict[str, Any]:
        value = self.backend.snapshot()
        snapshot = value if isinstance(value, dict) else {"data": value}
        self._last_snapshot = snapshot
        return snapshot

    def _dashboard_text(self, snapshot: dict[str, Any] | None = None) -> str:
        s = snapshot if snapshot is not None else self._snapshot()

        def human(value: Any, indent: int = 2) -> list[str]:
            pad = " " * indent
            if isinstance(value, dict):
                result: list[str] = []
                for key, item in value.items():
                    if isinstance(item, (dict, list)):
                        result.append(f"{pad}{key}:")
                        result.extend(human(item, indent + 2))
                    else:
                        result.append(f"{pad}{key}: {item}")
                return result or [f"{pad}(empty)"]
            if isinstance(value, list):
                result = []
                for item in value:
                    if isinstance(item, dict):
                        entries = list(item.items())
                        if not entries:
                            result.append(f"{pad}- {{}}")
                            continue
                        first_key, first_value = entries[0]
                        if isinstance(first_value, (dict, list)):
                            result.append(f"{pad}- {first_key}:")
                            result.extend(human(first_value, indent + 4))
                        else:
                            result.append(f"{pad}- {first_key}: {first_value}")
                        for key, nested in entries[1:]:
                            if isinstance(nested, (dict, list)):
                                result.append(f"{pad}  {key}:")
                                result.extend(human(nested, indent + 4))
                            else:
                                result.append(f"{pad}  {key}: {nested}")
                    else:
                        result.append(f"{pad}- {item}")
                return result or [f"{pad}(none)"]
            return [f"{pad}{value}"]

        def lines(name: str) -> list[str]:
            value = s.get(name)
            if value is None:
                return [f"{name.upper()}: unavailable"]
            if isinstance(value, (dict, list)):
                return [f"{name.upper()}:"] + human(value)
            return [f"{name.upper()}: {value}"]

        overview = ["ORION"]
        model = self._model_label(s)
        events = self._event_label(s)
        cost = self._cost_label(s)
        if model:
            overview.append(f"MODEL: {model}")
        if events:
            overview.append(f"EVENTS: {events}")
        if self._pending_approvals_count is not None:
            overview.append(f"APPROVALS: {self._pending_approvals_count} pending")
        if cost:
            overview.append(f"COST: {cost}")
        return "\n".join(
            overview
            + lines("runtime")
            + lines("tasks")
            + lines("agents")
            + lines("workspace")
        )

    def _clear_presentation(self) -> None:
        """Hide prior transcript content without mutating conversation history."""
        with self._write_lock:
            self._visible_transcript_start = len(self.transcript_events)
        if self._view is not None:
            self._set_view("", mode="chat", follow_tail=True)
        else:
            # A pipe/StringIO cannot erase bytes already written. Keep this path
            # ANSI-free and explicit rather than emitting terminal-specific CLS.
            self._write("[cockpit] display cleared\n")

    def build_application(self):
        """Build the sole screen owner used for interactive terminals."""
        if Application is None:
            raise RuntimeError("prompt-toolkit unavailable")
        self._refresh_overview()
        transcript = TextArea(
            text=self._transcript_text(),
            read_only=True,
            scrollbar=False,
            focusable=False,
            wrap_lines=True,
        )
        original_transcript_mouse_handler = transcript.control.mouse_handler

        def transcript_mouse_handler(mouse_event):
            """Scroll the conversation without stealing focus from the composer."""
            if mouse_event.event_type == MouseEventType.SCROLL_UP:
                for _ in range(3):
                    self._scroll_view("line_up")
                return None
            if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
                for _ in range(3):
                    self._scroll_view("line_down")
                return None
            return original_transcript_mouse_handler(mouse_event)

        transcript.control.mouse_handler = transcript_mouse_handler
        self._view = transcript
        self._replace_view_text(transcript.text, cursor_position=len(transcript.text))
        command_words = [
            f"/{name}"
            for name in (
                "help",
                "commands",
                "status",
                "dashboard",
                "watch",
                "clear",
                "exit",
                "quit",
                *self._command_names(),
            )
        ]
        # Preserve order while removing aliases duplicated by the backend.
        command_words = list(dict.fromkeys(command_words))
        editor = TextArea(
            height=Dimension(min=1, preferred=3, max=3),
            prompt=self.prompt,
            multiline=True,
            wrap_lines=True,
            completer=WordCompleter(command_words, ignore_case=True, sentence=True),
            complete_while_typing=False,
        )
        bindings = KeyBindings()

        @bindings.add("enter")
        def _submit(event):
            value = editor.text.strip()
            editor.text = ""
            if value:
                if value.startswith("/"):
                    if not self._command(value):
                        event.app.exit()
                else:
                    if self._view_mode != "chat":
                        chat = self._transcript_text()
                        self._set_view(chat, mode="chat", follow_tail=True)
                    self.submit(value)
                # Natural language is delivered to the runtime callback; only
                # explicit slash commands are handled by the cockpit backend.
            event.app.invalidate()

        @bindings.add(Keys.ControlJ, eager=True)
        def _insert_newline(event):
            # Keep this portable on Windows terminals where Alt+Enter is not a
            # stable key sequence, and respect the current caret position.
            editor.buffer.insert_text("\n")

        @bindings.add("f1")
        def _help(event):
            self._set_view(self._help_text(), mode="help")

        @bindings.add(Keys.PageUp, eager=True)
        def _page_up(event):
            self._scroll_view("page_up")

        @bindings.add(Keys.PageDown, eager=True)
        def _page_down(event):
            self._scroll_view("page_down")

        @bindings.add(Keys.Home, eager=True)
        def _home(event):
            self._scroll_view("home")

        @bindings.add(Keys.End, eager=True)
        def _end(event):
            self._scroll_view("end")

        @bindings.add(Keys.ControlUp, eager=True)
        def _line_up(event):
            self._scroll_view("line_up")

        @bindings.add(Keys.ControlDown, eager=True)
        def _line_down(event):
            self._scroll_view("line_down")

        @bindings.add("c-c")
        def _stop(event):
            self.stop()

        @bindings.add("c-d")
        def _eof(event):
            self.stop()

        header = Window(
            FormattedTextControl(text=self._header_fragments),
            height=1,
            dont_extend_height=True,
        )
        footer = Window(
            FormattedTextControl(text=self._footer_fragments),
            height=1,
            dont_extend_height=True,
            wrap_lines=False,
        )
        conversation_label = Window(
            FormattedTextControl(text=[("class:section.label", " Conversation ")]),
            char="-",
            height=lambda: 1 if self._terminal_rows() >= 8 else 0,
            dont_extend_height=True,
            wrap_lines=False,
        )
        message_label = Window(
            FormattedTextControl(text=[("class:section.label", " Message ")]),
            char="-",
            height=lambda: 1 if self._terminal_rows() >= 8 else 0,
            dont_extend_height=True,
            wrap_lines=False,
        )
        style = Style.from_dict(
            {
                "header.brand": "bold reverse",
                "header.sep": "dim",
                "header.state": "bold",
                "header.mode": "bold",
                "header.meta": "dim",
                "footer": "dim",
                "footer.key": "bold",
                "section.label": "bold",
            }
        )
        application_kwargs: dict[str, Any] = {}
        # Respect already-created prompt-toolkit IO objects.  Besides making
        # dependency injection deterministic, this avoids prompt_toolkit
        # probing a second Win32 console handle (which can fail in VS Code,
        # redirected ConPTY sessions, test harnesses, etc.).
        if PromptToolkitInput is not None and isinstance(self.input, PromptToolkitInput):
            application_kwargs["input"] = self.input
        if PromptToolkitOutput is not None and isinstance(self.output, PromptToolkitOutput):
            application_kwargs["output"] = self.output
        self._app = Application(
            layout=Layout(
                HSplit([header, conversation_label, transcript, message_label, editor, footer]),
                focused_element=editor,
            ),
            key_bindings=bindings,
            full_screen=True,
            mouse_support=True,
            style=style,
            **application_kwargs,
        )
        return self._app

    def run_tui(self) -> None:
        app = self.build_application()
        self.start(self._on_message or (lambda _: None))
        try:
            with _suppress_native_stderr():
                try:
                    app.run()
                except (EOFError, KeyboardInterrupt):
                    # Real terminal/pipe hangup can surface as EOFError before
                    # a key binding is dispatched. Treat it like Ctrl+D/C.
                    pass
        finally:
            self.stop()

    def _command(self, line: str) -> bool:
        command, _, arg = line.partition(" ")
        command = command.lower()
        if command in {"/exit", "/quit"}:
            self.stop()
            return False
        if command == "/clear":
            self._clear_presentation()
            return True
        if command in {"/help", "/commands"}:
            target = arg.strip().split(maxsplit=1)[0] if command == "/help" and arg.strip() else None
            help_text = self._help_text(target)
            if self._view is not None:
                self._set_view(help_text, mode="help")
            else:
                self._write(help_text + "\n")
            return True
        if command in {"/status", "/dashboard", "/watch"}:
            snap = self._refresh_overview()
            if self._view is not None:
                dashboard = self._dashboard_text(snap)
                self._set_view(dashboard, mode=command[1:])
            else:
                # Keep machine-friendly JSON for pipes/non-TTY callers.
                self._render_result({"title": command[1:].upper(), "data": snap})
            return True
        if command.startswith("/"):
            self._render_result(self.backend.execute(line))
            if command in {"/approve", "/reject", "/model", "/autonomy", "/workspace"}:
                self._refresh_overview()
            return True
        return True

    def loop(self) -> None:
        if not self._running:
            self.start(lambda _: None)
        if (
            callable(getattr(self.input, "isatty", None))
            and self.input.isatty()
            and callable(getattr(self.output, "isatty", None))
            and self.output.isatty()
            and Application is not None
        ):
            self.run_tui()
            return
        for raw in self.input:
            if self._stop.is_set():
                break
            line = raw.rstrip("\r\n")
            if not line:
                continue
            if line.startswith("/") and not self._command(line):
                break
            if line.startswith("/"):
                continue
            self.submit(line)
        # Iteration ending is EOF (normal for pipes and StringIO), not a live
        # session.  Keep stop idempotent so callers can safely stop afterwards.
        self.stop()


OrionCockpitCLI = CockpitCLIAdapter
CLI = CockpitCLIAdapter

__all__ = ["CLI", "CockpitCLIAdapter", "OrionCockpitCLI", "TranscriptEvent"]
