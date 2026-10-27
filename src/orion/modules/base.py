"""Modules: the pieces that snap onto Orion's core.

The core (journal, memory, guardian, runtime, inbox, initiative engine, reminders) is the plate.
Everything else is a module: a channel (iMessage), a capability (voice, delegation), a
background skill (adaptation), a source (calendar, mail). A module can:

- give tools to the model (`assemble`, the same in `orion chat` and `orion serve`);
- add a section to the constitution (`instructions`);
- run supervised services, be an owner channel, report things for the initiative engine to
  pass on, or ring the owner (`serve`, daemon only);
- report its state in `orion doctor`.

The set of enabled modules is fixed at startup, so the tool registry and the system prompt are
too: the prompt-cache contract holds. Modules are trusted code: installing one gives it the
same access as Orion itself. What it can do in the world still goes through the guardian
(every tool declares its effect), and what it brings in carries `Provenance` and `Trust`.

Third-party modules are Python packages exposing a `Module` subclass under the `orion.modules`
entry point group; they are configured in `[modules.<name>]`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from pydantic import BaseModel, ConfigDict

from orion.resources import read_resource

if TYPE_CHECKING:
    from orion.channels.inbox import OwnerInbox
    from orion.config import OrionConfig
    from orion.core.journal import SqliteJournal
    from orion.core.supervisor import Supervisor
    from orion.guardian.autonomy import Autonomy
    from orion.guardian.client import GuardianClient
    from orion.guardian.policy import ActionProposal, Approval, Approver
    from orion.initiative.arbiter import PresenceSource
    from orion.initiative.engine import AlertSource, Caller, DigestSource, ReportSource
    from orion.initiative.timers import Timers
    from orion.memory.service import MemoryService
    from orion.providers.base import ModelProvider
    from orion.runtime import Orion
    from orion.tools.base import Tool

M = TypeVar("M", bound="Module")


@dataclass(frozen=True)
class Planned:
    """Something Orion will do for the owner, on its own, at a set time (D78)."""

    at: datetime
    what: str
    """In French, as the owner would read it: « te demander comment s'est passé l'entretien »."""
    undo: str = ""
    """How to call it off, for Orion: « later (cancel, fu_…) ». Empty: it cannot be."""


def next_weekly(now: datetime, weekday: int, at: time) -> datetime:
    """The next time it is `weekday` (0: Monday) at `at`, after `now`, in now's zone."""
    for ahead in range(8):
        day = now.date() + timedelta(days=ahead)
        moment = datetime.combine(day, at, now.tzinfo)
        if day.weekday() == weekday and moment > now:
            return moment
    raise AssertionError("unreachable")  # pragma: no cover


class ModuleError(RuntimeError):
    """A module cannot load or start: missing dependency, bad configuration, unreachable
    service. Orion refuses to start rather than run without it."""


class ModuleSettings(BaseModel):
    """Base for a third-party module's `[modules.<name>]` section."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False


class Module:
    """Subclass and override the hooks you need; all are optional."""

    name: ClassVar[str]
    """Unique, lowercase; also the `[modules.<name>]` section of a third-party module."""
    requires: ClassVar[tuple[str, ...]] = ()
    """Modules that must be enabled too; they are assembled and started first."""
    instructions: ClassVar[str | None] = None
    """A built-in module's constitution section (a packaged resource of `orion.resources`).
    Third-party modules override `instructions_text` instead."""
    settings_model: ClassVar[type[ModuleSettings]] = ModuleSettings
    offer: ClassVar[str | None] = None
    """What the module would let Orion do, and how the owner turns it on, in one French line:
    while the module is off, Orion proposes it when a request needs it (D64)."""
    channel: ClassVar[bool] = False
    """An owner channel (Orion can be talked to through it): `orion serve` needs one."""

    def __init__(self, config: OrionConfig) -> None:
        self.config = config

    @property
    def enabled(self) -> bool:
        """Built-in modules read their own config section; others `[modules.<name>]`."""
        return self.settings.enabled

    @property
    def settings(self) -> Any:
        return self.settings_model.model_validate(self.config.modules.get(self.name, {}))

    def offers(self) -> list[str]:
        """What Orion could do with a little more setup: the `offer` while the module is off; a
        module that is on may name what it still lacks. Read once at startup (cache)."""
        return [self.offer] if self.offer and not self.enabled else []

    def instructions_text(self) -> str:
        """Appended to the constitution, so it must not change while Orion runs (cache)."""
        return read_resource(self.instructions).strip() if self.instructions else ""

    async def assemble(self, plate: Plate) -> list[Tool]:
        """Open resources and return the module's tools. Runs for every entry point."""
        return []

    async def serve(self, daemon: Daemon) -> None:
        """Daemon only: register services, outlets, reports. Runs in dependency order."""

    async def close(self) -> None:
        """Release what `assemble` or `serve` opened."""

    def doctor(self) -> list[str]:
        """Lines for `orion doctor` when the module is enabled (config problems first)."""
        return []

    async def planned(self, now: datetime, until: datetime) -> list[Planned]:
        """What the module will do for the owner on its own between now and `until`, so he
        can see it all in one place and call any of it off (D78)."""
        return []


class LateApprover:
    """Approver chosen after assembly (a channel needs Orion to exist first). Until one is set,
    everything that needs approval is refused."""

    def __init__(self) -> None:
        from orion.guardian.policy import DenyAllApprover

        self.target: Approver = DenyAllApprover()

    async def approve(self, proposal: ActionProposal) -> Approval:
        return await self.target.approve(proposal)


@dataclass
class Plate:
    """What every module can build on, at assembly."""

    config: OrionConfig
    provider: ModelProvider
    journal: SqliteJournal
    memory: MemoryService
    timers: Timers
    approver: LateApprover
    modules: list[Module]
    guardian: GuardianClient | None = None
    """The guardian process, when Orion runs split (D28): it holds the secrets, so modules
    reach Inkbox, calendars and mailboxes through it."""
    autonomy: Autonomy | None = None

    def module(self, kind: type[M]) -> M | None:
        """An enabled module, by class (for modules that build on another)."""
        for module in self.modules:
            if isinstance(module, kind):
                return module
        return None

    def has(self, name: str) -> bool:
        return any(module.name == name for module in self.modules)


Service = Callable[[], Awaitable[None]]


@dataclass
class Daemon:
    """What modules plug into when `orion serve` starts."""

    plate: Plate
    orion: Orion
    inbox: OwnerInbox
    supervisor: Supervisor
    reports: list[ReportSource] = field(default_factory=list["ReportSource"])
    """Outcomes to pass on (always delivered)."""
    alerts: list[AlertSource] = field(default_factory=list["AlertSource"])
    """Unrequested messages (quiet hours and the budget apply)."""
    digests: list[DigestSource] = field(default_factory=list["DigestSource"])
    """Lines for the daily digests."""
    presence: list[PresenceSource] = field(default_factory=list["PresenceSource"])
    """Whether the owner is busy now (the arbiter waits for a break)."""
    caller: Caller | None = None

    def add_service(self, name: str, run: Service) -> None:
        self.supervisor.add(name, run)
