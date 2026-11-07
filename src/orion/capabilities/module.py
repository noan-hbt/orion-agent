"""What Orion can do right now, and what broke (ORI-019, D283): `[modules.capabilities]`, on by
default.

Each enabled module says what it lets Orion do (`Module.capabilities`, built on its `doctor`
lines unless it can tell a broken connector itself). This module asks them every few minutes
and journals `capability.health_changed` only at a transition: a connector that falls is one
event, not one per check. The `capabilities` tool reads the state back (the projection of
those events), so Orion can say what it can and cannot do, and since when. Nothing goes in the
system message (prompt cache).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel

from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.hlc import Timestamp
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.modules.base import Capability, Daemon, Module, ModuleSettings, Plate
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from orion.config import OrionConfig

log = structlog.get_logger(__name__)

CHANGED = "capability.health_changed"
EVERY = 300.0


@dataclass
class HealthBook:
    """capability name → (available, detail, since). A projection of the journal."""

    name: str = "capabilities"
    state: dict[str, tuple[bool, str, datetime]] = field(
        default_factory=dict[str, tuple[bool, str, datetime]]
    )
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.state.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.kind != CHANGED or event.redacted:
            return
        p: dict[str, Any] = event.payload
        at = datetime.fromtimestamp(Timestamp.decode(event.hlc).wall_ms / 1000, UTC)
        self.state[str(p["capability"])] = (bool(p["available"]), str(p.get("detail", "")), at)


class Health:
    def __init__(self, journal: EventStore, modules: list[Module]) -> None:
        self._journal = journal
        self._modules = modules
        self.book = HealthBook()
        self._runner = ProjectionRunner(journal, [self.book])

    def probe(self) -> list[Capability]:
        found: list[Capability] = []
        for module in self._modules:
            try:
                found += module.capabilities()
            except Exception as exc:  # a probe that breaks says the module is unavailable
                log.exception("capabilities.probe_failed", module=module.name)
                found.append(Capability(module.name, False, f"{type(exc).__name__}: {exc}"))
        return found

    async def check(self) -> list[Capability]:
        """Probe every module; journal each capability whose availability changed."""
        await self._runner.catch_up()
        changed = [
            c
            for c in self.probe()
            if c.name not in self.book.state or self.book.state[c.name][0] != c.available
        ]
        if changed:
            await self._journal.append(
                [
                    NewEvent(
                        kind=CHANGED,
                        payload={
                            "capability": c.name,
                            "available": c.available,
                            "detail": c.detail,
                        },
                        provenance=Provenance(source="orion:capabilities", actor="orion:core"),
                        trust=Trust.SYSTEM,
                    )
                    for c in changed
                ]
            )
            await self._runner.catch_up()
        return changed

    async def run(self, every: float = EVERY) -> None:
        while True:
            await self.check()
            await asyncio.sleep(every)

    async def handle(self, _args: BaseModel, _ctx: ToolContext) -> ToolResult:
        await self._runner.catch_up()
        if not self.book.state:
            return ToolResult("Aucun état de capacité relevé pour l'instant.")
        lines = [
            f"- {name} : {'disponible' if ok else 'INDISPONIBLE'} depuis le {since:%d/%m %H:%M} UTC"
            + (f" ({detail})" if detail else "")
            for name, (ok, detail, since) in sorted(self.book.state.items())
        ]
        return ToolResult("\n".join(lines))


class NoInput(BaseModel):
    pass


def capabilities_tool(health: Health) -> Tool:
    return Tool(
        name="capabilities",
        description=(
            "What Orion can do right now through its modules (mail, calendar, browser…), and "
            "what is unavailable, since when. Read it before promising something a connector "
            "must do, or when he asks what works."
        ),
        input_model=NoInput,
        handler=health.handle,
        effect=Effect.NONE,
        reversibility=Reversibility.REVERSIBLE,
        concurrency_safe=True,
        accepts_untrusted_context=True,
    )


class CapabilitiesSettings(ModuleSettings):
    enabled: bool = True


class CapabilitiesModule(Module):
    name = "capabilities"
    settings_model = CapabilitiesSettings

    def __init__(self, config: OrionConfig) -> None:
        super().__init__(config)
        self.health: Health | None = None

    async def assemble(self, plate: Plate) -> list[Tool]:
        others = [m for m in plate.modules if m is not self]
        self.health = Health(plate.journal, others)
        return [capabilities_tool(self.health)]

    async def serve(self, daemon: Daemon) -> None:
        if self.health is not None:
            daemon.add_service("capabilities.check", self.health.run)

    def capabilities(self) -> list[Capability]:
        return []
