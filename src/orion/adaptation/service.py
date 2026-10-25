"""Adapter: the background process through which Orion learns its owner.

Runs every few minutes, off the conversation path:

1. attention (deterministic): drop digest slots the owner keeps ignoring;
2. reflection (cheap model, batched): once enough new owner signals have piled up, review them
   and adjust the playbook.

Each reflection is journaled with the position it covered, so a restart resumes where it left
off and never reflects on the same signals twice.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import structlog

from orion.adaptation.attention import ignored_slots
from orion.adaptation.reflector import ReflectionReport, Reflector
from orion.adaptation.signals import collect
from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.memory.model import SettingKey
from orion.memory.service import MemoryService, Origin

log = structlog.get_logger(__name__)

REFLECTED = "adaptation.reflected"
MAX_SIGNALS = 80


@dataclass(frozen=True)
class AdaptationSettings:
    timezone: str = "Europe/Paris"
    digests: tuple[time, ...] = ()
    """The configured digest times, before any learned change."""
    reflect_after_messages: int = 12
    reflect_daily_after_messages: int = 3
    reflect_every: timedelta = timedelta(hours=24)


@dataclass
class AdaptationReport:
    muted: list[str] = field(default_factory=list[str])
    reflection: ReflectionReport | None = None


class Adapter:
    def __init__(
        self,
        journal: EventStore,
        memory: MemoryService,
        reflector: Reflector | None,
        settings: AdaptationSettings,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._journal = journal
        self._memory = memory
        self._reflector = reflector
        self._settings = settings
        self._zone = ZoneInfo(settings.timezone)
        self._clock = clock or (lambda: datetime.now(self._zone))

    async def run(self, interval: float = 600.0) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(interval)

    async def tick(self) -> AdaptationReport:
        await self._memory.sync()
        events = await self._events()
        now = self._clock().astimezone(self._zone)
        report = AdaptationReport(muted=await self._attention(events, now))
        report.reflection = await self._reflect(events, now)
        return report

    async def _attention(self, events: list[Event], now: datetime) -> list[str]:
        current = (await self._memory.store.settings()).get(SettingKey.DIGESTS)
        if current is not None and isinstance(current.value, list):
            digests = tuple(time.fromisoformat(v) for v in current.value)
            since = current.seq
        else:
            digests, since = self._settings.digests, 0
        decisions = ignored_slots(events, digests, self._zone, now, since_seq=since)
        if not decisions:
            return []
        muted = {d.slot for d in decisions}
        remaining = [f"{slot:%H:%M}" for slot in digests if slot not in muted]
        reason = "; ".join(d.reason for d in decisions) + " : je ne les envoie plus"
        await self._memory.change_setting(
            SettingKey.DIGESTS,
            remaining,
            reason,
            Origin(
                actor="orion:adaptation",
                source="orion:adaptation",
                trust=Trust.SYSTEM,
                derived_from=tuple(e for d in decisions for e in d.evidence),
            ),
        )
        log.info("adaptation.muted_digests", slots=[f"{s:%H:%M}" for s in sorted(muted)])
        return [f"{s:%H:%M}" for s in sorted(muted)]

    async def _reflect(self, events: list[Event], now: datetime) -> ReflectionReport | None:
        if self._reflector is None or not events:
            return None
        cursor, last_at = 0, None
        for e in events:
            if e.kind == REFLECTED and not e.redacted:
                cursor = int(e.payload["up_to_seq"])
                last_at = datetime.fromisoformat(str(e.payload["at"]))
        signals = collect(events, self._zone, now, after_seq=cursor)
        messages = sum(1 for s in signals if s.kind == "message")
        s = self._settings
        due = messages >= s.reflect_after_messages or (
            messages >= s.reflect_daily_after_messages
            and (last_at is None or now - last_at >= s.reflect_every)
        )
        if not due:
            return None
        # The oldest first, MAX_SIGNALS at a time: what is left over is reviewed next time,
        # never skipped.
        batch = signals[:MAX_SIGNALS]
        report = await self._reflector.reflect(batch)
        if report.unavailable:
            return report  # nothing reviewed: the same signals wait for the next pass
        up_to = events[-1].seq
        if len(signals) > MAX_SIGNALS:
            seqs = {e.id: e.seq for e in events}
            up_to = seqs.get(batch[-1].id, up_to)
        await self._journal.append(
            [
                NewEvent(
                    kind=REFLECTED,
                    payload={
                        "up_to_seq": up_to,
                        "at": now.isoformat(),
                        "signals": len(signals),
                        **report.model_dump(),
                    },
                    provenance=Provenance(source="orion:adaptation", actor="orion:adaptation"),
                    trust=Trust.SYSTEM,
                    correlation_id=new_id(),
                )
            ]
        )
        return report

    async def _events(self) -> list[Event]:
        events: list[Event] = []
        cursor = 0
        while batch := await self._journal.read(cursor, 1000):
            events.extend(batch)
            cursor = batch[-1].seq
        return events
