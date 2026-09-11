"""Toolbox Orion : modules bundled locaux et packages distants optionnels."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TextIO

from github_tools import GithubTool, GithubToolCatalog
from orion_config import OrionConfig
from tool_manager import ToolManager, ToolManifest, ToolPackageError
from tool_policy import ToolClassification


class ToolboxUI:
    """Dashboard terminal sans dÃ©pendance, lisible sous Windows et en pipe."""

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        error_stream: TextIO | None = None,
        width: int | None = None,
        is_tty: bool | None = None,
    ) -> None:
        self.stream = stream or sys.stdout
        self.error_stream = error_stream or sys.stderr
        detected_tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self.is_tty = detected_tty if is_tty is None else bool(is_tty)
        self.color = self.is_tty and os.getenv("NO_COLOR") is None
        detected_width = shutil.get_terminal_size(fallback=(88, 24)).columns
        self.width = max(52, min(int(width or detected_width), 120))

    def _print(self, value: str = "", *, error: bool = False) -> None:
        print(value, file=self.error_stream if error else self.stream)

    def paint(self, value: str, code: str) -> str:
        return f"\033[{code}m{value}\033[0m" if self.color else value

    def title(self, repository: str | None, ref: str | None = None) -> None:
        self._print()
        self._print(self.paint("  ORION TOOLBOX", "36;1"))
        if repository:
            self._print(self.paint(f"  Remote: {repository} @ {ref or 'main'}", "2"))
        else:
            self._print(self.paint("  Local capabilities Â· remote not required", "2"))
        self._print()

    def info(self, value: str) -> None:
        self._print(self.paint(f"  - {value}", "2"))

    def success(self, value: str) -> None:
        self._print(self.paint(f"  OK  {value}", "32"))

    def error(self, value: str) -> None:
        self._print(self.paint(f"  ERR {value}", "31"), error=True)

    def divider(self, label: str | None = None) -> None:
        if not label:
            self._print("  " + "-" * (self.width - 4))
            return
        prefix = f"-- {label} "
        self._print("  " + prefix + "-" * max(2, self.width - len(prefix) - 4))

    def _wrapped(self, text: str, *, indent: int = 8) -> list[str]:
        width = max(20, self.width - indent - 2)
        return textwrap.wrap(str(text), width=width) or [""]

    @staticmethod
    def _state(manifest: ToolManifest, enabled: set[str], disabled: set[str]) -> str:
        if manifest.id in disabled:
            return "disabled"
        if manifest.id in enabled:
            return "enabled"
        return "off"

    def _state_badge(self, state: str) -> str:
        labels = {"enabled": "[ON]", "off": "[OFF]", "disabled": "[DISABLED]"}
        code = "32" if state == "enabled" else "31" if state == "disabled" else "2"
        # Pad before applying ANSI escapes so invisible bytes never affect the
        # column width on Windows terminals.
        return self.paint(f"{labels[state]:<10}", code)

    def _package_row(
        self,
        index: int,
        manifest: ToolManifest,
        *,
        state: str,
        source: str,
        worker_access: str | None = None,
    ) -> None:
        badge = self._state_badge(state)
        version = self.paint(f"v{manifest.version}", "2")
        self._print(f"  {index:>2}  {badge} {manifest.name}  {version}")
        self._print(self.paint(f"      {manifest.id}  |  {source}", "2"))
        if worker_access:
            self._print(self.paint(f"      Workers: {worker_access}", "2"))
        if manifest.description:
            for line in self._wrapped(manifest.description):
                self._print(f"      {line}")

    def tools(self, values: list[GithubTool]) -> None:
        if not values:
            self.info("Aucun tool ne correspond Ã  cette recherche.")
            return
        self.divider("REMOTE CATALOG")
        for index, item in enumerate(values, 1):
            manifest = item.manifest
            self._print(f"  R{index:<2} {manifest.name}  {self.paint('v' + manifest.version, '2')}")
            self._print(self.paint(f"      {manifest.id}", "2"))
            if manifest.description:
                for line in self._wrapped(manifest.description):
                    self._print(f"      {line}")
        self._print()

    def packages(
        self,
        values: list[tuple[ToolManifest, Path]],
        *,
        enabled: set[str],
        disabled: set[str],
    ) -> None:
        self.divider("LOCAL CAPABILITIES")
        if not values:
            self.info("Aucun package bundled ou installÃ©.")
            return
        for index, (manifest, _package_dir) in enumerate(values, 1):
            self._package_row(
                index,
                manifest,
                state=self._state(manifest, enabled, disabled),
                source="bundled module" if manifest.kind == "module" else "local tool",
            )
        self._print()

    def dashboard(
        self,
        values: list["LocalPackage"],
        *,
        enabled: set[str],
        disabled: set[str],
        repository: str | None,
        ref: str,
        remote: list[GithubTool] | None = None,
        worker_access: dict[str, str] | None = None,
        worker_summary: str | None = None,
    ) -> None:
        """Render one stable dashboard frame; no cursor control is required."""
        self.title(repository, ref)
        states = [self._state(item.manifest, enabled, disabled) for item in values]
        self._print(
            "  STATUS  "
            + self.paint(f"{states.count('enabled')} enabled", "32")
            + f"  |  {states.count('off')} off  |  "
            + self.paint(f"{states.count('disabled')} disabled", "31")
        )
        if worker_summary:
            self._print(self.paint(f"  WORKERS {worker_summary}", "2"))
        self._print()

        modules = [item for item in values if item.manifest.kind == "module"]
        tools = [item for item in values if item.manifest.kind != "module"]
        indexes = {item.manifest.id: index for index, item in enumerate(values, 1)}
        self.divider("BUNDLED MODULES")
        if modules:
            for item in modules:
                self._package_row(
                    indexes[item.manifest.id],
                    item.manifest,
                    state=self._state(item.manifest, enabled, disabled),
                    source=item.source,
                    worker_access=(worker_access or {}).get(item.manifest.id),
                )
        else:
            self.info("Aucun module bundled.")
        self._print()

        self.divider("LOCAL TOOLS")
        if tools:
            for item in tools:
                self._package_row(
                    indexes[item.manifest.id],
                    item.manifest,
                    state=self._state(item.manifest, enabled, disabled),
                    source=item.source,
                    worker_access=(worker_access or {}).get(item.manifest.id),
                )
        else:
            self.info("Aucun tool local.")
        self._print()

        self.divider("REMOTE")
        if not repository:
            self.info("Non configurÃ©. Les modules locaux restent utilisables sans GitHub.")
        elif remote is None:
            self.info("Catalogue non chargÃ©. Tape `r` pour le charger, `/texte` pour rechercher.")
        elif remote:
            for index, item in enumerate(remote, 1):
                self._print(f"  R{index:<2} {item.manifest.name}  {self.paint('v' + item.manifest.version, '2')}")
                self._print(self.paint(f"      {item.manifest.id}", "2"))
        else:
            self.info("Aucun rÃ©sultat distant.")
        self._print()
        footer = "# detail | enable # | disable # | config # | w worker tools | r remote | h help | q quit"
        for line in self._wrapped(footer, indent=4):
            self._print(self.paint(f"  {line}", "2"))

    def detail(
        self,
        item: "LocalPackage",
        state: str,
        *,
        worker_access: str | None = None,
        worker_action: str | None = None,
    ) -> None:
        self._print()
        self.divider("DETAIL")
        self._print(f"  {self.paint(item.manifest.name, '1')}  {self._state_badge(state)}")
        self._print(f"  ID       {item.manifest.id}")
        self._print(f"  Type     {'module' if item.manifest.kind == 'module' else 'tool'}")
        self._print(f"  Source   {item.source}")
        self._print(f"  Version  {item.manifest.version}")
        self._print(f"  Risk     {item.manifest.policy.classification.value}")
        self._print(
            f"  Settings {len(item.manifest.configuration.get('fields', []))}"
        )
        if worker_access:
            self._print(f"  Workers  {worker_access}")
        if item.manifest.permissions:
            self._print(f"  Access   {', '.join(item.manifest.permissions)}")
        if item.manifest.description:
            self._print("  About")
            for line in self._wrapped(item.manifest.description, indent=6):
                self._print(f"    {line}")
        actions: list[str] = []
        if state != "enabled":
            actions.append("e enable")
        if state != "disabled":
            actions.append("d disable")
        if item.manifest.configuration.get("fields", []):
            actions.append("c configure")
        if worker_action:
            actions.append(worker_action)
        actions.append("b back")
        self._print()
        self._print("  Actions: " + "  |  ".join(actions))

    def worker_tools(
        self,
        values: list[tuple["LocalPackage", str, tuple[str, ...]]],
        *,
        subagents_enabled: bool,
    ) -> None:
        self._print()
        self.divider("SUBAGENT TOOL ACCESS")
        if subagents_enabled:
            self._print("  Subagents: ON")
        else:
            self._print("  Subagents: OFF (la sÃ©lection sera conservÃ©e pour une activation future)")
        self._print(
            "  Ce plafond est distinct des tools d'Orion : un worker ne peut utiliser que les tools cochÃ©s ici."
        )
        self._print()
        if not values:
            self.info("Aucun tool local disponible.")
            return
        markers = {
            "allowed": "[x]",
            "blocked": "[ ]",
            "approval_allowed": "[A]",
            "approval_blocked": "[!]",
            "partial": "[~]",
            "inactive": "[-]",
            "configured_off": "[~]",
            "unmapped": "[?]",
        }
        labels = {
            "allowed": "autorisÃ© aux workers",
            "blocked": "bloquÃ© pour les workers",
            "approval_allowed": "autorisÃ© via approbation Orion/opÃ©rateur",
            "approval_blocked": "bloquÃ© ; peut Ãªtre autorisÃ© avec approbation Ã  chaque appel",
            "partial": "autorisation partielle",
            "inactive": "tool Orion inactif",
            "configured_off": "autorisÃ© mais tool Orion inactif",
            "unmapped": "callable non dÃ©clarÃ© par le package",
        }
        for index, (item, state, callables) in enumerate(values, 1):
            self._print(f"  {index:>2}  {markers[state]:<3} {item.manifest.name}")
            callable_text = ", ".join(callables) if callables else "(aucun callable dÃ©lÃ©guable)"
            self._print(self.paint(f"      {callable_text}  |  {labels[state]}", "2"))
        self._print()
        self._print("  Actions: # toggle  |  a allow all safe+active  |  n none  |  b back")

    def remote_detail(self, item: GithubTool, *, installed: bool) -> None:
        self._print()
        self.divider("REMOTE DETAIL")
        manifest = item.manifest
        self._print(f"  {self.paint(manifest.name, '1')}  v{manifest.version}")
        self._print(f"  ID      {manifest.id}")
        self._print(f"  State   {'installed locally' if installed else 'not installed'}")
        self._print(f"  Risk    {manifest.policy.classification.value}")
        self._print(f"  Settings {len(manifest.configuration.get('fields', []))}")
        if manifest.description:
            for line in self._wrapped(manifest.description, indent=4):
                self._print(f"    {line}")
        self._print()
        self._print(f"  Actions: i {'update' if installed else 'install'}  |  b back")

    def help(self) -> None:
        self._print()
        self.divider("COMMANDS")
        self._print("  #                 ouvrir la fiche d'un module/tool local")
        self._print("  enable #|id       activer explicitement")
        self._print("  disable #|id      dÃ©sactiver explicitement")
        self._print("  config #|id       configurer sans changer l'activation")
        self._print("  c                 choisir rapidement un package Ã  configurer")
        self._print("  w | workers       gÃ©rer les tools transmissibles aux sous-agents")
        self._print("  r | remote        charger/recharger le catalogue distant")
        self._print("  /texte            rechercher dans le catalogue distant")
        self._print("  R#                ouvrir la fiche d'un rÃ©sultat distant")
        self._print("  dashboard | d     rÃ©afficher l'Ã©cran principal")
        self._print("  q                 quitter")


@dataclass(frozen=True)
class LocalPackage:
    manifest: ToolManifest
    path: Path
    source: str


def _subagent_allowed_tools(manager: ToolManager) -> set[str]:
    core = manager.config.get("_core", {})
    if not isinstance(core, dict):
        return set()
    subagents = core.get("subagents", {})
    if not isinstance(subagents, dict):
        return set()
    raw = subagents.get("default_tools", [])
    if not isinstance(raw, list):
        return set()
    return {str(item).strip() for item in raw if str(item).strip()}


def _manifest_worker_callables(manifest: ToolManifest) -> tuple[str, ...]:
    """Return callable names declared by a package manifest."""

    return tuple(name for name, _classification in manifest.policy.tool_classifications)


def _manifest_safe_worker_callables(manifest: ToolManifest) -> tuple[str, ...]:
    return tuple(
        name
        for name, classification in manifest.policy.tool_classifications
        if classification is not ToolClassification.PRIVILEGED
    )


def _manifest_has_privileged_callables(manifest: ToolManifest) -> bool:
    return any(
        classification is ToolClassification.PRIVILEGED
        for _name, classification in manifest.policy.tool_classifications
    )


def _worker_tool_state(manager: ToolManager, item: LocalPackage) -> tuple[str, tuple[str, ...]]:
    manifest = item.manifest
    callables = _manifest_worker_callables(manifest)
    declared = tuple(name for name, _classification in manifest.policy.tool_classifications)
    allowed = _subagent_allowed_tools(manager)
    enabled, disabled = _activation_sets(manager)
    active = manifest.id in enabled and manifest.id not in disabled

    if not declared:
        return "unmapped", ()
    configured = set(callables) & allowed
    if not active:
        return ("configured_off" if configured else "inactive"), callables
    if _manifest_has_privileged_callables(manifest):
        if configured == set(callables):
            return "approval_allowed", callables
        if configured:
            return "partial", callables
        return "approval_blocked", callables
    if configured == set(callables):
        return "allowed", callables
    if configured:
        return "partial", callables
    return "blocked", callables


def _worker_access_label(state: str) -> str:
    return {
        "allowed": "ALLOWED",
        "blocked": "BLOCKED (use w to allow)",
        "approval_allowed": "ALLOWED (approval required per privileged call)",
        "approval_blocked": "BLOCKED (use w; approval still required)",
        "partial": "PARTIAL",
        "inactive": "unavailable (tool off)",
        "configured_off": "configured, but tool off",
        "unmapped": "unavailable (package callable metadata missing)",
    }[state]


def _set_subagent_allowed_tools(
    manager: ToolManager,
    config_path: Path,
    values: set[str] | list[str] | tuple[str, ...],
) -> None:
    ordered = sorted({str(item).strip() for item in values if str(item).strip()})
    ToolManager._update_toml_table(config_path.resolve(), "subagents", {"default_tools": ordered})
    core = manager.config.setdefault("_core", {})
    if isinstance(core, dict):
        subagents = core.setdefault("subagents", {})
        if isinstance(subagents, dict):
            subagents["default_tools"] = ordered


def _worker_tool_options(manager: ToolManager) -> list[tuple[LocalPackage, str, tuple[str, ...]]]:
    result: list[tuple[LocalPackage, str, tuple[str, ...]]] = []
    for item in _local_packages(manager):
        if item.manifest.kind == "module":
            continue
        state, callables = _worker_tool_state(manager, item)
        result.append((item, state, callables))
    return result


def _manager(config: OrionConfig) -> ToolManager:
    return ToolManager(
        config.path(config.tools.directory),
        state_path=config.path(config.tools.state_path),
        root_dir=config.base_dir,
        bundled_dir=Path(__file__).resolve().with_name("tool_packages"),
        config={
            "enabled": config.tools.enabled,
            "disabled": config.tools.disabled,
            **config.tools.settings,
            "_core": {
                "tasks": dict(vars(config.tasks)),
                "scheduler": dict(vars(config.scheduler)),
                "subagents": dict(vars(config.subagents)),
                "teams": dict(vars(config.teams)),
            },
        },
    )


def _select(values: list[GithubTool], value: str) -> GithubTool | None:
    try:
        index = int(value) - 1
    except ValueError:
        return next((item for item in values if item.manifest.id == value), None)
    return values[index] if 0 <= index < len(values) else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GÃ©rer les capabilities Orion locales et distantes")
    parser.add_argument("--config", type=Path, default=Path("orion.toml"))
    parser.add_argument("--repo", help="DÃ©pÃ´t public au format owner/repository")
    parser.add_argument("--ref", help="Branche ou tag GitHub")
    parser.add_argument("--search", help="Filtre non interactif")
    parser.add_argument("--install", metavar="ID", help="Installer directement un identifiant")
    parser.add_argument("--list", action="store_true", help="Lister les modules/tools disponibles localement")
    parser.add_argument(
        "--enable",
        nargs="?",
        const="__installed__",
        metavar="ID",
        help="Activer un module/tool local. Avec --install sans ID, active le package installÃ©.",
    )
    parser.add_argument("--disable", metavar="ID", help="DÃ©sactiver explicitement un module/tool local")
    parser.add_argument("--configure", metavar="ID", help="Configurer un module/tool local")
    parser.add_argument("--no-config", action="store_true", help="Ne pas demander les paramÃ¨tres du tool")
    return parser


def _validate_action_args(args: argparse.Namespace) -> str | None:
    """Reject ambiguous one-shot operations before any config mutation."""

    actions: list[str] = []
    if args.list:
        actions.append("--list")
    if args.disable:
        actions.append("--disable")
    if args.configure:
        actions.append("--configure")
    if args.enable and args.enable != "__installed__":
        actions.append("--enable")
    if args.install:
        actions.append("--install")
    if len(actions) <= 1:
        return None
    # --install may combine only with the flag form ``--enable`` (no ID),
    # represented by __installed__. Explicit --enable ID is already an action.
    return "Actions incompatibles : " + ", ".join(actions)


def _terminal_is_interactive() -> bool:
    try:
        return bool(sys.stdin.isatty()) and bool(sys.stdout.isatty())
    except (AttributeError, OSError):
        return False


def _local_values(manager: ToolManager, query: str = "") -> list[tuple[ToolManifest, Path]]:
    needle = query.strip().lower()
    values = manager.available()
    if not needle:
        return values
    return [
        item
        for item in values
        if needle in item[0].id.lower()
        or needle in item[0].name.lower()
        or needle in item[0].description.lower()
    ]


def _local_packages(manager: ToolManager, query: str = "") -> list[LocalPackage]:
    installed_ids = {manifest.id for manifest, _path in manager.installed()}
    return [
        LocalPackage(
            manifest=manifest,
            path=path,
            source="installed" if manifest.id in installed_ids else "bundled",
        )
        for manifest, path in _local_values(manager, query)
    ]


def _activation_sets(manager: ToolManager) -> tuple[set[str], set[str]]:
    return (
        {str(item) for item in manager.config.get("enabled", [])},
        {str(item) for item in manager.config.get("disabled", [])},
    )


def _resolve_local(manager: ToolManager, selector: str) -> LocalPackage | None:
    values = _local_packages(manager)
    raw = str(selector).strip()
    try:
        index = int(raw) - 1
    except ValueError:
        return next((item for item in values if item.manifest.id == raw), None)
    return values[index] if 0 <= index < len(values) else None


def _resolve_remote(values: list[GithubTool], selector: str) -> GithubTool | None:
    raw = str(selector).strip()
    if raw.lower().startswith("r"):
        raw = raw[1:]
    return _select(values, raw)


def _show_local(manager: ToolManager, ui: ToolboxUI, query: str = "") -> None:
    ui.packages(
        _local_values(manager, query),
        enabled={str(item) for item in manager.config.get("enabled", [])},
        disabled={str(item) for item in manager.config.get("disabled", [])},
    )


def _render_dashboard(
    manager: ToolManager,
    ui: ToolboxUI,
    *,
    repository: str | None,
    ref: str,
    remote: list[GithubTool] | None,
) -> None:
    enabled, disabled = _activation_sets(manager)
    packages = _local_packages(manager)
    worker_access: dict[str, str] = {}
    safe_active: set[str] = set()
    privileged_active: set[str] = set()
    allowed = _subagent_allowed_tools(manager)
    for item in packages:
        if item.manifest.kind == "module":
            continue
        state, callables = _worker_tool_state(manager, item)
        worker_access[item.manifest.id] = _worker_access_label(state)
        if item.manifest.id in enabled and item.manifest.id not in disabled:
            safe_active.update(_manifest_safe_worker_callables(item.manifest))
            privileged_active.update(
                name
                for name, classification in item.manifest.policy.tool_classifications
                if classification is ToolClassification.PRIVILEGED
            )
    subagents_enabled = "orion.subagents" in enabled and "orion.subagents" not in disabled
    if subagents_enabled:
        worker_summary = (
            f"subagents ON  |  {len(allowed & safe_active)}/{len(safe_active)} "
            "active safe tools allowed"
            f"  |  {len(allowed & privileged_active)}/{len(privileged_active)} "
            "privileged allowed (approval/call)"
        )
    else:
        worker_summary = "subagents OFF  |  worker tool permissions inactive"
    ui.dashboard(
        packages,
        enabled=enabled,
        disabled=disabled,
        repository=repository,
        ref=ref,
        remote=remote,
        worker_access=worker_access,
        worker_summary=worker_summary,
    )


def _configure_local(
    manager: ToolManager,
    config: OrionConfig,
    tool_id: str,
    ui: ToolboxUI,
    *,
    input_fn: Callable[[str], str] = input,
) -> bool:
    manifest = manager.find(tool_id)
    if manifest is None:
        raise ToolPackageError(f"Tool/module introuvable : {tool_id}")
    if not manifest.configuration.get("fields", []):
        ui.info(f"{manifest.name} ne dÃ©clare aucun paramÃ¨tre configurable.")
        return False
    _configure(manager, config, manifest, ui, input_fn=input_fn)
    return True


def _configure(
    manager: ToolManager,
    config: OrionConfig,
    manifest: Any,
    ui: ToolboxUI,
    *,
    enabled: bool = True,
    input_fn: Callable[[str], str] = input,
) -> None:
    fields = manifest.configuration.get("fields", [])
    if not enabled or not fields:
        return
    ui.info(f"Configuration de {manifest.name} (EntrÃ©e conserve la valeur actuelle)")
    changed = manager.configure(
        manifest,
        config_path=config.config_path or Path("orion.toml"),
        env_path=config.base_dir / ".env",
        input_fn=input_fn,
    )
    if changed:
        ui.success("ParamÃ¨tres enregistrÃ©s dans orion.toml et/ou .env")


def _install_remote_interactive(
    selected: GithubTool,
    *,
    catalog: GithubToolCatalog,
    manager: ToolManager,
    config: OrionConfig,
    ui: ToolboxUI,
    input_fn: Callable[[str], str],
) -> None:
    installed_ids = {manifest.id for manifest, _ in manager.installed()}
    force = selected.manifest.id in installed_ids
    verb = "Mettre Ã  jour" if force else "Installer"
    answer = input_fn(f"  {verb} {selected.manifest.name} ? [Y/n] ").strip().lower()
    if answer not in {"", "y", "yes", "o", "oui"}:
        ui.info("AnnulÃ©.")
        return
    manifest = catalog.install(selected, manager, force=force)
    _configure(manager, config, manifest, ui, input_fn=input_fn)
    already_enabled = manifest.id in manager.config.get("enabled", [])
    if already_enabled:
        ui.success(f"{manifest.id} {manifest.version} installÃ© et dÃ©jÃ  activÃ©.")
        return
    enable_answer = input_fn(
        f"  Activer {manifest.name} au prochain dÃ©marrage ? [Y/n] "
    ).strip().lower()
    if enable_answer in {"", "y", "yes", "o", "oui"}:
        manager.set_package_enabled(
            manifest.id,
            True,
            config_path=config.config_path or Path("orion.toml"),
        )
        ui.success(f"{manifest.id} {manifest.version} installÃ© et activÃ©.")
    else:
        ui.success(f"{manifest.id} {manifest.version} installÃ©.")
        ui.info("Le tool reste dÃ©sactivÃ© jusqu'Ã  une activation explicite.")


def _toggle_worker_tool(
    manager: ToolManager,
    item: LocalPackage,
    *,
    config_path: Path,
    ui: ToolboxUI,
) -> bool:
    state, callables = _worker_tool_state(manager, item)
    current = _subagent_allowed_tools(manager)
    if state == "unmapped":
        ui.info(
            f"{item.manifest.name} ne dÃ©clare pas ses callables dans le manifeste ; "
            "la Toolbox refuse de l'autoriser implicitement."
        )
        return False
    if state == "inactive":
        ui.info(f"Active d'abord {item.manifest.name} pour Orion, puis autorise-le aux workers.")
        return False
    if state == "configured_off":
        current.difference_update(callables)
        _set_subagent_allowed_tools(manager, config_path, current)
        ui.success(f"{item.manifest.name} retirÃ© du plafond des workers.")
        return True
    if state in {"allowed", "approval_allowed"}:
        current.difference_update(callables)
        _set_subagent_allowed_tools(manager, config_path, current)
        ui.success(f"{item.manifest.name} bloquÃ© pour les workers.")
        return True

    current.update(callables)
    _set_subagent_allowed_tools(manager, config_path, current)
    ui.success(f"{item.manifest.name} autorisÃ© pour les workers.")
    return True


def _worker_tools_loop(
    *,
    manager: ToolManager,
    config_path: Path,
    ui: ToolboxUI,
    input_fn: Callable[[str], str],
) -> None:
    while True:
        options = _worker_tool_options(manager)
        enabled, disabled = _activation_sets(manager)
        subagents_enabled = (
            "orion.subagents" in enabled and "orion.subagents" not in disabled
        )
        ui.worker_tools(options, subagents_enabled=subagents_enabled)
        try:
            command = input_fn("  workers > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return
        if command in {"", "b", "back", "q"}:
            return
        if command in {"n", "none", "clear"}:
            _set_subagent_allowed_tools(manager, config_path, set())
            ui.success("Tous les tools sont maintenant bloquÃ©s pour les workers.")
            continue
        if command in {"a", "all"}:
            allowed: set[str] = set()
            for item, state, _callables in options:
                if state in {"allowed", "blocked", "partial"}:
                    allowed.update(_manifest_safe_worker_callables(item.manifest))
            _set_subagent_allowed_tools(manager, config_path, allowed)
            ui.success("Tous les tools actifs et non privilÃ©giÃ©s sont autorisÃ©s aux workers.")
            continue
        try:
            index = int(command) - 1
        except ValueError:
            ui.info("Commande inconnue. Utilise un numÃ©ro, a, n ou b.")
            continue
        if not 0 <= index < len(options):
            ui.info("NumÃ©ro de tool introuvable.")
            continue
        _toggle_worker_tool(
            manager,
            options[index][0],
            config_path=config_path,
            ui=ui,
        )


def _local_detail_loop(
    item: LocalPackage,
    *,
    manager: ToolManager,
    config: OrionConfig,
    config_path: Path,
    ui: ToolboxUI,
    input_fn: Callable[[str], str],
) -> None:
    while True:
        enabled, disabled = _activation_sets(manager)
        state = ToolboxUI._state(item.manifest, enabled, disabled)
        worker_access: str | None = None
        worker_action: str | None = None
        if item.manifest.kind != "module":
            worker_state, _callables = _worker_tool_state(manager, item)
            worker_access = _worker_access_label(worker_state)
            if worker_state != "unmapped":
                worker_action = "w toggle workers"
        elif item.manifest.id == "orion.subagents":
            worker_action = "w worker tools"
        ui.detail(
            item,
            state,
            worker_access=worker_access,
            worker_action=worker_action,
        )
        try:
            command = input_fn("  detail > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return
        if command in {"", "b", "back", "q"}:
            return
        try:
            if command in {"e", "enable"}:
                manager.set_package_enabled(item.manifest.id, True, config_path=config_path)
                ui.success(f"{item.manifest.id} activÃ©. RedÃ©marre Orion pour appliquer.")
                if item.manifest.id == "orion.subagents":
                    ui.info(
                        "Les sous-agents n'hÃ©ritent d'aucun tool automatiquement. "
                        "Utilise w pour choisir leur plafond de capabilities."
                    )
                elif item.manifest.kind != "module":
                    ui.info(
                        "Ce tool est activÃ© pour Orion principal. L'accÃ¨s des workers reste sÃ©parÃ© ; "
                        "utilise w pour l'autoriser aux sous-agents."
                    )
            elif command in {"d", "disable"}:
                manager.set_package_enabled(item.manifest.id, False, config_path=config_path)
                ui.success(f"{item.manifest.id} dÃ©sactivÃ©. RedÃ©marre Orion pour appliquer.")
            elif command in {"c", "config", "configure"}:
                _configure_local(
                    manager,
                    config,
                    item.manifest.id,
                    ui,
                    input_fn=input_fn,
                )
            elif command in {"w", "workers", "worker"}:
                if item.manifest.id == "orion.subagents":
                    _worker_tools_loop(
                        manager=manager,
                        config_path=config_path,
                        ui=ui,
                        input_fn=input_fn,
                    )
                elif item.manifest.kind != "module":
                    _toggle_worker_tool(
                        manager,
                        item,
                        config_path=config_path,
                        ui=ui,
                    )
                else:
                    ui.info("Ce module ne gÃ¨re pas d'accÃ¨s tool pour les workers.")
            else:
                ui.info("Action inconnue. Utilise e, d, c, w ou b.")
        except (ToolPackageError, ValueError) as exc:
            ui.error(str(exc))


def _remote_detail_loop(
    selected: GithubTool,
    *,
    catalog: GithubToolCatalog,
    manager: ToolManager,
    config: OrionConfig,
    ui: ToolboxUI,
    input_fn: Callable[[str], str],
) -> None:
    installed = selected.manifest.id in {manifest.id for manifest, _ in manager.installed()}
    ui.remote_detail(selected, installed=installed)
    try:
        command = input_fn("  remote > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if command in {"i", "install", "update"}:
        _install_remote_interactive(
            selected,
            catalog=catalog,
            manager=manager,
            config=config,
            ui=ui,
            input_fn=input_fn,
        )


def _interactive_loop(
    *,
    manager: ToolManager,
    config: OrionConfig,
    config_path: Path,
    ui: ToolboxUI,
    catalog: GithubToolCatalog | None,
    repository: str | None,
    ref: str,
    initial_remote: list[GithubTool] | None = None,
    input_fn: Callable[[str], str] = input,
) -> int:
    remote_values = initial_remote
    _render_dashboard(
        manager,
        ui,
        repository=repository,
        ref=ref,
        remote=remote_values,
    )
    while True:
        try:
            command = input_fn("\n  toolbox > ").strip()
        except (EOFError, KeyboardInterrupt):
            ui._print()
            return 0
        lowered = command.lower()
        if lowered in {"q", "quit", "exit"}:
            return 0
        if lowered in {"h", "help", "?"}:
            ui.help()
            continue
        if lowered in {"dashboard", "home", "d"}:
            _render_dashboard(
                manager, ui, repository=repository, ref=ref, remote=remote_values
            )
            continue
        if lowered in {"w", "workers", "worker"}:
            _worker_tools_loop(
                manager=manager,
                config_path=config_path,
                ui=ui,
                input_fn=input_fn,
            )
            _render_dashboard(
                manager, ui, repository=repository, ref=ref, remote=remote_values
            )
            continue
        if lowered in {"r", "remote"}:
            if catalog is None:
                ui.info("Aucun dÃ©pÃ´t distant configurÃ©. Relance avec --repo owner/repository pour en ajouter un.")
                continue
            catalog._tree = None
            catalog._tools = None
            try:
                remote_values = catalog.search()
                _render_dashboard(
                    manager, ui, repository=repository, ref=ref, remote=remote_values
                )
            except ToolPackageError as exc:
                ui.error(str(exc))
            continue
        if command.startswith("/"):
            if catalog is None:
                query = command[1:]
                enabled, disabled = _activation_sets(manager)
                filtered = _local_packages(manager, query)
                ui.dashboard(
                    filtered,
                    enabled=enabled,
                    disabled=disabled,
                    repository=repository,
                    ref=ref,
                    remote=None,
                )
                continue
            try:
                remote_values = catalog.search(command[1:])
                _render_dashboard(
                    manager, ui, repository=repository, ref=ref, remote=remote_values
                )
            except ToolPackageError as exc:
                ui.error(str(exc))
            continue
        if lowered.startswith("r") and lowered[1:].isdigit():
            selected = _resolve_remote(remote_values or [], lowered)
            if selected is None or catalog is None:
                ui.info("RÃ©sultat distant introuvable. Charge d'abord le catalogue avec r.")
                continue
            try:
                _remote_detail_loop(
                    selected,
                    catalog=catalog,
                    manager=manager,
                    config=config,
                    ui=ui,
                    input_fn=input_fn,
                )
            except (ToolPackageError, ValueError) as exc:
                ui.error(str(exc))
            continue

        parts = command.split(None, 1)
        action = parts[0].lower() if parts else ""
        if action in {"enable", "disable", "config", "configure"} and len(parts) == 2:
            item = _resolve_local(manager, parts[1])
            if item is None:
                ui.info("Module/tool local introuvable.")
                continue
            try:
                if action == "enable":
                    manager.set_package_enabled(item.manifest.id, True, config_path=config_path)
                    ui.success(f"{item.manifest.id} activÃ©. RedÃ©marre Orion pour appliquer.")
                    if item.manifest.id == "orion.subagents":
                        ui.info(
                            "Aucun tool n'est transmis automatiquement aux workers. "
                            "Utilise w pour choisir leur plafond."
                        )
                    elif item.manifest.kind != "module":
                        ui.info(
                            "ActivÃ© pour Orion principal. L'accÃ¨s des workers reste sÃ©parÃ© ; "
                            "utilise w pour l'autoriser aux sous-agents."
                        )
                elif action == "disable":
                    manager.set_package_enabled(item.manifest.id, False, config_path=config_path)
                    ui.success(f"{item.manifest.id} dÃ©sactivÃ©. RedÃ©marre Orion pour appliquer.")
                else:
                    _configure_local(
                        manager,
                        config,
                        item.manifest.id,
                        ui,
                        input_fn=input_fn,
                    )
            except (ToolPackageError, ValueError) as exc:
                ui.error(str(exc))
            _render_dashboard(
                manager, ui, repository=repository, ref=ref, remote=remote_values
            )
            continue
        if lowered == "c":
            try:
                selector = input_fn("  Package Ã  configurer (# ou id) : ").strip()
            except (EOFError, KeyboardInterrupt):
                continue
            item = _resolve_local(manager, selector)
            if item is None:
                ui.info("Module/tool local introuvable.")
                continue
            try:
                _configure_local(
                    manager,
                    config,
                    item.manifest.id,
                    ui,
                    input_fn=input_fn,
                )
            except (ToolPackageError, ValueError) as exc:
                ui.error(str(exc))
            continue
        if command.isdigit():
            item = _resolve_local(manager, command)
            if item is None:
                ui.info("NumÃ©ro local introuvable.")
                continue
            _local_detail_loop(
                item,
                manager=manager,
                config=config,
                config_path=config_path,
                ui=ui,
                input_fn=input_fn,
            )
            _render_dashboard(
                manager, ui, repository=repository, ref=ref, remote=remote_values
            )
            continue
        ui.info("Commande inconnue. Utilise h pour afficher l'aide.")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ui = ToolboxUI()
    argument_error = _validate_action_args(args)
    if argument_error:
        ui.error(argument_error)
        return 2
    try:
        config = OrionConfig.from_file(args.config)
        manager = _manager(config)
    except (OSError, ValueError, ToolPackageError) as exc:
        ui.error(f"Configuration invalide : {exc}")
        return 1
    config_path = config.config_path or args.config
    github_settings: dict[str, Any] = dict(config.tools.settings.get("github", {}))
    repository = args.repo or str(github_settings.get("repo", ""))
    ref = args.ref or str(github_settings.get("ref", "main"))

    try:
        if args.list:
            ui.title(repository or None, ref)
            _show_local(manager, ui, args.search or "")
            return 0
        if args.disable:
            manager.set_package_enabled(args.disable, False, config_path=config_path)
            ui.success(f"{args.disable} dÃ©sactivÃ©. RedÃ©marre Orion pour appliquer.")
            return 0
        if args.configure:
            _configure_local(manager, config, args.configure, ui)
            return 0
        if args.enable and args.enable != "__installed__":
            if args.install:
                raise ToolPackageError("Utilise --enable sans ID avec --install, ou active le module sÃ©parÃ©ment.")
            manager.set_package_enabled(args.enable, True, config_path=config_path)
            ui.success(f"{args.enable} activÃ©. RedÃ©marre Orion pour appliquer.")
            manifest = manager.find(args.enable)
            if args.enable == "orion.subagents":
                ui.info(
                    "Les workers n'hÃ©ritent d'aucun tool automatiquement. "
                    "Lance la Toolbox en mode interactif puis utilise w pour choisir leur plafond."
                )
            elif manifest is not None and manifest.kind != "module":
                ui.info(
                    "Activation Orion principal uniquement. L'accÃ¨s des sous-agents reste sÃ©parÃ© "
                    "et se rÃ¨gle avec la commande interactive w."
                )
            return 0
        if args.enable == "__installed__" and not args.install:
            raise ToolPackageError("--enable sans identifiant doit Ãªtre utilisÃ© avec --install.")
    except (ToolPackageError, ValueError) as exc:
        ui.error(str(exc))
        return 1

    no_action = not any(
        (
            args.list,
            args.disable,
            args.configure,
            args.enable,
            args.install,
            args.search is not None,
        )
    )
    if no_action and not _terminal_is_interactive():
        _render_dashboard(
            manager,
            ui,
            repository=repository or None,
            ref=ref,
            remote=None,
        )
        ui.info("Mode interactif indisponible sans TTY. Utilise --list/--enable/--disable/--configure ou relance dans un terminal.")
        return 0

    catalog: GithubToolCatalog | None = None
    if repository:
        catalog = GithubToolCatalog(
            repository,
            ref=ref,
            timeout=int(github_settings.get("timeout", 20)),
        )
    if args.install:
        ui.title(repository or None, ref)

    try:
        # Keep local module management independent from GitHub. The remote
        # catalog is fetched only for an explicit remote search/install/reload.
        values = (
            catalog.search(args.search or "")
            if catalog is not None and (args.install or args.search is not None)
            else []
        )
        if args.install:
            if catalog is None:
                raise ToolPackageError(
                    "Aucun dÃ©pÃ´t distant configurÃ©. Utilise --repo owner/repository pour installer un package distant."
                )
            selected = next((item for item in values if item.manifest.id == args.install), None)
            if selected is None:
                raise ToolPackageError(f"Tool introuvable : {args.install}")
            existing = {manifest.id for manifest, _ in manager.installed()}
            force = selected.manifest.id in existing
            manifest = catalog.install(selected, manager, force=force)
            _configure(manager, config, manifest, ui, enabled=not args.no_config)
            if args.enable == "__installed__":
                manager.set_package_enabled(
                    manifest.id,
                    True,
                    config_path=config.config_path or args.config,
                )
                ui.success(f"{manifest.id} {manifest.version} installÃ© et activÃ©.")
            else:
                ui.success(f"{manifest.id} {manifest.version} installÃ©.")
                ui.info(
                    f"Chargement dÃ©sactivÃ© ; relance avec --enable ou utilise `orion-tools enable {manifest.id}`."
                )
            return 0
        if args.search is not None and not _terminal_is_interactive():
            _render_dashboard(
                manager,
                ui,
                repository=repository or None,
                ref=ref,
                remote=values,
            )
            return 0
    except ToolPackageError as exc:
        ui.error(str(exc))
        return 1

    return _interactive_loop(
        manager=manager,
        config=config,
        config_path=Path(config_path),
        ui=ui,
        catalog=catalog,
        repository=repository or None,
        ref=ref,
        initial_remote=values if args.search is not None else None,
        input_fn=input,
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ToolPackageError as exc:
        raise SystemExit(f"[orion-toolbox:error] {exc}") from exc
