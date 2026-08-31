"""CLI interactive de découverte et d'installation des tools GitHub Orion."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from github_tools import GithubTool, GithubToolCatalog
from orion_config import OrionConfig
from tool_manager import ToolManager, ToolPackageError


class ToolboxUI:
    """Rendu compact, lisible et sans dépendance externe."""

    def __init__(self) -> None:
        self.color = bool(getattr(sys.stdout, "isatty", lambda: False)()) and os.getenv("NO_COLOR") is None

    def paint(self, value: str, code: str) -> str:
        return f"\033[{code}m{value}\033[0m" if self.color else value

    def title(self, repository: str, ref: str) -> None:
        print()
        print(self.paint("  Orion Toolbox", "36;1"))
        print(self.paint(f"  {repository}  ·  {ref}", "2"))
        print()

    def info(self, value: str) -> None:
        print(self.paint(f"  · {value}", "2"))

    def success(self, value: str) -> None:
        print(self.paint(f"  ✓ {value}", "32"))

    def error(self, value: str) -> None:
        print(self.paint(f"  × {value}", "31"), file=sys.stderr)

    def tools(self, values: list[GithubTool]) -> None:
        if not values:
            self.info("Aucun tool ne correspond à cette recherche.")
            return
        print(self.paint("  Tools disponibles", "1"))
        for index, item in enumerate(values, 1):
            manifest = item.manifest
            print(f"  {index:>2}. {manifest.name}  {self.paint('v' + manifest.version, '2')}")
            print(f"      {manifest.id}  ·  {manifest.description or 'sans description'}")
        print()

    def help(self) -> None:
        print("  n       installer le tool numéro n")
        print("  /texte  filtrer les tools par texte")
        print("  r       recharger le dépôt")
        print("  q       quitter")


def _manager(config: OrionConfig) -> ToolManager:
    return ToolManager(
        config.path(config.tools.directory),
        state_path=config.path(config.tools.state_path),
        root_dir=config.base_dir,
        config={"enabled": config.tools.enabled, "disabled": config.tools.disabled, **config.tools.settings},
    )


def _select(values: list[GithubTool], value: str) -> GithubTool | None:
    try:
        index = int(value) - 1
    except ValueError:
        return next((item for item in values if item.manifest.id == value), None)
    return values[index] if 0 <= index < len(values) else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Découvrir et installer des tools Orion depuis GitHub")
    parser.add_argument("--config", type=Path, default=Path("orion.toml"))
    parser.add_argument("--repo", help="Dépôt public au format owner/repository")
    parser.add_argument("--ref", help="Branche ou tag GitHub")
    parser.add_argument("--search", help="Filtre non interactif")
    parser.add_argument("--install", metavar="ID", help="Installer directement un identifiant")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = OrionConfig.from_file(args.config)
    github_settings: dict[str, Any] = dict(config.tools.settings.get("github", {}))
    repository = args.repo or str(github_settings.get("repo", ""))
    if not repository:
        repository = input("Dépôt GitHub (owner/repository) : ").strip()
    ref = args.ref or str(github_settings.get("ref", "main"))
    catalog = GithubToolCatalog(
        repository,
        ref=ref,
        timeout=int(github_settings.get("timeout", 20)),
    )
    manager = _manager(config)
    ui = ToolboxUI()
    ui.title(catalog.repository, catalog.ref)

    try:
        values = catalog.search(args.search or "")
        if args.install:
            selected = next((item for item in values if item.manifest.id == args.install), None)
            if selected is None:
                raise ToolPackageError(f"Tool introuvable : {args.install}")
            existing = {manifest.id for manifest, _ in manager.installed()}
            force = selected.manifest.id in existing
            manifest = catalog.install(selected, manager, force=force)
            ui.success(f"{manifest.id} {manifest.version} installé.")
            return 0
    except ToolPackageError as exc:
        ui.error(str(exc))
        return 1

    ui.tools(values)
    ui.help()
    while True:
        try:
            command = input("\n  toolbox › ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if command.lower() in {"q", "quit", "exit"}:
            return 0
        if command.lower() == "h":
            ui.help()
            continue
        if command.lower() == "r":
            catalog._tree = None
            catalog._tools = None
            try:
                values = catalog.search()
                ui.tools(values)
            except ToolPackageError as exc:
                ui.error(str(exc))
            continue
        if command.startswith("/"):
            try:
                values = catalog.search(command[1:])
                ui.tools(values)
            except ToolPackageError as exc:
                ui.error(str(exc))
            continue
        selected = _select(values, command)
        if selected is None:
            ui.info("Commande inconnue. Utilise h pour l'aide.")
            continue
        existing = {manifest.id for manifest, _ in manager.installed()}
        force = selected.manifest.id in existing
        action = "Mettre à jour" if force else "Installer"
        answer = input(f"  {action} {selected.manifest.name} ? [Y/n] ").strip().lower()
        if answer not in {"", "y", "yes", "o", "oui"}:
            ui.info("Annulé.")
            continue
        try:
            manifest = catalog.install(selected, manager, force=force)
            ui.success(f"{manifest.id} {manifest.version} installé.")
        except ToolPackageError as exc:
            ui.error(str(exc))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ToolPackageError as exc:
        raise SystemExit(f"[orion-toolbox:error] {exc}") from exc
