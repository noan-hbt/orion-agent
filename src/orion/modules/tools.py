"""Orion installs and removes modules from the catalog, with the owner's yes (D105).

`modules` lists what is installed and what the catalog offers. `module_install` and
`module_remove` change Orion's own code: the owner approves each one (never alone, never from a
conversation holding outside content), the guardian writes the module's switch in orion.toml
under that same yes, and Orion restarts to load it or to let it go.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from orion.configure.paths import ConfigureError
from orion.core.events import Provenance
from orion.modules.installer import NAME, Installer, InstallError
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from orion.configure.module import Configurator


class ModulesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModuleNameInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NAME.pattern, description="The module's name, as `modules` lists it")


def module_tools(installer: Installer, configurator: Configurator) -> list[Tool]:
    def is_on(name: str) -> bool:
        """From the file as it is now: a switch just written counts before the restart."""
        from orion.config import load_config

        source = installer.config.source
        config = load_config(source) if source is not None and source.exists() else installer.config
        return bool(config.modules.get(name, {}).get("enabled", False))

    async def listing(_args: Any, _ctx: ToolContext) -> ToolResult:
        here = installer.installed()
        lines = [
            f"- {name} : installé, {'allumé' if is_on(name) else 'éteint'} ({m.source})"
            for name, m in sorted(here.items())
        ]
        offered = [
            f"- {e.name} : {e.description}"
            + (f" Il atteint : {e.accesses}" if e.accesses else "")
            + (f" À régler : {e.settings}" if e.settings else "")
            for e in installer.catalog().values()
            if e.name not in here
        ]
        return ToolResult(
            "Modules installés :\n"
            + ("\n".join(lines) or "aucun")
            + "\n\nDans le catalogue :\n"
            + ("\n".join(offered) or "rien d'autre")
        )

    async def install(args: Any, ctx: ToolContext) -> ToolResult:
        from orion.configure.module import Change

        try:
            entry = await asyncio.to_thread(installer.install, args.name)
        except InstallError as exc:
            return ToolResult(f"Pas installé : {exc}.", is_error=True)
        switch = [Change(path=f"modules.{entry.name}.enabled", value=True)]
        problem = configurator.precheck(switch)
        if problem is not None:
            needs = f" Il lui faut : {entry.settings}." if entry.settings else ""
            return ToolResult(
                f"« {entry.name} » est installé mais reste éteint : {problem}.{needs} Une fois "
                f"réglé, `modules.{entry.name}.enabled`."
            )
        try:
            done = await configurator.change(
                switch, f"installer le module {entry.name}", _origin(ctx), ctx.grant, entry.name
            )
        except ConfigureError as exc:
            return ToolResult(f"« {entry.name} » est installé mais pas allumé : {exc}.")
        return ToolResult(f"« {entry.name} » est installé et allumé. {done}")

    async def remove(args: Any, ctx: ToolContext) -> ToolResult:
        from orion.configure.module import Change

        if args.name not in installer.installed():
            return ToolResult(f"« {args.name} » n'est pas un module installé.", is_error=True)
        done = ""
        if is_on(args.name):
            switch = [Change(path=f"modules.{args.name}.enabled", value=False)]
            try:
                done = await configurator.change(
                    switch,
                    f"désinstaller le module {args.name}",
                    _origin(ctx),
                    ctx.grant,
                    args.name,
                    removing=True,
                )
            except ConfigureError as exc:
                return ToolResult(f"Pas désinstallé : {exc}.", is_error=True)
        try:
            await asyncio.to_thread(installer.remove, args.name)
        except InstallError as exc:
            return ToolResult(f"Pas désinstallé : {exc}.", is_error=True)
        return ToolResult(f"« {args.name} » est désinstallé. {done}".strip())

    return [
        Tool(
            name="modules",
            description="Modules beyond Orion's own: those installed, and those the public "
            "catalog offers (what each does, reaches, needs set). For a capability you lack, "
            "look here; install one only when he asks (`module_install`).",
            input_model=ModulesInput,
            handler=listing,
            effect=Effect.NONE,
            reversibility=Reversibility.REVERSIBLE,
            concurrency_safe=True,
        ),
        Tool(
            name="module_install",
            description="Install a module from the catalog and turn it on, then restart. It "
            "is code that runs with your access, his memory included: only when he asks, and "
            "he approves its exact source.",
            input_model=ModuleNameInput,
            handler=install,
            effect=Effect.EXTERNAL,
            reversibility=Reversibility.REVERSIBLE,
        ),
        Tool(
            name="module_remove",
            description="Turn off and uninstall a module installed from the catalog, then "
            "restart. Orion's own modules are turned off, never removed (configure_change).",
            input_model=ModuleNameInput,
            handler=remove,
            effect=Effect.EXTERNAL,
            reversibility=Reversibility.REVERSIBLE,
        ),
    ]


def _origin(ctx: ToolContext) -> Provenance:
    return Provenance(source=f"thread:{ctx.thread_id}", actor="orion:core")
