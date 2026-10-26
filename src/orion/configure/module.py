"""Configure: `[modules.configure]` in orion.toml (on by default).

The owner is on his phone, not on the server. « Active le mail », « mets tes points à 9 h et
18 h », « ajoute Angèle, elle peut savoir quand je suis libre »: Orion edits its own orion.toml
(D72, D92):

- `configure_show`: its settings, as they are now;
- `configure_change`: a few keys, each by its dotted path (`set`, or `add` / `remove` an item
  of a list). Orion asks first, showing the exact change (an external effect: the guardian puts
  it to the owner). It may change its own settings alone once the owner let it
  (`configure_change` is autonomable), never the owner's (`orion.configure.paths`: the
  guardian, the channels, who may write to Orion, the budget);
- the new file is checked whole (the config model, then which modules would load) before the
  owner is asked, and again before it is written; comments and layout are kept (tomlkit); the
  previous file is kept beside it;
- when Orion runs split (D28), the guardian process writes the file, after checking the
  owner's yes itself: the core cannot change what protects him, even compromised;
- then Orion restarts itself once the turn is answered (and the guardian too, when it wrote).

A secret is never a value in the file, nor sent by message: at most the name of the
environment variable that holds it, set on the server by `orion setup`.

    config.changed  (the changes, why, the file's digest before and after)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import tomllib
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import structlog
import tomlkit
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from tomlkit.items import Table

from orion.configure.paths import (
    FORBIDDEN_KEYS,
    ORION_SECTIONS,
    OWNER_PATHS,
    OWNER_SECTIONS,
    ConfigureError,
    check_path,
    tier,
)
from orion.core.events import NewEvent, Provenance, Trust
from orion.core.journal import EventStore
from orion.guardian.client import GuardianClient, GuardianUnavailableError
from orion.modules.base import Daemon, Module, ModuleError, ModuleSettings, Plate
from orion.modules.installer import Installer
from orion.modules.tools import module_tools
from orion.resources import read_resource
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from orion.config import OrionConfig

log = structlog.get_logger(__name__)

__all__ = ["CHANGED", "Change", "Configurator", "ConfigureError", "apply_changes"]

CHANGED = "config.changed"
RESTART_AFTER = 8.0
"""Seconds before restarting: the turn's answer leaves first."""

Restart = Callable[[], Coroutine[Any, Any, None]]


class ConfigureSettings(ModuleSettings):
    enabled: bool = True


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        min_length=3,
        max_length=120,
        description="Dotted key, e.g. 'modules.mail.enabled', 'initiative.quiet_start', "
        "'modules.secretary.contacts'",
    )
    value: Any = Field(
        description="The new value (true, '22:30:00', a list…); null removes the key. With "
        "`add`: the item to add (a contact: {name, telegram_id or number, about, share}); with "
        "`remove`: the item, or a contact's name"
    )
    op: Literal["set", "add", "remove"] = Field(
        default="set", description="Set the key (default), or add / remove an item of a list"
    )

    def described(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_defaults=True)


def _child(table: Any, part: str) -> Any:
    return table[part]


def apply_changes(text: str, changes: list[Change]) -> str:
    """The file with the changes, comments and layout kept. Raises ConfigureError."""
    doc = tomlkit.parse(text)
    for change in changes:
        parts = check_path(change.path)
        tier(change.path, change.value)
        table: Any = doc
        for part in parts[:-1]:
            if part not in table:
                table[part] = tomlkit.table()
            table = _child(table, part)
            if not isinstance(table, (Table, dict)):
                raise ConfigureError(f"{change.path!r}: {part} is not a section")
        key = parts[-1]
        if change.op == "set":
            if change.value is None:
                if key in table:
                    del table[key]
            else:
                table[key] = change.value
            continue
        current = cast(object, table.get(key))  # pyright: ignore[reportUnknownMemberType]
        if current is not None and not isinstance(current, list):
            raise ConfigureError(f"{change.path!r} is not a list: use `set`")
        items: list[Any] = [_plain(item) for item in cast(list[Any], current or [])]
        if change.op == "add":
            if change.value is None:
                raise ConfigureError(f"{change.path!r}: nothing to add")
            items.append(change.value)
        else:
            kept = [item for item in items if not _same_item(item, change.value)]
            if len(kept) == len(items):
                raise ConfigureError(f"{change.path!r}: no {change.value!r} to remove")
            items = kept
        if items and all(isinstance(item, dict) for item in items):
            tables: Any = tomlkit.aot()
            for item in items:
                tables.append(tomlkit.item(item))
            table[key] = tables
        else:
            table[key] = items
    return tomlkit.dumps(doc)


def _plain(item: Any) -> Any:
    unwrapped: Any = item.unwrap() if hasattr(item, "unwrap") else item
    return unwrapped


def _same_item(item: Any, wanted: Any) -> bool:
    """`wanted` is the item itself, or the name of a table in the list (a contact's)."""
    if item == wanted:
        return True
    if isinstance(item, dict) and isinstance(wanted, str):
        named: dict[str, Any] = item  # pyright: ignore[reportUnknownVariableType]
        return str(named.get("name", "")).casefold() == wanted.strip().casefold()
    return False


def validate(text: str, source: Path) -> OrionConfig:
    """The whole new file, as Orion would start with it: the config, then its modules."""
    from orion.config import parse_config
    from orion.modules.registry import load

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigureError(f"the file would not parse: {exc}") from exc
    try:
        config = parse_config(data, source)
        load(config)
    except (ValidationError, ModuleError, ValueError) as exc:
        raise ConfigureError(f"Orion would not start with this: {exc}") from exc
    return config


def rewrite(source: Path, changes: list[Change]) -> tuple[str, str]:
    """The file now, and with the changes, checked whole. Raises ConfigureError."""
    if not source.exists():
        raise ConfigureError("Orion runs without an orion.toml: nothing to change")
    before = source.read_text("utf-8")
    after = apply_changes(before, changes)
    if after != before:
        validate(after, source)
    return before, after


def write(source: Path, before: str, after: str) -> None:
    """Atomically, keeping the previous file beside it. Raises ConfigureError."""
    try:
        if source.read_text("utf-8") != before:
            raise ConfigureError(f"{source} changed meanwhile: read it again and redo the change")
        mode = source.stat().st_mode & 0o777  # the core reads what the guardian writes
        previous = source.with_name(f"{source.name}.previous")
        previous.write_text(before, "utf-8")
        previous.chmod(mode)
        tmp = source.with_name(f".{source.name}.{os.getpid()}.tmp")
        tmp.write_text(after, "utf-8")
        tmp.chmod(mode)
        tmp.replace(source)
    except OSError as exc:
        raise ConfigureError(f"cannot write {source}: {exc}") from exc


def editable_sections(config: OrionConfig) -> dict[str, Any]:
    """The sections that can be changed by message, without what never is; the modules' as
    written in the file."""
    sections = {name for name in (*ORION_SECTIONS, *OWNER_SECTIONS) if name != "modules"}
    data = cast(dict[str, Any], _changeable(config.model_dump(mode="json", include=sections)))
    if config.modules:
        data["modules"] = _changeable(config.modules)
    return data


def editable_view(config: OrionConfig) -> str:
    """Every setting that can be changed by message, and every installed module with whether
    it is on: a module absent from the file can be turned on all the same."""
    from orion.modules.registry import discover

    sections = {name for name in (*ORION_SECTIONS, *OWNER_SECTIONS) if name != "modules"}
    data = _changeable(config.model_dump(mode="json", include=sections))
    modules = {kind.name: kind(config).enabled for kind in discover(config)}
    lines = [
        json.dumps(data, ensure_ascii=False, indent=1),
        "Modules (chacun s'active ou s'éteint par `modules.<nom>.enabled`, même absent du "
        "fichier ; `voice` et `delegation` par `voice.enabled`, `delegation.enabled`) :",
        *(f"- {name} : {'allumé' if on else 'éteint'}" for name, on in modules.items()),
    ]
    if config.modules:
        lines.append("Réglages de modules écrits dans le fichier :")
        lines.append(json.dumps(config.modules, ensure_ascii=False, indent=1))
    lines.append(
        f"Sont à lui ({', '.join(OWNER_SECTIONS)}, les comptes, "
        f"{', '.join(OWNER_PATHS)}) : il dit oui à chaque changement, tu ne le fais jamais "
        "seul. Les secrets et les chemins ne se changent jamais par message (`orion setup`, "
        "sur le serveur)."
    )
    return "\n".join(lines)


def _changeable(value: Any) -> Any:
    """Without what is never changed by message (keys, paths)."""
    if isinstance(value, dict):
        table = cast(dict[str, Any], value)
        return {
            k: _changeable(v)
            for k, v in table.items()
            if k not in FORBIDDEN_KEYS and not k.endswith("_dir")
        }
    return value


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class Configurator:
    def __init__(
        self, journal: EventStore, source: Path | None, guardian: GuardianClient | None = None
    ) -> None:
        self._journal = journal
        self.source = source
        self.guardian = guardian
        """When Orion runs split, the guardian writes the file (D92)."""
        self.restart: Restart | None = None
        self._restarting: asyncio.Task[None] | None = None
        """Kept so the restart is not garbage-collected while it waits."""
        self._lock = asyncio.Lock()

    def precheck(self, changes: list[Change]) -> str | None:
        """What is wrong with these changes, before the owner is asked."""
        if self.source is None:
            return "Not changed: Orion runs without an orion.toml"
        try:
            rewrite(self.source, changes)
        except ConfigureError as exc:
            return f"Not changed: {exc}"
        return None

    async def change(
        self,
        changes: list[Change],
        why: str,
        origin: Provenance,
        grant: str | None = None,
        module: str = "",
        removing: bool = False,
    ) -> str:
        """`module`: the switch of a module he just let install or remove, under that yes."""
        if self.source is None:
            raise ConfigureError("Orion runs without an orion.toml: nothing to change")
        async with self._lock:
            if self.guardian is not None:
                try:
                    done = await self.guardian.configure(
                        [c.described() for c in changes], why, grant, module, removing
                    )
                except GuardianUnavailableError as exc:
                    raise ConfigureError(str(exc)) from exc
                if not done.get("changed"):
                    return "Rien ne change : c'est déjà réglé ainsi."
                digests = (str(done["before"]), str(done["after"]))
            else:
                before, after = await asyncio.to_thread(rewrite, self.source, changes)
                if after == before:
                    return "Rien ne change : c'est déjà réglé ainsi."
                write(self.source, before, after)
                digests = (digest(before), digest(after))
            await self._journal.append(
                [
                    NewEvent(
                        kind=CHANGED,
                        payload={
                            "changes": [c.described() for c in changes],
                            "why": why,
                            "before": digests[0],
                            "after": digests[1],
                        },
                        provenance=origin,
                        trust=Trust.OWNER,
                    )
                ]
            )
        log.info("config.changed", paths=[c.path for c in changes])
        if self.restart is None:
            return "Réglage écrit. Il prendra effet au prochain démarrage d'Orion."
        self._restarting = asyncio.get_running_loop().create_task(self.restart())
        return "Réglage écrit. Je redémarre dans quelques secondes pour l'appliquer."


async def reexec(delay: float = RESTART_AFTER) -> None:
    """Restart in place (same process id under systemd): the new config is read at start."""
    await asyncio.sleep(delay)
    log.info("config.restarting")
    os.execv(sys.executable, [sys.executable, "-m", "orion", *sys.argv[1:]])


class ChangeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    changes: list[Change] = Field(min_length=1, max_length=10)
    why: str = Field(min_length=3, max_length=300, description="What he asked, in his words")


class ShowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SecretInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        pattern=r"^[A-Z][A-Z0-9_]{2,63}$",
        description="The environment variable to set, e.g. MAIL_PERSO_PASSWORD",
    )
    why: str = Field(max_length=200, description="What it is, in a few words, for him")


def secret_tool(guardian: GuardianClient) -> Tool:
    async def ask(args: Any, _ctx: ToolContext) -> ToolResult:
        try:
            await guardian.ask_secret(args.name, args.why)
        except GuardianUnavailableError as exc:
            return ToolResult(f"Not asked: {exc}", is_error=True)
        return ToolResult(
            f"Il a reçu un bouton pour saisir {args.name} dans le formulaire du gardien. Tu ne "
            "verras jamais la valeur : quand le gardien te dit qu'elle est enregistrée, nomme-la "
            f'dans tes réglages ({{"env": "{args.name}"}}).'
        )

    return Tool(
        name="secret_ask",
        description="Ask the owner for a secret (a mailbox password, an API key) through the "
        "guardian's own form on his phone: the value never goes through the conversation. "
        "Never ask him to send a secret in a message.",
        input_model=SecretInput,
        handler=ask,
        effect=Effect.INTERNAL,
        reversibility=Reversibility.REVERSIBLE,
    )


def configure_tools(configurator: Configurator, config: OrionConfig) -> list[Tool]:
    async def show(_args: Any, _ctx: ToolContext) -> ToolResult:
        return ToolResult(
            "Tes réglages, tels qu'ils sont :\n"
            + editable_view(config)
            + "\n\nComment les changer :\n"
            + read_resource("configure-howto.md").strip()
        )

    async def change(args: Any, ctx: ToolContext) -> ToolResult:
        origin = Provenance(
            source=f"thread:{ctx.thread_id}",
            actor="orion:core",
            derived_from=(ctx.origin_event_id,) if ctx.origin_event_id else (),
        )
        try:
            said = await configurator.change(list(args.changes), args.why, origin, ctx.grant)
        except ConfigureError as exc:
            return ToolResult(f"Not changed: {exc}", is_error=True)
        return ToolResult(said)

    def check(args: BaseModel) -> str | None:
        changes: list[Change] = list(getattr(args, "changes", []))
        return configurator.precheck(changes)

    return [
        Tool(
            name="configure_show",
            description="Your own settings as they are now, and every module with whether it "
            "is on.",
            input_model=ShowInput,
            handler=show,
            effect=Effect.NONE,
            reversibility=Reversibility.REVERSIBLE,
            concurrency_safe=True,
            accepts_untrusted_context=True,
        ),
        Tool(
            name="configure_change",
            description="Change your own settings (orion.toml) when the owner asks: turn a "
            "module on or off, a time, a model, who may write to you "
            "(`modules.secretary.contacts`, op add / remove), the guardian's limits. Checked "
            "whole before he is asked; then you restart. Never a secret: those are set on the "
            "server with `orion setup`.",
            input_model=ChangeInput,
            handler=change,
            effect=Effect.EXTERNAL,
            reversibility=Reversibility.REVERSIBLE,
            check=check,
        ),
    ]


class ConfigureModule(Module):
    name = "configure"
    instructions = "configure.md"
    settings_model = ConfigureSettings

    def __init__(self, config: OrionConfig) -> None:
        super().__init__(config)
        self.configurator: Configurator | None = None

    async def assemble(self, plate: Plate) -> list[Tool]:
        # Two tools, not one with actions: the owner's yes is checked against
        # `configure_change` by name, and autonomy is granted to it by name (D92).
        self.configurator = Configurator(plate.journal, self.config.source, plate.guardian)
        tools = configure_tools(self.configurator, self.config)
        tools += module_tools(Installer(self.config), self.configurator)
        if plate.guardian is not None and self.config.guardian.secrets_url:
            tools.append(secret_tool(plate.guardian))
        return tools

    async def serve(self, daemon: Daemon) -> None:
        if self.configurator is not None:
            self.configurator.restart = reexec
