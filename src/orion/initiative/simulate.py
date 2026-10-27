"""A week of initiative, played ahead on a copy (D82).

A dozen sources compete for a few messages a day. Nobody can tell from the code what Orion would
actually send over a week: `orion simulate` plays it. It copies the journal into a scratch
folder, builds Orion on the copy with a model that writes nothing (no call leaves, nothing is
spent), plugs in every module's alerts, digests and presence, and ticks the initiative engine
on a simulated clock. What would have gone out, what was held back and why, day by day.

Nothing is sent and the real journal is never written: the engine's notifier only takes notes.
What needs a model to exist (the night's thoughts, curiosity's finds, the weekly page) is not
produced by the simulation: only what is already planned or in the journal plays.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from orion.app import build_app
from orion.channels.inbox import OwnerInbox
from orion.config import OrionConfig
from orion.core.events import Event
from orion.core.journal import EventStore
from orion.core.supervisor import Supervisor
from orion.initiative.engine import DELIVERED, SUPPRESSED, InitiativeEngine, InitiativeSettings
from orion.modules.base import Daemon
from orion.providers.base import ModelRequest, ModelResponse, Usage
from orion.runtime import OWNER_THREAD

_DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
STEP = timedelta(minutes=5)


class Silent:
    """A model that writes a placeholder and calls nothing: the simulation spends nothing."""

    name = "simulation"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        text = "(message simulé)"
        return ModelResponse(
            message={"role": "assistant", "content": text},
            text=text,
            tool_calls=[],
            finish_reason="stop",
            usage=Usage(),
            model="simulation",
        )


class Notes:
    """The engine's notifier: nothing is sent, the time of each message is noted."""

    def __init__(self, clock: Callable[[], datetime]) -> None:
        self.clock = clock
        self.sent: list[datetime] = []

    async def available(self) -> bool:
        return True

    async def notify(self, text: str, correlation_id: str) -> None:
        self.sent.append(self.clock())


@dataclass
class Line:
    at: datetime | None
    key: str
    why: str
    reason: str = ""


@dataclass
class Week:
    start: datetime
    days: int
    budget: int
    sent: dict[date, list[Line]] = field(default_factory=dict[date, list[Line]])
    held: dict[date, list[Line]] = field(default_factory=dict[date, list[Line]])

    def render(self) -> str:
        out: list[str] = [
            f"Simulation du {self.start:%d/%m %H:%M}, sur {self.days} jours "
            f"(budget : {self.budget} messages spontanés par jour). Rien n'a été envoyé."
        ]
        total = 0
        for offset in range(self.days + 1):
            day = (self.start + timedelta(days=offset)).date()
            sent, held = self.sent.get(day, []), self.held.get(day, [])
            if not sent and not held:
                continue
            total += len(sent)
            out.append(f"\n{_DAYS[day.weekday()].capitalize()} {day:%d/%m} : {len(sent)} envoyé(s)")
            for line in sent:
                at = f"{line.at:%H:%M}" if line.at else "--:--"
                out.append(f"  {at}  {_kind(line.key):<12} {line.why}")
            for line in held:
                out.append(f"  tu    {_kind(line.key):<12} {line.why} — {line.reason}")
        out.append(f"\nEn tout : {total} message(s) sur {self.days} jours.")
        out.append(
            "Non simulé : ce qui demande un modèle pour exister (pensées de la nuit, trouvailles "
            "de curiosité, page de la semaine)."
        )
        return "\n".join(out)


def _kind(key: str) -> str:
    return key.split(":", 1)[0]


def _copy_journal(source: Path, target: Path) -> None:
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


async def simulate(config: OrionConfig, days: int = 7, start: datetime | None = None) -> Week:
    zone = ZoneInfo(config.agent.timezone)
    begin = (start or datetime.now(zone)).astimezone(zone)
    scratch = Path(tempfile.mkdtemp(prefix="orion-simulate-"))
    try:
        if config.journal_path.exists():
            _copy_journal(config.journal_path, scratch / config.journal_path.name)
        played = config.model_copy(
            update={
                "data_dir": scratch,
                "source": None,
                "channels": config.channels.model_copy(
                    update={
                        "imessage": config.channels.imessage.model_copy(update={"enabled": False})
                    }
                ),
                "voice": config.voice.model_copy(update={"enabled": False}),
                "federation": config.federation.model_copy(update={"enabled": False}),
                "memory": config.memory.model_copy(update={"embedding_model": None}),
            }
        )
        return await _play(played, begin, days, zone)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


async def _play(config: OrionConfig, begin: datetime, days: int, zone: ZoneInfo) -> Week:
    now = [begin]
    initiative = config.initiative
    week = Week(begin, days, initiative.daily_budget)
    async with build_app(config, Silent()) as app:
        inbox = OwnerInbox(app.orion, 0.0, beat_seconds=0.0)
        daemon = Daemon(app.plate, app.orion, inbox, Supervisor())
        for module in app.modules:
            if not module.channel:
                await module.serve(daemon)  # registers alerts; its services never run
        notes = Notes(lambda: now[0])
        engine = InitiativeEngine(
            app.orion,
            app.journal,
            app.timers,
            notes,
            InitiativeSettings(
                timezone=config.agent.timezone,
                thread_id=OWNER_THREAD,
                digests=tuple(initiative.digests),
                quiet_start=initiative.quiet_start,
                quiet_end=initiative.quiet_end,
                daily_budget=initiative.daily_budget,
            ),
            memory=app.memory.store,
            clock=lambda: now[0],
            reports=daemon.reports,
            alerts=daemon.alerts,
            digests=daemon.digests,
            presence=daemon.presence,
        )
        first = await app.journal.last_seq()
        sent_at: dict[str, datetime] = {}
        end = begin + timedelta(days=days)
        while now[0] < end:
            for key in await engine.tick():
                sent_at[key] = now[0]
            now[0] += STEP
        for event in await _events_after(app.journal, first):
            payload = event.payload
            key, why = str(payload.get("key", "")), str(payload.get("why", ""))
            if event.kind == DELIVERED:
                at = sent_at.get(key)
                day = at.date() if at else date.fromisoformat(str(payload["day"]))
                week.sent.setdefault(day, []).append(Line(at, key, why))
            elif event.kind == SUPPRESSED:
                day = date.fromisoformat(str(payload["day"]))
                reason = str(payload.get("reason", ""))
                week.held.setdefault(day, []).append(Line(None, key, why, reason))
        for lines in week.sent.values():
            lines.sort(key=lambda line: line.at or begin)
    return week


async def _events_after(journal: EventStore, after: int) -> list[Event]:
    found: list[Event] = []
    cursor = after
    while batch := await journal.read(cursor, 1000):
        found += batch
        cursor = batch[-1].seq
    return found
