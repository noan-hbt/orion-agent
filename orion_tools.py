"""Utilitaire d'installation des extensions Orion.

Exemples::

    python orion_tools.py list
    python orion_tools.py install ./mon-tool
    python orion_tools.py install https://exemple.org/orion-tool.zip
    python orion_tools.py update mon-tool
    python orion_tools.py update --all
    python orion_tools.py remove mon-tool --yes
"""

from __future__ import annotations

import argparse
from pathlib import Path

from orion_config import OrionConfig
from tool_manager import ToolManager, ToolPackageError


def _manager(config_path: Path) -> ToolManager:
    config = OrionConfig.from_file(config_path)
    return ToolManager(
        config.path(config.tools.directory),
        state_path=config.path(config.tools.state_path),
        root_dir=config.base_dir,
        config={
            "enabled": config.tools.enabled,
            "disabled": config.tools.disabled,
            **config.tools.settings,
        },
    )


def _config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("orion.toml"),
        help="Fichier de configuration Orion (défaut : orion.toml).",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Gestionnaire de tools Orion")
    _config_argument(parser)
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("list", help="Lister les tools installés.")

    install = commands.add_parser("install", help="Installer un dossier, zip ou URL.")
    install.add_argument("source", help="Chemin local ou URL d'une archive .zip.")
    install.add_argument("--force", action="store_true", help="Remplacer la version existante.")
    install.add_argument("--configure", action="store_true", help="Demander les paramètres déclarés par le tool.")

    update = commands.add_parser("update", help="Mettre à jour un ou tous les tools.")
    update.add_argument("tool_id", nargs="?", help="Identifiant du tool à mettre à jour.")
    update.add_argument("--all", action="store_true", help="Mettre à jour tous les tools ayant une source.")
    update.add_argument("--configure", action="store_true", help="Demander les paramètres déclarés par le tool.")

    remove = commands.add_parser("remove", help="Désinstaller un tool.")
    remove.add_argument("tool_id", help="Identifiant du tool.")
    remove.add_argument("--yes", action="store_true", help="Ne pas demander de confirmation.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = OrionConfig.from_file(args.config)
    manager = _manager(args.config)

    if args.command == "list":
        installed = manager.installed()
        if not installed:
            print("Aucun tool installé.")
            return 0
        for manifest, package_dir in installed:
            print(f"{manifest.id} {manifest.version} — {manifest.name} [{package_dir}]")
        return 0

    if args.command == "install":
        manifest = manager.install(args.source, force=args.force)
        if args.configure:
            manager.configure(
                manifest,
                config_path=config.config_path or args.config,
                env_path=config.base_dir / ".env",
            )
        print(f"Tool installé : {manifest.id} {manifest.version}")
        return 0

    if args.command == "update":
        state = manager._read_state()
        if args.all:
            tool_ids = sorted(state)
        elif args.tool_id:
            tool_ids = [args.tool_id]
        else:
            raise ToolPackageError("Indique un identifiant ou utilise --all.")
        updated = 0
        for tool_id in tool_ids:
            source = state.get(tool_id, {}).get("source")
            if not source:
                print(f"Ignoré (source inconnue) : {tool_id}")
                continue
            if str(source).startswith("github://"):
                from github_tools import install_github_source

                manifest = install_github_source(str(source), manager, force=True)
            else:
                manifest = manager.install(source, force=True)
            if args.configure:
                manager.configure(
                    manifest,
                    config_path=config.config_path or args.config,
                    env_path=config.base_dir / ".env",
                )
            print(f"Tool mis à jour : {manifest.id} {manifest.version}")
            updated += 1
        if updated == 0:
            print("Aucun tool mis à jour.")
        return 0

    if args.command == "remove":
        if not args.yes:
            answer = input(f"Désinstaller {args.tool_id} ? [y/N] ")
            if answer.strip().lower() not in {"y", "yes", "o", "oui"}:
                print("Annulé.")
                return 0
        manager.remove(args.tool_id)
        print(f"Tool désinstallé : {args.tool_id}")
        return 0

    raise ToolPackageError(f"Commande inconnue : {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ToolPackageError as exc:
        raise SystemExit(f"[orion-tools:error] {exc}") from exc
