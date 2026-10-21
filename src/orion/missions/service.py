"""Missions: a goal Orion keeps working on, over days, without being asked again.

« Trouve-moi un appart à Dreux sous 700 € » is not a question to answer once: it is a mission.
Orion starts it, then works on it at its own pace (every `every_hours`), each time in the
mission's own thread, so the mission remembers what it did, what it saw and what the owner
turned down, and why. Each run ends in silence when there is nothing new worth saying, or in a
short report, which reaches the owner through the initiative engine (quiet hours, the daily
budget, the arbiter): the owner's answer lands in their own conversation, where Orion can note
a refusal or close the mission.

What a mission saw is recorded item by item (`mission_seen` answers which are new), so the
same listing is never reported twice; what the owner refused is kept with the reason, and the
next runs read it.

    mission.started  mission.updated  mission.seen  mission.rejected  mission.ran
    mission.closed
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import structlog

from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner

log = structlog.get_logger(__name__)

STARTED = "mission.started"
UPDATED = "mission.updated"
SEEN = "mission.seen"
REJECTED = "mission.rejected"
RAN = "mission.ran"
CLOSED = "mission.closed"
RUSHED = "mission.rushed"
RUSHES_PER_DAY = 8
"""Passes asked for right away (beyond its pace), per mission and per day."""
NOTES_SHOWN = 6
REJECTED_SHOWN = 20


@dataclass
class Mission:
    id: str
    goal: str
    criteria: str
    every_hours: int
    started_at: datetime
    notes: list[str] = field(default_factory=list[str])
    seen: dict[str, str] = field(default_factory=dict[str, str])
    """key (an address, a reference) → a short label."""
    rejected: dict[str, str] = field(default_factory=dict[str, str])
    """key → why the owner turned it down."""
    runs: int = 0
    last_run: datetime | None = None
    reports: list[tuple[int, datetime, str, Trust]] = field(
        default_factory=list[tuple[int, datetime, str, Trust]]
    )
    """(run number, when, what it had to say, the trust of what it was based on)."""
    trust: Trust = Trust.OWNER
    """The lowest trust of what the mission recorded (notes, items seen): its brief carries
    page content, so it is never more trusted than that."""
    closed: str | None = None
    """Why it ended (done, given up…), once closed."""
    rushes: list[datetime] = field(default_factory=list[datetime])
    """When a pass was asked for right away (by the owner, or by a pass with more to see)."""
    rushes_seen: int = 0
    """How many of those the last pass started after: one asked later makes it due again."""

    def rushes_left(self, now: datetime) -> int:
        return max(0, RUSHES_PER_DAY - sum(1 for at in self.rushes if at.date() == now.date()))

    @property
    def thread(self) -> str:
        return f"mission:{self.id}"

    def due(self, now: datetime) -> bool:
        if self.closed is not None:
            return False
        if len(self.rushes) > self.rushes_seen:
            return True
        return self.last_run is None or now - self.last_run >= timedelta(hours=self.every_hours)

    def brief(self) -> str:
        """What a run starts from: the mission as it stands."""
        lines = [
            f"Mission ({self.id}) : {self.goal}",
            f"Critères : {self.criteria or '(aucun précisé)'}",
            f"Passage n°{self.runs + 1}, toutes les {self.every_hours} h.",
        ]
        if self.rejected:
            lines.append(
                "Refusé par le propriétaire (ne le repropose pas, et tiens compte du motif) :"
            )
            lines += [
                f"- {key} : {why}" for key, why in list(self.rejected.items())[-REJECTED_SHOWN:]
            ]
        if self.seen:
            lines.append(
                f"Déjà vu : {len(self.seen)} élément(s) ; `mission` (seen) dit ce qui est neuf."
            )
        if self.notes:
            lines.append("Tes notes des passages précédents :")
            lines += [f"- {note}" for note in self.notes[-NOTES_SHOWN:]]
        lines.append(
            "Travaille maintenant : cherche ce qui est neuf (recherche web, ton navigateur), "
            "enregistre chaque élément trouvé avec `mission` (seen), note ce que tu apprends "
            "avec `mission` (note). Ne demande aucun accord pendant ce travail. Puis réponds : "
            "SILENCE s'il n'y a rien de neuf qui vaille un message, sinon le message pour le "
            "propriétaire, court, avec les liens. Si la mission est accomplie ou n'a plus de "
            "sens, propose-lui de la clore."
        )
        lines.append(
            "S'il reste des pistes que ce passage n'a pas le temps de voir, `mission` (now) : un "
            "autre passage repart juste après celui-ci, et dis-le-lui. Sinon, ne lui promets "
            "pas de continuer avant le prochain passage."
        )
        return "\n".join(lines)


@dataclass
class MissionState:
    name: str = "missions"
    missions: dict[str, Mission] = field(default_factory=dict[str, Mission])
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.missions.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted or not event.kind.startswith("mission."):
            return
        payload: dict[str, Any] = event.payload
        if event.kind == STARTED:
            self.missions[str(payload["mission_id"])] = Mission(
                id=str(payload["mission_id"]),
                goal=str(payload["goal"]),
                criteria=str(payload.get("criteria", "")),
                every_hours=int(payload.get("every_hours", 24)),
                started_at=datetime.fromisoformat(str(payload["at"])),
            )
            return
        mission = self.missions.get(str(payload.get("mission_id", "")))
        if mission is None:
            return
        if event.kind in (UPDATED, SEEN, REJECTED):
            mission.trust = min(mission.trust, event.trust)
        if event.kind == UPDATED:
            if payload.get("criteria"):
                mission.criteria = str(payload["criteria"])
            if payload.get("every_hours"):
                mission.every_hours = int(payload["every_hours"])
            if payload.get("note"):
                mission.notes.append(str(payload["note"]))
        elif event.kind == SEEN:
            for item in payload.get("items", []):
                mission.seen.setdefault(str(item["key"]), str(item.get("label", "")))
        elif event.kind == REJECTED:
            mission.rejected[str(payload["key"])] = str(payload.get("why", ""))
        elif event.kind == RAN:
            mission.runs += 1
            mission.last_run = datetime.fromisoformat(str(payload["at"]))
            mission.rushes_seen = int(payload.get("rushes_seen", len(mission.rushes)))
            if payload.get("said"):
                mission.reports.append(
                    (
                        mission.runs,
                        mission.last_run,
                        str(payload["said"]),
                        Trust[str(payload.get("trust", "WEB"))],
                    )
                )
        elif event.kind == RUSHED:
            mission.rushes.append(datetime.fromisoformat(str(payload["at"])))
        elif event.kind == CLOSED:
            mission.closed = str(payload.get("outcome", "closed"))


class Missions:
    def __init__(self, journal: EventStore) -> None:
        self._journal = journal
        self.state = MissionState()
        self._runner = ProjectionRunner(journal, [self.state])
        self.waking: list[Callable[[], None]] = []
        """Told when a mission starts or is asked for now, so its pass comes at once, not at the
        next tick."""

    async def sync(self) -> None:
        await self._runner.catch_up()

    async def get(self, mission_id: str) -> Mission | None:
        await self.sync()
        return self.state.missions.get(mission_id)

    async def active(self) -> list[Mission]:
        await self.sync()
        return [m for m in self.state.missions.values() if m.closed is None]

    async def start(
        self,
        goal: str,
        criteria: str,
        every_hours: int,
        now: datetime,
        provenance: Provenance,
        correlation_id: str | None,
    ) -> str:
        mission_id = f"m_{new_id()}"
        await self._record(
            STARTED,
            {
                "mission_id": mission_id,
                "goal": goal,
                "criteria": criteria,
                "every_hours": every_hours,
                "at": now.isoformat(),
            },
            provenance,
            Trust.OWNER,
            correlation_id,
        )
        self._wake()
        return mission_id

    async def rush(
        self,
        mission_id: str,
        now: datetime,
        provenance: Provenance,
        trust: Trust,
        correlation_id: str | None,
    ) -> int:
        """Ask for a pass right away; returns how many more may be asked for today."""
        await self._record(
            RUSHED,
            {"mission_id": mission_id, "at": now.isoformat()},
            provenance,
            trust,
            correlation_id,
        )
        self._wake()
        mission = await self.get(mission_id)
        return mission.rushes_left(now) if mission is not None else 0

    def _wake(self) -> None:
        for tell in self.waking:
            tell()

    async def update(
        self,
        mission_id: str,
        provenance: Provenance,
        trust: Trust,
        correlation_id: str | None,
        criteria: str = "",
        every_hours: int = 0,
        note: str = "",
    ) -> None:
        payload: dict[str, object] = {"mission_id": mission_id}
        if criteria:
            payload["criteria"] = criteria
        if every_hours:
            payload["every_hours"] = every_hours
        if note:
            payload["note"] = note
        await self._record(UPDATED, payload, provenance, trust, correlation_id)

    async def see(
        self,
        mission_id: str,
        items: list[tuple[str, str]],
        provenance: Provenance,
        trust: Trust,
        correlation_id: str | None,
    ) -> tuple[list[tuple[str, str]], list[str]]:
        """Records items found; returns (new ones, ones the owner already turned down)."""
        mission = await self.get(mission_id)
        if mission is None:
            raise KeyError(mission_id)
        fresh = [(k, label) for k, label in items if k not in mission.seen]
        refused = [k for k, _ in items if k in mission.rejected]
        if fresh:
            await self._record(
                SEEN,
                {"mission_id": mission_id, "items": [{"key": k, "label": lb} for k, lb in fresh]},
                provenance,
                trust,
                correlation_id,
            )
        return [(k, lb) for k, lb in fresh if k not in mission.rejected], refused

    async def reject(
        self,
        mission_id: str,
        key: str,
        why: str,
        provenance: Provenance,
        correlation_id: str | None,
    ) -> None:
        await self._record(
            REJECTED,
            {"mission_id": mission_id, "key": key, "why": why},
            provenance,
            Trust.OWNER,
            correlation_id,
        )

    async def ran(
        self, mission: Mission, now: datetime, said: str, trust: Trust, rushes_seen: int = -1
    ) -> None:
        """`rushes_seen`: the passes asked for when this one started (by default, all of them);
        one asked for during it still comes."""
        await self._record(
            RAN,
            {
                "mission_id": mission.id,
                "at": now.isoformat(),
                "said": said,
                "trust": trust.name,
                "rushes_seen": len(mission.rushes) if rushes_seen < 0 else rushes_seen,
            },
            Provenance(source=f"mission:{mission.id}", actor="orion:missions"),
            Trust.SYSTEM,
            None,
        )

    async def close(
        self, mission_id: str, outcome: str, provenance: Provenance, correlation_id: str | None
    ) -> None:
        await self._record(
            CLOSED,
            {"mission_id": mission_id, "outcome": outcome},
            provenance,
            Trust.OWNER,
            correlation_id,
        )

    async def _record(
        self,
        kind: str,
        payload: dict[str, object],
        provenance: Provenance,
        trust: Trust,
        correlation_id: str | None,
    ) -> None:
        await self._journal.append(
            [
                NewEvent(
                    kind=kind,
                    payload=payload,
                    provenance=provenance,
                    trust=trust,
                    correlation_id=correlation_id,
                )
            ]
        )
        await self.sync()
