"""Finding and ordering modules: the built-in ones, then those installed as packages (in
Orion's environment, or by `orion modules install` into its data folder, D105)."""

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
    from orion.advice.module import AdviceModule
    from orion.awaiting.module import AwaitingModule
    from orion.booking.module import BookingModule
    from orion.calendar.module import CalendarModule
    from orion.channels.module import IMessageModule
    from orion.channels.telegram_module import TelegramModule
    from orion.chronicle.module import ChronicleModule
    from orion.computer.module import ComputerModule
    from orion.configure.module import ConfigureModule
    from orion.console.module import ConsoleModule
    from orion.contacts.module import ContactsModule
    from orion.curiosity.module import CuriosityModule
    from orion.delegation.module import DelegationModule
    from orion.departure.module import DepartureModule
    from orion.expert.module import ExpertModule
    from orion.federation.module import FederationModule
    from orion.files.module import FilesModule
    from orion.focus.module import FocusModule
    from orion.hands.module import HandsModule
    from orion.intentions.module import IntentionsModule
    from orion.library.module import LibraryModule
    from orion.mail.module import MailModule
    from orion.mcp.module import McpModule
    from orion.memory.doubt import DoubtModule
    from orion.memory.night import NightModule
    from orion.missions.module import MissionsModule
    from orion.mobile.module import MobileModule
    from orion.onboarding.module import OnboardingModule
    from orion.papers.module import PapersModule
    from orion.photos.module import PhotosModule
    from orion.places.module import PlacesModule
    from orion.recap.module import RecapModule
    from orion.relations.module import RelationsModule
    from orion.secretary.module import SecretaryModule
    from orion.shell.module import ShellModule
    from orion.skills.module import SkillsModule
    from orion.style.module import StyleModule
    from orion.tasks.module import TasksModule
    from orion.thinking.module import ThinkingModule
    from orion.vocal.module import VocalModule
    from orion.voice.module import VoiceModule
    from orion.watch.module import WatchModule
    from orion.web.module import WebModule

    return [
        MobileModule,
        IMessageModule,
        TelegramModule,
        VoiceModule,
        DelegationModule,
        AdaptationModule,
        NightModule,
        DoubtModule,
        CalendarModule,
        MailModule,
        FederationModule,
        WebModule,
        CuriosityModule,
        FilesModule,
        ComputerModule,
        BookingModule,
        OnboardingModule,
        FocusModule,
        ExpertModule,
        MissionsModule,
        TasksModule,
        LibraryModule,
        PhotosModule,
        PapersModule,
        ThinkingModule,
        WatchModule,
        AwaitingModule,
        ChronicleModule,
        IntentionsModule,
        RelationsModule,
        ContactsModule,
        DepartureModule,
        AdviceModule,
        StyleModule,
        ConfigureModule,
        VocalModule,
        PlacesModule,
        SecretaryModule,
        ConsoleModule,
        HandsModule,
        RecapModule,
        McpModule,
        SkillsModule,
        ShellModule,
    ]


def discover(config: OrionConfig | None = None) -> list[type[Module]]:
    """Every module Orion knows about, built-in first. Names must be unique."""
    if config is not None:
        from orion.modules.installer import activate, modules_dir

        activate(modules_dir(config))
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
    from orion.modules.installer import imports_modules, installed, modules_dir

    kinds = available if available is not None else discover(config)
    known = {kind.name for kind in kinds}
    unread: set[str] = set()
    if not imports_modules():  # the guardian: installed modules are known, never imported
        unread = set(installed(modules_dir(config))) - known
    for name, section in config.modules.items():
        if name in known or name in unread:
            continue
        if not section.get("enabled", False):
            continue  # left behind by a removed module, off: harmless
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
