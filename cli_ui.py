"""Interface terminal légère et sans dépendance pour Orion."""

from __future__ import annotations

import os
import sys
from typing import Any


class CLIConsole:
    """Rendu sobre du terminal, avec repli automatique sans couleur."""

    _RESET = "\033[0m"
    _DIM = "\033[2m"
    _CYAN = "\033[36m"
    _GREEN = "\033[32m"
    _YELLOW = "\033[33m"

    def __init__(
        self,
        *,
        output: Any = None,
        use_color: bool | None = None,
        show_banner: bool = True,
        name: str = "Orion",
    ) -> None:
        self.output = output or sys.stdout
        self.name = name
        self.show_banner = bool(show_banner)
        if use_color is None:
            use_color = bool(getattr(self.output, "isatty", lambda: False)())
            use_color = use_color and os.environ.get("NO_COLOR") is None
        self.use_color = bool(use_color)
        self._banner_shown = False

    def _paint(self, text: str, color: str) -> str:
        return f"{color}{text}{self._RESET}" if self.use_color else text

    def banner(self) -> None:
        if self._banner_shown or not self.show_banner:
            return
        self._banner_shown = True
        print(file=self.output)
        print(self._paint(f"  {self.name}  ·  prêt", self._CYAN), file=self.output)
        print(self._paint("  événements · outils · tâches durables", self._DIM), file=self.output)
        print(file=self.output, flush=True)

    def read(self, prompt: str = "› ") -> str:
        return input(self._paint(prompt, self._CYAN))

    def assistant(self, content: str, *, intermediate: bool = False) -> None:
        marker = "·" if intermediate else "●"
        label = "progression" if intermediate else self.name.lower()
        prefix = self._paint(f"{marker} {label}> ", self._YELLOW if intermediate else self._GREEN)
        lines = str(content).strip().splitlines() or [""]
        print(file=self.output)
        print(prefix + lines[0], file=self.output)
        continuation = " " * len(prefix)
        for line in lines[1:]:
            print(continuation + line, file=self.output)
        print(file=self.output, flush=True)

    def system(self, content: str) -> None:
        print(self._paint(f"  {content}", self._DIM), file=self.output, flush=True)


__all__ = ["CLIConsole"]
