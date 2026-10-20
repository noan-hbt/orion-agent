"""Finding and ordering modules: the built-in ones, then those installed as packages."""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import TYPE_CHECKING

from orion.modules.base import Module, ModuleError

if TYPE_CHECKING:
    from orion.config import OrionConfig

ENTRY_POINT_GROUP = "orion.modules"


def builtin_modules() -> list[type[Module]]:
    # Imported here: a module's dependencies load only when modules are listed.
    from orion.adaptation.module import AdaptationModule
    from orion.calendar.module import CalendarModule
    from orion.channels.module import IMessageModule
    from orion.computer.module import ComputerModule
    from orion.console.module import ConsoleModule
    from orion.delegation.module import DelegationModule
    from orion.expert.module import ExpertModule
    from orion.federation.module import FederationModule
    from orion.hands.module import HandsModule
    from orion.library.module import LibraryModule
    from orion.mail.module import MailModule
    from orion.memory.night import NightModule
    from orion.missions.module import MissionsModule
    from orion.onboarding.module import OnboardingModule
    from orion.voice.module import VoiceModule
    from orion.web.module import WebModule

    return [
        IMessageModule,
        VoiceModule,
        DelegationModule,
        AdaptationModule,
        NightModule,
        CalendarModule,
        MailModule,
        FederationModule,
        WebModule,
        ComputerModule,
        OnboardingModule,
        ExpertModule,
        MissionsModule,
        LibraryModule,
        ConsoleModule,
        HandsModule,
    ]


def discover() -> list[type[Module]]:
    """Every module Orion knows about, built-in first. Names must be unique."""
    found = builtin_modules()
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        loaded = entry.load()
        if not (isinstance(loaded, type) and issubclass(loaded, Module)):
            raise ModuleError(f"entry point {entry.name!r} is not an orion Module subclass")
        found.append(loaded)
    names = [m.name for m in found]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ModuleError(f"several modules are named {', '.join(duplicates)}")
    return found


def load(config: OrionConfig, available: list[type[Module]] | None = None) -> list[Module]:
    """The enabled modules, each after the modules it requires. Fails closed on a module that
    is configured but not installed, or enabled without what it needs."""
    kinds = available if available is not None else discover()
    known = {kind.name for kind in kinds}
    for name in config.modules:
        if name not in known:
            raise ModuleError(f"[modules.{name}] is configured but no such module is installed")
    enabled = {module.name: module for module in (kind(config) for kind in kinds) if module.enabled}
    for module in enabled.values():
        for needed in module.requires:
            if needed not in enabled:
                raise ModuleError(f"module {module.name!r} needs {needed!r}: enable it too")

    ordered: list[Module] = []
    visiting: set[str] = set()

    def visit(module: Module) -> None:
        if module in ordered:
            return
        if module.name in visiting:
            raise ModuleError(f"modules depend on each other in a loop around {module.name!r}")
        visiting.add(module.name)
        for needed in module.requires:
            visit(enabled[needed])
        visiting.discard(module.name)
        ordered.append(module)

    for module in enabled.values():
        visit(module)
    return ordered
