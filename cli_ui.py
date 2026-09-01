"""Interface terminal interactive d'Orion.

Rich assure le rendu Markdown et prompt_toolkit maintient le prompt stable
pendant les sorties asynchrones. Un mode de repli reste disponible lorsque
ces dépendances ne sont pas installées ou que la sortie n'est pas un TTY.
"""

from __future__ import annotations

import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
import random

try:
    from rich.console import Console, Group
    from rich.markdown import Markdown
    from rich.padding import Padding
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    _RICH_AVAILABLE = True
except ImportError:  # pragma: no cover - repli pour installation incomplète
    _RICH_AVAILABLE = False

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.application.current import get_app
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.history import FileHistory, InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.patch_stdout import patch_stdout
    from prompt_toolkit.styles import Style

    _PROMPT_AVAILABLE = True
except ImportError:  # pragma: no cover - repli pour installation incomplète
    _PROMPT_AVAILABLE = False


_COMMANDS = ("/help", "/status", "/tools", "/tasks", "/agents", "/jobs", "/clear", "/exit")


class CLIConsole:
    """Terminal sobre, thread-safe et adapté aux réponses asynchrones."""

    _ACCENT = "#d97757"
    _MUTED = "#8b8b8b"
    _PROGRESS = "#7aa2c8"

    def __init__(
        self,
        *,
        output: Any = None,
        use_color: bool | None = None,
        show_banner: bool = True,
        name: str = "Orion",
        model: str | None = None,
        history_path: str | Path | None = "data/cli_history.txt",
        render_markdown: bool = True,
        show_timestamps: bool = True,
    ) -> None:
        self.output = output or sys.stdout
        self._uses_stdout = output is None or output is sys.stdout
        self.name = name
        self.model = model
        self.show_banner = bool(show_banner)
        self.render_markdown = bool(render_markdown)
        self.show_timestamps = bool(show_timestamps)
        self._lock = threading.RLock()
        self._banner_shown = False
        self._busy = False

        if use_color is None:
            use_color = bool(getattr(self.output, "isatty", lambda: False)())
            use_color = use_color and os.environ.get("NO_COLOR") is None
        self.use_color = bool(use_color)
        self._interactive = bool(
            _PROMPT_AVAILABLE
            and getattr(sys.stdin, "isatty", lambda: False)()
            and getattr(self.output, "isatty", lambda: False)()
        )
        self._session: Any = None
        if self._interactive:
            self._session = self._build_session(history_path)

    def _console(self) -> Any:
        if not _RICH_AVAILABLE:
            return None
        target = sys.stdout if self._uses_stdout else self.output
        return Console(
            file=target,
            force_terminal=self.use_color,
            no_color=not self.use_color,
            highlight=False,
            soft_wrap=False,
        )

    def _build_session(self, history_path: str | Path | None) -> Any:
        key_bindings = KeyBindings()

        @key_bindings.add("enter")
        def submit(event: Any) -> None:
            event.current_buffer.validate_and_handle()

        @key_bindings.add("escape", "enter")
        def newline(event: Any) -> None:
            event.current_buffer.insert_text("\n")

        history: Any = InMemoryHistory()
        if history_path:
            path = Path(history_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            history = FileHistory(str(path))

        style = Style.from_dict(
            {
                "prompt": f"bold {self._ACCENT}",
                "continuation": self._MUTED,
                "bottom-toolbar": "bg:#252525 #b7b7b7",
                "completion-menu.completion": "bg:#252525 #d0d0d0",
                "completion-menu.completion.current": f"bg:{self._ACCENT} #ffffff",
            }
        )
        return PromptSession(
            multiline=True,
            history=history,
            auto_suggest=AutoSuggestFromHistory(),
            completer=WordCompleter(list(_COMMANDS), sentence=True),
            complete_while_typing=False,
            enable_history_search=True,
            key_bindings=key_bindings,
            style=style,
            mouse_support=False,
        )

    def _toolbar(self) -> FormattedText:
        if self._busy:
            return FormattedText(
                [
                    ("class:bottom-toolbar", "  Orion travaille…  "),
                    ("class:bottom-toolbar", "· vous pouvez continuer à écrire  "),
                ]
            )
        return FormattedText(
            [
                ("class:bottom-toolbar", "  Entrée envoyer  "),
                ("class:bottom-toolbar", "· Alt+Entrée nouvelle ligne  "),
                ("class:bottom-toolbar", "· /help  "),
            ]
        )

    def _print_plain(self, content: str = "") -> None:
        print(content, file=self.output, flush=True)

    def banner(self) -> None:
        if self._banner_shown or not self.show_banner:
            return
        self._banner_shown = True
        console = self._console()
        if console is None:
            self._print_plain(f"\n{self.name} · prêt")
            self._print_plain("Événements · tools · tâches durables\n")
            return
        accroches_orion = [
            "Ne m'appelle pas, je t'appellerai.",
            "Je ne dors pas, je veille.",
            "Un signal, une action. Comme Zimmer sur du silence.",
            "HAL aurait ouvert la porte, moi j'ouvre un event.",
            "Dans l'espace, personne ne t'entend... sauf Orion.",
            "Suis le lapin blanc. Ou l'event, au choix.",
            "Ce n'est pas un bug, c'est un déclencheur.",
            "Houston, on a un event.",
            "Je vois des events.",
            "Rien ne se perd, tout se déclenche.",
            "Third star to the right, straight on till event.",
            "Some people call it loop, I call it home.",
            "Pas besoin d'appuyer sur pause, j'attends déjà.",
            "May the event be with you.",
            "Un pas pour toi, un webhook pour l'humanité.",
        ]
        title = Text(self.name, style=f"bold {self._ACCENT}")
        subtitle = Text(random.choice(accroches_orion), style="bold")
        details = Text()
        if self.model:
            details.append("modèle  ", style=self._MUTED)
            details.append(self.model)
            details.append("\n")
        details.append("dossier  ", style=self._MUTED)
        details.append(str(Path.cwd()))
        details.append("\n\n")
        details.append("/help", style=f"bold {self._ACCENT}")
        details.append(" pour les commandes · Alt+Entrée pour une nouvelle ligne", style=self._MUTED)
        width = min(max(console.width, 40), 88)
        with self._lock:
            console.print()
            console.print(
                Panel(
                    Group(subtitle, Text(), details),
                    title=title,
                    title_align="left",
                    border_style=self._MUTED,
                    padding=(1, 2),
                    width=width,
                )
            )
            console.print()

    def read(self, prompt: str = "❯ ") -> str:
        if self._session is None:
            return input(prompt)
        with patch_stdout(raw=True):
            return self._session.prompt(
                FormattedText([("class:prompt", prompt)]),
                prompt_continuation=lambda *_: FormattedText(
                    [("class:continuation", "│ ")]
                ),
                bottom_toolbar=self._toolbar,
                reserve_space_for_menu=4,
            )

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        # La réponse arrive souvent depuis un worker. Sans invalidation,
        # prompt_toolkit peut conserver l'ancien texte de la toolbar à l'écran.
        if self._session is not None:
            try:
                get_app().invalidate()
            except Exception:
                # Le prompt n'est pas forcément actif (arrêt ou sortie non-TTY).
                pass

    def _time_label(self, timestamp: str | None) -> str:
        if not self.show_timestamps:
            return ""
        try:
            moment = datetime.fromisoformat(timestamp) if timestamp else datetime.now().astimezone()
            return moment.astimezone().strftime("%H:%M")
        except (TypeError, ValueError):
            return ""

    def assistant(
        self,
        content: str,
        *,
        intermediate: bool = False,
        timestamp: str | None = None,
    ) -> None:
        content = str(content).strip()
        if not content:
            return
        console = self._console()
        if console is None:
            label = "progression" if intermediate else self.name
            self._print_plain(f"\n{label}> {content}\n")
            return

        time_label = self._time_label(timestamp)
        with self._lock:
            console.print()
            if intermediate:
                line = Text("◇  ", style=self._PROGRESS)
                line.append(content, style="dim")
                if time_label:
                    line.append(f"  {time_label}", style=self._MUTED)
                console.print(Padding(line, (0, 1)))
            else:
                header = Text("●  ", style=f"bold {self._ACCENT}")
                header.append(self.name, style="bold")
                if time_label:
                    header.append(f"  {time_label}", style=self._MUTED)
                console.print(header)
                if self.render_markdown:
                    console.print(Padding(Markdown(content), (0, 1, 0, 3)))
                else:
                    console.print(Padding(Text(content), (0, 1, 0, 3)))
            console.print()

    def system(self, content: str) -> None:
        console = self._console()
        if console is None:
            self._print_plain(content)
            return
        with self._lock:
            console.print(Text(f"  {content}", style=self._MUTED))

    def warning(self, content: str) -> None:
        console = self._console()
        if console is None:
            self._print_plain(f"Attention : {content}")
            return
        with self._lock:
            console.print(Text.assemble(("  !  ", "bold yellow"), (content, "yellow")))

    def error(self, content: str) -> None:
        console = self._console()
        if console is None:
            self._print_plain(f"Erreur : {content}")
            return
        with self._lock:
            console.print()
            console.print(
                Panel(
                    Text(content),
                    title="Erreur Orion",
                    title_align="left",
                    border_style="red",
                    padding=(0, 1),
                )
            )
            console.print()

    def clear(self) -> None:
        console = self._console()
        if console is not None:
            console.clear()
        else:
            os.system("cls" if os.name == "nt" else "clear")
        self._banner_shown = False
        self.banner()

    def help(self) -> None:
        rows = [
            ("/help", "Afficher les commandes"),
            ("/status", "État du runtime et tâche active"),
            ("/tools", "Tools disponibles pour Orion"),
            ("/tasks", "Dernières tâches durables"),
            ("/agents", "Sous-agents disponibles"),
            ("/jobs", "Travaux délégués récents"),
            ("/clear", "Nettoyer l'écran"),
            ("/exit", "Arrêter Orion proprement"),
        ]
        console = self._console()
        if console is None:
            self._print_plain("\n" + "\n".join(f"{name:10} {description}" for name, description in rows))
            return
        table = Table.grid(padding=(0, 2))
        table.add_column(style=f"bold {self._ACCENT}", no_wrap=True)
        table.add_column()
        for row in rows:
            table.add_row(*row)
        with self._lock:
            console.print()
            console.print(Panel(table, title="Commandes", title_align="left", border_style=self._MUTED))
            console.print()

    def status(self, values: Mapping[str, Any]) -> None:
        console = self._console()
        if console is None:
            self._print_plain("\n" + "\n".join(f"{key}: {value}" for key, value in values.items()))
            return
        table = Table.grid(padding=(0, 2))
        table.add_column(style=self._MUTED, no_wrap=True)
        table.add_column(style="bold")
        for key, value in values.items():
            table.add_row(str(key), str(value))
        with self._lock:
            console.print()
            console.print(Panel(table, title="État", title_align="left", border_style=self._MUTED))
            console.print()

    def items(self, title: str, items: Sequence[Any], *, empty: str) -> None:
        console = self._console()
        if not items:
            self.system(empty)
            return
        if console is None:
            self._print_plain("\n" + title)
            for item in items:
                self._print_plain(f"- {item}")
            return
        table = Table.grid(padding=(0, 1))
        table.add_column(style=self._MUTED, no_wrap=True)
        table.add_column()
        for item in items:
            if isinstance(item, Mapping):
                identifier = str(item.get("id", "•"))
                description = str(
                    item.get("label")
                    or item.get("objective")
                    or item.get("name")
                    or item
                )
                status = item.get("status")
                if status:
                    description += f"  [{status}]"
                table.add_row(identifier, description)
            else:
                table.add_row("•", str(item))
        with self._lock:
            console.print()
            console.print(Panel(table, title=title, title_align="left", border_style=self._MUTED))
            console.print()


__all__ = ["CLIConsole"]
