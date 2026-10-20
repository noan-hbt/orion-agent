"""Configure: `[modules.configure]` in orion.toml (on by default).

The owner is on his phone, not on the server. « Active le mail », « mets tes points à 9 h et
18 h », « passe le modèle fort sur gpt-6.1-sol »: Orion edits its own orion.toml (D72):

- `configure` show: the settings it may change, as they are now;
- `configure` change: a few keys, each by its dotted path. Orion asks first, showing the exact
  change (an external effect: the guardian puts it to the owner), unless the owner let it do
  this alone (`configure_change` is autonomable: « change tes réglages sans me demander »);
- the new file is checked whole (the config model, then which modules would load) before it is
  written; comments and layout are kept (tomlkit); the previous file is kept beside it;
- then Orion restarts itself, once the turn is answered.

What it never touches: the guardian's section (what it may do alone, its limits), federation,
the iMessage line (who it obeys), providers' keys, paths, logging. A secret is never a value in
the file: at most the name of the environment variable that holds it, set on the server.

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
from typing import TYPE_CHECKING, Any

import structlog
import tomlkit
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from tomlkit.items import Table

from orion.core.events import NewEvent, Provenance, Trust
from orion.core.journal import EventStore
from orion.modules.base import Daemon, Module, ModuleError, ModuleSettings, Plate
from orion.tools.actions import grouped
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from orion.config import OrionConfig

log = structlog.get_logger(__name__)

CHANGED = "config.changed"
EDITABLE = ("agent", "initiative", "adaptation", "memory", "modules", "voice", "delegation")
"""Top-level sections Orion may change. Everything else is the owner's, on the server."""
FORBIDDEN_KEYS = ("allow_bash", "binary", "api_key", "base_url", "signing_key", "key")
"""Keys never changed by message, wherever they are (a key, what runs on the server)."""
FORBIDDEN_PATHS = ("modules.secretary.contacts",)
"""Who may write to Orion is the owner's to say, on the server."""
RESTART_AFTER = 8.0
"""Seconds before restarting: the turn's answer leaves first."""

Restart = Callable[[], Coroutine[Any, Any, None]]


class ConfigureSettings(ModuleSettings):
    enabled: bool = True


class ConfigureError(ValueError):
    pass


def check_path(path: str) -> list[str]:
    parts = path.strip().split(".")
    if not all(parts) or parts[0] not in EDITABLE:
        raise ConfigureError(
            f"{path!r}: only {', '.join(EDITABLE)} can be changed by message; the rest is "
            "changed on the server"
        )
    if any(p in FORBIDDEN_KEYS for p in parts) or path.strip().startswith(FORBIDDEN_PATHS):
        raise ConfigureError(f"{path!r}: never changed by message")
    return parts


def _child(table: Any, part: str) -> Any:
    return table[part]


def apply_changes(text: str, changes: list[tuple[str, Any]]) -> str:
    """The file with the changes, comments and layout kept. Raises ConfigureError."""
    doc = tomlkit.parse(text)
    for path, value in changes:
        parts = check_path(path)
        table: Any = doc
        for part in parts[:-1]:
            if part not in table:
                table[part] = tomlkit.table()
            table = _child(table, part)
            if not isinstance(table, (Table, dict)):
                raise ConfigureError(f"{path!r}: {part} is not a section")
        if value is None:
            if parts[-1] in table:
                del table[parts[-1]]
        else:
            table[parts[-1]] = value
    return tomlkit.dumps(doc)


def validate(text: str, source: Path) -> OrionConfig:
    """The whole new file, as Orion would start with it: the config, then its modules."""
    from orion.config import load_config
    from orion.modules.registry import load

    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigureError(f"the file would not parse: {exc}") from exc
    probe = source.with_name(f".{source.name}.check")
    probe.write_text(text, "utf-8")
    try:
        config = load_config(probe)
        load(config)
    except (ValidationError, ModuleError, ValueError) as exc:
        raise ConfigureError(f"Orion would not start with this: {exc}") from exc
    finally:
        probe.unlink(missing_ok=True)
    return config


def editable_view(config: OrionConfig) -> str:
    data = config.model_dump(mode="json", include=set(EDITABLE))
    return json.dumps(data, ensure_ascii=False, indent=1)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class Configurator:
    def __init__(self, journal: EventStore, source: Path | None) -> None:
        self._journal = journal
        self.source = source
        self.restart: Restart | None = None
        self._lock = asyncio.Lock()

    async def change(self, changes: list[tuple[str, Any]], why: str, origin: Provenance) -> str:
        if self.source is None or not self.source.exists():
            raise ConfigureError("Orion runs without an orion.toml: nothing to change")
        async with self._lock:
            before = self.source.read_text("utf-8")
            after = apply_changes(before, changes)
            if after == before:
                return "Rien ne change : c'est déjà réglé ainsi."
            await asyncio.to_thread(validate, after, self.source)
            try:
                self.source.with_name(f"{self.source.name}.previous").write_text(before, "utf-8")
                tmp = self.source.with_name(f".{self.source.name}.tmp")
                tmp.write_text(after, "utf-8")
                tmp.replace(self.source)
            except OSError as exc:
                raise ConfigureError(f"cannot write {self.source}: {exc}") from exc
            await self._journal.append(
                [
                    NewEvent(
                        kind=CHANGED,
                        payload={
                            "changes": [{"path": p, "value": v} for p, v in changes],
                            "why": why,
                            "before": _digest(before),
                            "after": _digest(after),
                        },
                        provenance=origin,
                        trust=Trust.OWNER,
                    )
                ]
            )
        log.info("config.changed", paths=[p for p, _ in changes])
        if self.restart is None:
            return "Réglage écrit. Il prendra effet au prochain démarrage d'Orion."
        asyncio.get_running_loop().create_task(self.restart())
        return "Réglage écrit. Je redémarre dans quelques secondes pour l'appliquer."


async def reexec(delay: float = RESTART_AFTER) -> None:
    """Restart in place (same process id under systemd): the new config is read at start."""
    await asyncio.sleep(delay)
    log.info("config.restarting")
    os.execv(sys.executable, [sys.executable, "-m", "orion", *sys.argv[1:]])


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        min_length=3,
        max_length=120,
        description="Dotted key, e.g. 'modules.mail.enabled', 'initiative.quiet_start', "
        "'agent.strong_model'",
    )
    value: Any = Field(description="The new value (true, '22:30:00', a list…); null removes it")


class ChangeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    changes: list[Change] = Field(min_length=1, max_length=10)
    why: str = Field(min_length=3, max_length=300, description="What he asked, in his words")


class ShowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def configure_tools(configurator: Configurator, config: OrionConfig) -> list[Tool]:
    async def show(_args: Any, _ctx: ToolContext) -> ToolResult:
        return ToolResult(
            "Ce que tu peux changer par message (le reste se règle sur le serveur) :\n"
            + editable_view(config)
        )

    async def change(args: Any, ctx: ToolContext) -> ToolResult:
        origin = Provenance(
            source=f"thread:{ctx.thread_id}",
            actor="orion:core",
            derived_from=(ctx.origin_event_id,) if ctx.origin_event_id else (),
        )
        try:
            said = await configurator.change(
                [(c.path, c.value) for c in args.changes], args.why, origin
            )
        except ConfigureError as exc:
            return ToolResult(f"Not changed: {exc}", is_error=True)
        return ToolResult(said)

    return [
        Tool(
            name="configure_show",
            description="Your own settings that can be changed by message, as they are now.",
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
            "module on or off, a time, a model. Checked whole before it is written; then you "
            "restart. Never the guardian, the line, keys.",
            input_model=ChangeInput,
            handler=change,
            effect=Effect.EXTERNAL,
            reversibility=Reversibility.REVERSIBLE,
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
        self.configurator = Configurator(plate.journal, self.config.source)
        return [
            grouped(
                "configure",
                "Your own settings: see them, and change them when the owner asks.",
                configure_tools(self.configurator, self.config),
                {"configure_show": "show", "configure_change": "change"},
            )
        ]

    async def serve(self, daemon: Daemon) -> None:
        if self.configurator is not None:
            self.configurator.restart = reexec
