"""Conversation-first terminal cockpit for Orion.

The adapter deliberately owns all terminal output.  Core/runtime code only sees
normalised ``InboundMessage`` objects through ``start``'s callback.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
from contextlib import contextmanager
from collections.abc import Mapping, Sequence
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
    from prompt_toolkit.formatted_text import FormattedText, StyleAndTextTuples
    from prompt_toolkit.input import Input as PromptToolkitInput
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.layout import HSplit, Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.layout.margins import ScrollbarMargin
    from prompt_toolkit.lexers import Lexer
    from prompt_toolkit.mouse_events import MouseEventType
    from prompt_toolkit.styles import Style
    from prompt_toolkit.output import Output as PromptToolkitOutput
    from prompt_toolkit.widgets import TextArea
except ImportError:  # pragma: no cover
    Application = None
    PromptToolkitInput = None
    PromptToolkitOutput = None
    ScrollbarMargin = None
    Lexer = None

try:
    # Already a hard dependency of Rich; importing it directly lets the cockpit
    # render Markdown instead of showing raw ``**markers**``.
    from markdown_it import MarkdownIt
except ImportError:  # pragma: no cover - optional
    MarkdownIt = None


@dataclass(frozen=True)
class TranscriptEvent:
    kind: str
    text: str
    correlation_id: str | None = None
    speaker: str | None = None


class _MarkdownRenderer:
    """Turn Markdown into prompt_toolkit fragments, one list per line.

    The transcript used to display raw Markdown, so ``**bold**`` and
    ``\\`code\\``` reached the operator as literal asterisks and backticks.
    ``markdown-it-py`` parses the source (it already ships as a Rich
    dependency) and this maps the tokens onto the cockpit palette so the
    structure survives without the markers.
    """

    # Block toplevel (rendered with a blank line after it)
    _HEADINGS = {"h1": "class:md.h1", "h2": "class:md.h2", "h3": "class:md.h3",
                 "h4": "class:md.h4", "h5": "class:md.h4", "h6": "class:md.h4"}

    def __init__(self, text: str) -> None:
        self._text = text
        self.lines: list[StyleAndTextTuples] = []
        self._parse()

    # -- parsing ---------------------------------------------------------
    def _parse(self) -> None:
        tokens = self._tokenize(self._text)
        if tokens is None:
            # No parser available: fall back to unstyled lines, which keeps the
            # raw Markdown visible rather than losing the message.
            self.lines = [[("class:transcript.body", line)] for line in self._text.split("\n")]
            return
        for token in tokens:
            self._block(token)
        # Drop trailing blank lines so the document does not end with padding.
        while self.lines and not self.lines[-1]:
            self.lines.pop()
        if not self.lines:
            self.lines = [[]]

    @staticmethod
    def _tokenize(text: str) -> list[Any] | None:
        if MarkdownIt is None:
            return None
        try:
            # "commonmark" omits GFM constructs, and "gfm-like" pulls in the
            # linkify plugin, which is a separate (optional) package -- using it
            # made every token fall back to unstyled text. Start from
            # commonmark and enable only the rules that need no extra install.
            parser = MarkdownIt("commonmark").enable("table").enable("strikethrough")
            return parser.parse(text)
        except Exception:
            try:
                return MarkdownIt("commonmark").parse(text)
            except Exception:
                return None

    def _block(self, token: Any) -> None:
        kind = token.type
        if kind == "inline":
            fragments: StyleAndTextTuples = list(self._inline_fragments(token))
            pending = getattr(self, "_pending_prefix", None)
            self._pending_prefix = None
            if pending is not None:
                fragments = [pending, *fragments]
            if getattr(self, "_table_cell", None) is not None:
                # A table cell's inline content is buffered until the row ends,
                # so the row can be laid out as a single aligned line.
                self._table_cell.append(fragments)
                return
            self.lines.append(fragments)
            return
        if kind == "table_open":
            self._table_rows = []
            return
        if kind == "table_close":
            self._emit_table()
            return
        if kind == "tr_open":
            self._table_cell = []
            return
        if kind == "tr_close":
            cells = getattr(self, "_table_cell", None) or []
            self._table_cell = None
            self._table_rows = getattr(self, "_table_rows", [])
            self._table_rows.append(cells)
            return
        if kind in {"th_open", "td_open"}:
            self._in_header_cell = kind == "th_open"
            return
        if kind in {"th_close", "td_close"}:
            self._in_header_cell = False
            return
        if kind == "heading_open":
            self._heading_style = self._HEADINGS.get(token.tag, "class:md.h1")
            return
        if kind == "heading_close":
            self._heading_style = None
            self._blank()
            return
        if kind in {"bullet_list_open", "ordered_list_open"}:
            self._list_depth = getattr(self, "_list_depth", 0) + 1
            if kind == "ordered_list_open":
                self._ordered_depth = getattr(self, "_ordered_depth", 0) + 1
                self._list_item_index = 1
            return
        if kind in {"bullet_list_close", "ordered_list_close"}:
            self._list_depth = max(0, getattr(self, "_list_depth", 1) - 1)
            if kind == "ordered_list_close":
                self._ordered_depth = max(0, getattr(self, "_ordered_depth", 1) - 1)
            if self._list_depth == 0:
                self._blank()
            return
        if kind == "list_item_open":
            # The counter is owned by the enclosing ordered list so successive
            # items increment; a bullet list ignores it.
            self._pending_prefix = self._bullet_prefix()
            return
        if kind == "list_item_close":
            self._pending_prefix = None
            return
        if kind == "blockquote_open":
            self._quote_depth = getattr(self, "_quote_depth", 0) + 1
            return
        if kind == "blockquote_close":
            self._quote_depth = max(0, getattr(self, "_quote_depth", 1) - 1)
            if self._quote_depth == 0:
                self._blank()
            return
        if kind in {"fence", "code_block"}:
            # ``fence`` is a self-closing token: its body is in ``content``.
            self._render_code_block(token)
            return
        if kind == "hr":
            self.lines.append([("class:md.rule", "-" * 40)])
            self._blank()
            return
        if kind == "paragraph_close":
            self._blank()
            return
        if kind == "paragraph_open":
            # A list item's text arrives as a paragraph; keep any pending
            # bullet prefix so it survives until the inline token.
            return

    def _bullet_prefix(self) -> tuple[str, str]:
        depth = max(1, getattr(self, "_list_depth", 1))
        ordered = getattr(self, "_ordered_depth", 0) > 0
        index = getattr(self, "_list_item_index", 1)
        self._list_item_index = index + 1
        marker = f"{index}. " if ordered else "• "
        return ("class:md.bullet", "  " * (depth - 1) + marker)

    def _render_code_block(self, token: Any) -> None:
        style = "class:md.codeblock"
        content = str(token.content or "").rstrip("\n")
        if content:
            for line in content.split("\n"):
                self.lines.append([(style, line)])
        self._blank()

    def _emit_table(self) -> None:
        """Lay the buffered table out as aligned, separated columns."""
        rows = getattr(self, "_table_rows", [])
        self._table_rows = []
        if not rows:
            return
        rendered: list[list[str]] = [
            ["".join(text for _, text in cell) for cell in row] for row in rows
        ]
        widths: list[int] = []
        for row in rendered:
            for index, cell in enumerate(row):
                while len(widths) <= index:
                    widths.append(0)
                widths[index] = max(widths[index], len(cell))
        for row_index, row in enumerate(rendered):
            style = "class:md.strong" if row_index == 0 else "class:transcript.body"
            cells = [
                cell.ljust(widths[index]) if index < len(widths) - 1 else cell
                for index, cell in enumerate(row)
            ]
            self.lines.append([(style, "  ".join(cells).rstrip())])
            if row_index == 0:
                self.lines.append(
                    [("class:md.rule", "  ".join("-" * w for w in widths).rstrip())]
                )
        self._blank()

    def _blank(self) -> None:
        if self.lines and self.lines[-1] != []:
            self.lines.append([])

    def _inline_fragments(self, token: Any) -> StyleAndTextTuples:
        base = getattr(self, "_heading_style", None) or (
            "class:md.quote"
            if getattr(self, "_quote_depth", 0)
            else "class:transcript.body"
        )
        fragments: StyleAndTextTuples = []
        stack: list[str] = []

        def add(text: str, own: str | None = None) -> None:
            style = own or base
            if stack:
                style = style + " " + " ".join(f"class:{name}" for name in stack)
            fragments.append((style, text))

        for child in token.children or []:
            ctype = child.type
            if ctype == "text":
                add(child.content)
            elif ctype == "strong_open":
                stack.append("md.strong")
            elif ctype == "strong_close":
                if stack:
                    stack.pop()
            elif ctype == "em_open":
                stack.append("md.em")
            elif ctype == "em_close":
                if stack:
                    stack.pop()
            elif ctype == "code_inline":
                add(child.content, "class:md.code")
            elif ctype in {"softbreak", "hardbreak"}:
                add(" ")
            elif ctype == "link_open":
                stack.append("md.link")
            elif ctype == "link_close":
                if stack:
                    stack.pop()
            elif ctype == "image":
                alt = child.attrGet("alt") or child.content or "image"
                add(f"[{alt}]", "class:md.link")
        return fragments


if Lexer is not None:

    class _TranscriptLexer(Lexer):
        """Colour the read-only transcript by role, and render Markdown.

        ``TextArea`` owns the scroll behaviour and the buffer API the rest of
        the cockpit (and its tests) depend on, so rather than replacing the
        widget this lexer stamps style classes per document line.  The adapter
        publishes the class list when it installs new text; chat text is then
        rendered through the Markdown renderer so formatting markers do not
        reach the operator.
        """

        def __init__(self, adapter: "CockpitCLIAdapter") -> None:
            self._adapter = adapter
            self._cache_key: str | None = None
            self._cache: list[StyleAndTextTuples] = []

        def _rendered_lines(self, document: Document) -> list[StyleAndTextTuples]:
            adapter = self._adapter
            text = document.text
            key = f"{adapter._view_mode}\x00{text}"
            if self._cache_key == key:
                return self._cache
            if adapter._view_mode == "chat":
                # Chat is Markdown: parse it so the operator sees formatted
                # prose instead of literal ** and ` markers.  Role colours come
                # from the inline styles the renderer emits.
                lines = _MarkdownRenderer(text).lines
            else:
                # Dashboard / help / command output is machine text, styled from
                # the explicit per-line map the adapter published.
                styles = adapter._line_styles
                lines = []
                for index, raw in enumerate(text.split("\n")):
                    style = (
                        styles[index]
                        if index < len(styles)
                        else "class:transcript.view"
                    )
                    lines.append([(style, raw)])
            self._cache_key = key
            self._cache = lines
            return lines

        def lex_document(self, document: Document):
            lines = self._rendered_lines(document)

            def get_line(line_number: int) -> StyleAndTextTuples:
                if 0 <= line_number < len(lines):
                    return lines[line_number]
                return []

            return get_line

else:  # pragma: no cover - prompt_toolkit unavailable
    _TranscriptLexer = None


class CockpitCLIAdapter:
    name = "cli"

    # Upper bound on the number of transcript events rendered by the TUI.  The
    # full session is always retained in ``transcript_events``; this only caps
    # how much text each redraw has to rebuild and re-wrap.
    _MAX_RENDERED_EVENTS = 400

    def __init__(
        self,
        backend: Any,
        *,
        input: TextIO | None = None,
        output: TextIO | None = None,
        prompt: str = "orion > ",
        refresh_seconds: float = 1.0,
    ) -> None:
        self.backend = backend
        self.input = input or sys.stdin
        self.output = output or sys.stdout
        self.prompt = prompt
        # Cadence for the header/status refresh.  The overview was previously
        # only recomputed when output arrived or a command ran, so the runtime
        # state and session cost stayed frozen during a long silent run.  Zero
        # or negative disables the background refresh.
        self.refresh_seconds = max(0.0, float(refresh_seconds))
        self._refresh_stop = threading.Event()
        self._refresh_thread: threading.Thread | None = None
        self._snapshot_lock = threading.Lock()
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
        # Per-document-line style class for the transcript view.  Read by
        # ``_TranscriptLexer`` during rendering, so it always matches the text
        # currently installed in the view.
        self._line_styles: list[str] = []

    def _rendered_events(self) -> tuple[TranscriptEvent, ...]:
        """The transcript window the TUI renders, newest events last."""
        with self._write_lock:
            events = tuple(self.transcript_events[self._visible_transcript_start :])
        # Bound only what the TUI *renders*.  ``transcript_events`` still holds
        # the complete session (``/commands`` and providers read it), but the
        # view no longer rebuilds a multi-megabyte Document on every streamed
        # chunk, which is what made long sessions progressively slower and the
        # redraw visibly unstable.
        if len(events) > self._MAX_RENDERED_EVENTS:
            events = events[-self._MAX_RENDERED_EVENTS :]
        return events

    def _transcript_blocks(self) -> tuple[str, list[str]]:
        """Build the visible transcript plus a style class for each line.

        Returns ``(text, styles)`` where ``styles[i]`` styles line ``i`` of
        ``text``.  Both are produced in one pass and the class for each line is
        decided by *position* — the first line of a block is its role caption —
        never by matching the caption text, because a message body may legally
        contain a line that reads ``ORION``.
        """
        parts: list[str] = []
        styles: list[str] = []
        for event in self._rendered_events():
            if event.kind == "notification":
                block = f"• {event.text}"
                heading_style = "class:transcript.notice"
                body_style = "class:transcript.notice"
            elif event.kind == "user":
                heading = "YOU"
                if event.correlation_id:
                    heading += f"  ·  {event.correlation_id}"
                block = f"{heading}\n{event.text}"
                heading_style = "class:transcript.user"
                body_style = "class:transcript.user.body"
            elif event.speaker:
                block = f"{event.speaker.upper()}\n{event.text}"
                heading_style = "class:transcript.worker"
                body_style = "class:transcript.body"
            else:
                block = f"ORION\n{event.text}"
                heading_style = "class:transcript.orion"
                body_style = "class:transcript.body"

            lines = block.split("\n")
            for index in range(len(lines)):
                styles.append(heading_style if index == 0 else body_style)
            # The blank line separating two blocks inherits the previous
            # block's body style so the map stays aligned with the text.
            styles.append(body_style)
            parts.append(block)
        text = "\n\n".join(parts)
        # ``"\n\n".join`` consumes one separator per gap; keep the map exact.
        if styles:
            styles = styles[: len(text.split("\n"))]
            while len(styles) < len(text.split("\n")):
                styles.append("class:transcript.body")
        return text, styles

    def _transcript_text(self) -> str:
        return self._transcript_blocks()[0]

    def _replace_view_text(
        self,
        text: str,
        *,
        cursor_position: int | None = None,
        anchor: tuple[int, int] | None = None,
        style_map: Sequence[str] | None = None,
    ) -> None:
        """Replace TUI content without accidentally resetting its viewport.

        ``TextArea.text = ...`` always recreates the document with the cursor at
        position zero. For a transcript this makes asynchronous output fight the
        operator's scroll position. Keep the cursor where it was while browsing,
        and move it to the end only while tail-follow is enabled.

        ``anchor`` is the previous ``(row, column)`` of the cursor. Because the
        viewport is derived from the cursor, restoring the *cell* rather than
        the raw offset is what keeps the text under the reader's eyes while new
        output arrives at the bottom of the transcript.
        """
        if self._view is None:
            return
        if cursor_position is None:
            if self._follow_tail:
                cursor_position = len(text)
            else:
                cursor_position = self._anchor_index(text, anchor)
        # Publish the style map before the document lands so the lexer sees a
        # class for every line of the text it is about to colour.
        if style_map is not None:
            self._line_styles = list(style_map)
        self._view.document = Document(
            text,
            cursor_position=max(0, min(cursor_position, len(text))),
        )

    @staticmethod
    def _anchor_index(text: str, anchor: tuple[int, int] | None) -> int:
        """Map a remembered ``(row, column)`` cell onto the new text.

        Falls back to the document start when there is nothing to restore.
        """
        if anchor is None:
            return 0
        row, column = anchor
        document = Document(text)
        row = max(0, min(row, document.line_count - 1))
        line = document.lines[row]
        return document.translate_row_col_to_index(row, max(0, min(column, len(line))))

    def _set_view(
        self,
        text: str,
        *,
        mode: str,
        follow_tail: bool = False,
        cursor_position: int | None = None,
        style_map: Sequence[str] | None = None,
    ) -> None:
        self._view_mode = mode
        self._follow_tail = follow_tail
        if self._view is not None:
            if cursor_position is None:
                cursor_position = len(text) if follow_tail else 0
            if style_map is None:
                # Non-chat views are machine output (dashboard, help, command
                # result).  Without an explicit map the lexer would keep the
                # *previous* transcript's role colours, which is why command
                # output rendered in the chat body colour.  Chat keeps its
                # per-role map, rebuilt by ``_refresh_chat_view``.
                if mode == "chat":
                    _, style_map = self._transcript_blocks()
                else:
                    style_map = ["class:transcript.view"] * len(text.split("\n"))
            self._replace_view_text(
                text, cursor_position=cursor_position, style_map=style_map
            )
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
                "command": "cancel",
                "usage": "/cancel",
                "description": (
                    "Stop the active run at its next safe point. The model or "
                    "tool call already in flight finishes first."
                ),
                "available": True,
            },
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
        def normalise(value: Any) -> str:
            # An Enum that subclasses str renders as "RuntimeState.EVALUATING";
            # prefer its value, then the bare member name, so the header shows
            # a state word rather than an internal type path.
            raw_member = getattr(value, "value", None)
            if raw_member is not None and not isinstance(raw_member, (str, bytes)):
                value = raw_member
            text = str(value)
            if "." in text and text.split(".")[0].isidentifier():
                head, _, tail = text.rpartition(".")
                if head.isidentifier() and tail:
                    return tail
            return text

        value = snapshot.get("runtime")
        if isinstance(value, Mapping):
            state = value.get("state")
            if state is not None:
                return normalise(state).upper()
            running = value.get("running")
            if running is not None:
                return "ONLINE" if bool(running) else "STOPPED"
        if value is not None:
            return normalise(value).upper()
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

    @classmethod
    def _cost_label(cls, snapshot: Mapping[str, Any]) -> str | None:
        """Richest cost label available, or None when the snapshot has none."""
        variants = cls._cost_variants(snapshot)
        return variants[0] if variants else None

    @staticmethod
    def _cost_variants(snapshot: Mapping[str, Any]) -> list[str]:
        """Candidate cost labels, longest first, so the header can degrade.

        The usage ledger is in-memory and per-process, so the total resets when
        Orion restarts and cannot be compared directly with an account-level
        figure such as the OpenRouter dashboard.  Name the scope rather than
        implying it is cumulative, and surface how many calls are missing a
        provider-reported cost so an incomplete total is visible.  Narrow
        terminals drop the qualifiers rather than dropping the number.
        """
        value = snapshot.get("cost")
        if value is None:
            value = snapshot.get("usage")
        if not isinstance(value, Mapping):
            if isinstance(value, (int, float)):
                return [str(value)]
            return []
        amount: str | None = None
        for key in ("known_cost_usd", "total_cost_usd", "cost_usd"):
            if value.get(key) is not None:
                amount = f"${value[key]}"
                break
        if amount is None and value.get("total") is not None:
            currency = str(value.get("currency") or "").upper()
            prefix = "$" if currency in {"", "USD"} else f"{currency} "
            amount = f"{prefix}{value['total']}"
        if amount is None:
            return []
        missing = value.get("usage_missing_calls")
        scoped = f"{amount} this session"
        if isinstance(missing, int) and missing > 0:
            return [f"{scoped} · {missing} unpriced", scoped, amount]
        return [scoped, amount]

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

    def _start_refresh_loop(self) -> None:
        """Keep the header/status fresh while the TUI runs.

        The overview (runtime state, session cost, approvals, queue depth) was
        only recomputed when an output arrived or a command ran, so during a
        long silent run the header could sit on stale values indefinitely.
        Snapshots are gathered on a background thread because the backend walks
        installed tool manifests on disk, and the redraw is marshalled onto the
        prompt-toolkit loop.
        """
        if self.refresh_seconds <= 0:
            return
        if self._refresh_thread is not None and self._refresh_thread.is_alive():
            return
        self._refresh_stop.clear()

        def loop() -> None:
            while not self._refresh_stop.wait(self.refresh_seconds):
                try:
                    self._refresh_overview()
                except Exception:
                    continue
                app = self._app
                loop_obj = getattr(app, "loop", None) if app is not None else None
                call_soon = (
                    getattr(loop_obj, "call_soon_threadsafe", None)
                    if loop_obj is not None
                    else None
                )
                if callable(call_soon):
                    try:
                        call_soon(self._invalidate)
                    except RuntimeError:
                        # The UI loop is shutting down.
                        return

        self._refresh_thread = threading.Thread(
            target=loop, name="orion-cockpit-refresh", daemon=True
        )
        self._refresh_thread.start()

    def _stop_refresh_loop(self) -> None:
        self._refresh_stop.set()
        thread = self._refresh_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._refresh_thread = None

    def _invalidate(self) -> None:
        """Ask prompt-toolkit to redraw with the current cached snapshot."""
        app = self._app
        if app is not None:
            try:
                app.invalidate()
            except Exception:
                pass

    def _refresh_overview(self) -> dict[str, Any]:
        """Refresh safe header/dashboard data without doing work during redraws."""
        try:
            snapshot = self._snapshot()
        except Exception:
            snapshot = self._current_snapshot()
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
        snapshot = self._current_snapshot()
        state = self._runtime_label(snapshot) or ("ONLINE" if self._running else "READY")
        mode = self._view_mode.upper()
        # Ordered by usefulness so the width budget drops the least important
        # entries first.  Spend outranks the model name: which model is
        # configured is discoverable from /status, whereas the running cost is
        # the thing an operator watches.
        details: list[tuple[str, str]] = [
            ("class:header.mode", mode),
            ("class:header.state", self._compact(state, 16)),
        ]
        if self._pending_approvals_count is not None:
            details.append(("class:header.meta", f"approvals:{self._pending_approvals_count}"))
        events = self._event_label(snapshot)
        if events:
            details.append(("class:header.meta", f"events:{self._compact(events, 18)}"))
        model = self._model_label(snapshot)
        if model:
            details.append(("class:header.meta", self._compact(model, 28)))
        fragments: list[tuple[str, str]] = [("class:header.brand", " ORION ")]
        used = len(" ORION ")
        width = self._terminal_columns()
        if self._is_browsing_history():
            # Surfaced in the header rather than only the footer so the reason
            # new output is not appearing is visible at a glance.
            indicator = " SCROLLED BACK "
            if used + len(indicator) <= width:
                fragments.append(("class:header.alert", indicator))
                used += len(indicator)
        for style, text in details:
            separator = " | "
            if used + len(separator) + len(text) > width:
                continue
            fragments.append(("class:header.sep", separator))
            fragments.append((style, text))
            used += len(separator) + len(text)
        # Cost goes last but with a reserved slot: pick the richest variant that
        # still fits, so a narrow terminal shows the figure rather than losing
        # it behind the model name.
        for candidate in self._cost_variants(snapshot):
            separator = " | "
            if used + len(separator) + len(candidate) <= width:
                fragments.append(("class:header.sep", separator))
                fragments.append(("class:header.cost", candidate))
                used += len(separator) + len(candidate)
                break
        return FormattedText(fragments)

    def _is_browsing_history(self) -> bool:
        """True while the operator has scrolled away from live output."""
        return self._view_mode == "chat" and not self._follow_tail

    def _footer_fragments(self):
        # The reading-position hint comes first: when the transcript is not
        # tailing, the operator needs to know why new output is not on screen
        # and how to get back, more than they need the key list.
        groups: list[tuple[tuple[str, str], ...]] = []
        if self._is_browsing_history():
            groups.append(
                (
                    ("class:footer.alert", " history "),
                    ("class:footer", " End to follow live "),
                )
            )
        groups.extend(
            (
                (("class:footer.key", " PgUp/Dn "), ("class:footer", "scroll")),
                (("class:footer.key", " Enter "), ("class:footer", "send")),
                (("class:footer.key", " /help "), ("class:footer", "help")),
            )
        )
        if not self._is_browsing_history():
            groups.append(
                (("class:footer.key", " End "), ("class:footer", "tail"))
            )
        width = self._terminal_columns()
        fragments: list[tuple[str, str]] = []
        used = 0
        for group in groups:
            group_width = sum(len(text) for _, text in group)
            spacer = 2 if fragments else 0
            if used + spacer + group_width > width:
                break
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

    def _viewport_anchor(self) -> tuple[int, int] | None:
        """Remember the cursor's cell so a refresh can restore the viewport."""
        view = self._view
        if view is None:
            return None
        document = view.buffer.document
        return (document.cursor_position_row, document.cursor_position_col)

    def _refresh_chat_view(self) -> None:
        """Refresh prompt_toolkit-owned widgets on its event-loop thread."""
        with self._write_lock:
            if self._view is None or self._view_mode != "chat":
                return
            # Capture the reading position before the document is rebuilt; the
            # viewport follows the cursor, so losing the cell would slide the
            # transcript out from under an operator who is reading history.
            anchor = None if self._follow_tail else self._viewport_anchor()
            text, styles = self._transcript_blocks()
        # Rebuilding an unchanged document still resets the viewport, so skip
        # the assignment entirely when the rendered transcript is identical.
        if text == self._view.text:
            return
        self._replace_view_text(text, anchor=anchor, style_map=styles)
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
        text = " ".join(str(getattr(output, "text", None) or output.content).split())
        if not text:
            return "Orion · traitement en cours"
        # Assistant content emitted alongside tool calls is Orion's native
        # progress update.  Show it in full: these preambles are short by
        # nature, and truncating them produced dangling "…" lines that lost the
        # actual instruction Orion was describing.
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

    @staticmethod
    def _format_view_data(data: Any) -> str:
        """Render command data readably rather than as a raw JSON dump."""

        def scalar(value: Any) -> str:
            if isinstance(value, bool):
                return "yes" if value else "no"
            if value is None:
                return "-"
            if isinstance(value, (dict, list)):
                return json.dumps(value, ensure_ascii=False, default=str)
            return str(value)

        lines: list[str] = []
        if isinstance(data, list):
            for index, item in enumerate(data, start=1):
                if isinstance(item, dict):
                    parts = [f"{key}={scalar(value)}" for key, value in item.items()]
                    lines.append(f"{index}. " + "  ".join(parts))
                else:
                    lines.append(f"{index}. {scalar(item)}")
            return "\n".join(lines) if lines else "(vide)"
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, list):
                    lines.append(f"{key}:")
                    for item in value:
                        if isinstance(item, dict):
                            parts = [
                                f"{k}={scalar(v)}" for k, v in item.items()
                            ]
                            lines.append("  - " + "  ".join(parts))
                        else:
                            lines.append(f"  - {scalar(item)}")
                elif isinstance(value, dict):
                    lines.append(f"{key}:")
                    for sub_key, sub_value in value.items():
                        lines.append(f"  {sub_key}: {scalar(sub_value)}")
                else:
                    lines.append(f"{key}: {scalar(value)}")
            return "\n".join(lines) if lines else "(vide)"
        return scalar(data)

    def _render_result(self, result: Any) -> None:
        if self._view is not None:
            if isinstance(result, dict) and result.get("error"):
                rendered = "ERROR\n" + str(result["error"])
            elif result is not None:
                title = result.get("title") if isinstance(result, dict) else None
                data = (
                    result.get("data", result) if isinstance(result, dict) else result
                )
                # Prefer the backend's human-readable rendering.  Dumping raw
                # JSON here made command output read like a data structure
                # rather than a report, and it inherited the chat body styling.
                display = result.get("display") if isinstance(result, dict) else None
                if isinstance(display, str) and display.strip():
                    body = display
                else:
                    body = self._format_view_data(data)
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
        # Read by the header on the UI thread while the refresh thread writes
        # it, so publish under a lock.
        with self._snapshot_lock:
            self._last_snapshot = snapshot
        return snapshot

    def _current_snapshot(self) -> dict[str, Any]:
        with self._snapshot_lock:
            return self._last_snapshot

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
        if _TranscriptLexer is not None:
            transcript.control.lexer = _TranscriptLexer(self)
        self._view = transcript
        # Give the transcript a real scrollbar so the reading position is
        # visible instead of only inferable from the content.
        if ScrollbarMargin is not None:
            transcript.window.right_margins = [
                *list(transcript.window.right_margins),
                ScrollbarMargin(display_arrows=True),
            ]
        initial_text, initial_styles = self._transcript_blocks()
        self._replace_view_text(
            initial_text,
            cursor_position=len(initial_text),
            style_map=initial_styles,
        )
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
                # Chrome: neutral grey, with orange carrying the accents.
                # Reverse/box decorations are intentionally avoided so the
                # layout stays calm on any terminal theme.
                "header.brand": "bold #ff9e64",
                "header.sep": "#5c6370",
                "header.state": "#d8dee9",
                "header.mode": "#ff9e64",
                "header.meta": "#8b949e",
                "header.cost": "#ffb86b",
                "header.alert": "bold #ff9e64",
                "footer": "#8b949e",
                "footer.key": "#d8dee9",
                "footer.alert": "bold #ff9e64",
                "section.label": "bold #ff9e64",
                "scrollbar.background": "bg:#3b3f46",
                "scrollbar.button": "bg:#8b949e",
                "scrollbar.arrow": "bg:#3b3f46 #d8dee9",
                # Transcript roles.  White for what Orion says, grey for the
                # operator's own input and for machine chatter, orange for
                # worker traffic and view captions.
                "transcript.user": "bold #8b949e",
                "transcript.user.body": "#c9d1d9",
                "transcript.orion": "bold #ff9e64",
                "transcript.body": "#e6edf3",
                "transcript.worker": "bold #ffb86b",
                "transcript.notice": "#8b949e italic",
                # Dashboard / help / command output: grey, so it is clearly not
                # part of the conversation.
                "transcript.view": "#b9c0c9",
                # Markdown.  Headings and rules use the orange accent, code is
                # set apart from prose, and emphasis only changes weight so the
                # palette stays white/grey/orange.
                "md.h1": "bold #ff9e64",
                "md.h2": "bold #ffb86b",
                "md.h3": "bold #d8dee9",
                "md.h4": "#b9c0c9",
                "md.strong": "bold",
                "md.em": "italic",
                "md.code": "#ffb86b",
                "md.codeblock": "#8b949e",
                "md.bullet": "#ff9e64",
                "md.quote": "#8b949e italic",
                "md.link": "underline #ff9e64",
                "md.rule": "#5c6370",
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
        self._start_refresh_loop()
        try:
            with _suppress_native_stderr():
                try:
                    app.run()
                except (EOFError, KeyboardInterrupt):
                    # Real terminal/pipe hangup can surface as EOFError before
                    # a key binding is dispatched. Treat it like Ctrl+D/C.
                    pass
        finally:
            self._stop_refresh_loop()
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
        # Read stdin on a daemon thread and consume it through a queue so the
        # stop flag observed between iterations is enough to end the session.
        # Iterating ``self.input`` directly blocks inside the read, so SIGINT
        # and SIGTERM -- whose handlers only set that flag -- could never take
        # effect on a pipe that stays open without producing another line, and
        # the process had to be killed with SIGKILL.
        lines: "queue.Queue[object]" = queue.Queue()
        _EOF = object()

        def _reader() -> None:
            try:
                for raw in self.input:
                    lines.put(raw)
            except Exception:
                pass
            finally:
                lines.put(_EOF)

        threading.Thread(target=_reader, name="orion-cli-reader", daemon=True).start()
        while not self._stop.is_set():
            try:
                raw = lines.get(timeout=0.2)
            except queue.Empty:
                continue
            if raw is _EOF:
                break
            line = str(raw).rstrip("\r\n")
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
