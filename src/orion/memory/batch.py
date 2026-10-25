"""Memory extraction after a conversation, not after every message (D86).

Reading each exchange alone, right after it, cost a call per message for a few facts a day, and
saw too little: a detail corrected three messages later was recorded twice. Now the extractor
waits for a pause (`[memory] extract_idle_minutes` without a message from the owner) and reads
every exchange since its last pass, in one call:

- only once enough was said (`extract_min_messages`); a lone message waits for more, but never
  longer than `extract_max_wait_minutes` of silence;
- where it stopped is an event of its own, so a restart picks up the same exchanges, and
  rebuilding gives the same cursor. On first start, everything before is taken as read (it was,
  one exchange at a time).

    memory.extracted  (through: the last message read, how many exchanges, what was applied)
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.hlc import Timestamp
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.memory.extractor import EXTRACTOR_TRUST, ExtractionReport, MemoryExtractor
from orion.memory.model import FACT_ASSERTED
from orion.memory.service import MemoryService, Origin
from orion.memory.store import Fact

log = structlog.get_logger(__name__)

EXTRACTED = "memory.extracted"
RECEIVED = "message.received"
FINISHED = "turn.finished"
MESSAGE = "conversation.message"
CHECK_EVERY = 30.0


@dataclass
class Exchange:
    seq: int
    """The owner's last message in it."""
    correlation_id: str
    said: list[str] = field(default_factory=list[str])
    replies: list[str] = field(default_factory=list[str])
    facts: list[str] = field(default_factory=list[str])
    finished: str | None = None
    """The turn's stop reason, once it ended."""
    trust: Trust = Trust.OWNER
    """The lowest trust of what the turn read: what is drawn from it is worth no more."""
    events: list[str] = field(default_factory=list[str])
    """The exchange's events, so containing a source reaches what was drawn from them."""


@dataclass
class Pending:
    """What the owner said since the extractor's last pass."""

    thread_id: str
    name: str = "memory-batch"
    cursor: int = 0
    started: bool = False
    """Whether a pass was ever recorded (the first start takes the past as read)."""
    exchanges: dict[str, Exchange] = field(default_factory=dict[str, Exchange])
    last_said: datetime | None = None
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    @property
    def seen(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.cursor, self.started, self._seq = 0, False, 0
        self.exchanges.clear()
        self.last_said = None

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted:
            return
        payload: dict[str, Any] = event.payload
        if event.kind == EXTRACTED:
            self.cursor, self.started = int(payload["through"]), True
            self.exchanges = {k: v for k, v in self.exchanges.items() if v.seq > self.cursor}
            return
        correlation = event.correlation_id or ""
        if event.kind == RECEIVED and payload.get("thread_id") == self.thread_id:
            exchange = self.exchanges.setdefault(correlation, Exchange(event.seq, correlation))
            exchange.seq = event.seq
            exchange.trust = min(exchange.trust, event.trust)
            exchange.events.append(event.id)
            text = str(payload.get("text") or "")
            if payload.get("photos"):
                text = f"{text} (avec une photo)".strip()
            exchange.said.append(text)
            self.last_said = datetime.fromtimestamp(Timestamp.decode(event.hlc).wall_ms / 1000, UTC)
            return
        exchange = self.exchanges.get(correlation)
        if exchange is None:
            return
        if event.kind == MESSAGE:
            exchange.trust = min(exchange.trust, event.trust)
            exchange.events.append(event.id)
        if event.kind == FINISHED:
            exchange.finished = str(payload.get("reason"))
        elif event.kind == FACT_ASSERTED:
            exchange.facts.append(str(payload["fact_id"]))
        elif event.kind == MESSAGE:
            message: dict[str, Any] = payload.get("message") or {}
            content = message.get("content")
            if message.get("role") == "assistant" and isinstance(content, str) and content.strip():
                exchange.replies.append(content.strip())


class MemoryBatcher:
    def __init__(
        self,
        journal: EventStore,
        memory: MemoryService,
        extractor: MemoryExtractor,
        thread_id: str,
        timezone: str,
        idle: timedelta,
        min_messages: int,
        max_wait: timedelta,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._journal = journal
        self._memory = memory
        self._extractor = extractor
        self._idle, self._min, self._max_wait = idle, min_messages, max_wait
        self._clock = clock or (lambda: datetime.now(UTC))
        self._zone = ZoneInfo(timezone)
        self.pending = Pending(thread_id)
        self._runner = ProjectionRunner(journal, [self.pending])

    async def start(self) -> None:
        """The first time: what was said before was read one exchange at a time already."""
        await self._runner.catch_up()
        if not self.pending.started:
            await self._record(self.pending.seen, 0, ExtractionReport())

    def ready(self) -> list[Exchange]:
        """The exchanges to read now, in order, or none: still talking, or too little said."""
        exchanges = sorted(self.pending.exchanges.values(), key=lambda e: e.seq)
        last = self.pending.last_said
        if not exchanges or last is None:
            return []
        quiet = self._clock() - last
        if any(e.finished is None for e in exchanges) and quiet < self._max_wait:
            return []  # still talking; a turn that never ended (a crash) waits max_wait only
        enough = len(exchanges) >= self._min and quiet >= self._idle
        return exchanges if enough or quiet >= self._max_wait else []

    async def tick(self) -> ExtractionReport | None:
        await self._runner.catch_up()
        exchanges = self.ready()
        if not exchanges:
            return None
        read = [e for e in exchanges if e.finished == "completed"]
        recorded: list[Fact] = []
        for exchange in read:
            for fact_id in exchange.facts:
                fact = await self._memory.store.fact(fact_id)
                if fact is not None:
                    recorded.append(fact)
        report = ExtractionReport()
        if read:
            last = read[-1]
            origin = Origin(
                actor="orion:extractor",
                source=f"thread:{self.pending.thread_id}",
                trust=min([EXTRACTOR_TRUST, *(e.trust for e in read)]),
                derived_from=tuple(i for e in read for i in e.events),
                correlation_id=last.correlation_id,
            )
            report = await self._extractor.consolidate_many(
                [("\n".join(e.said), "\n".join(e.replies)) for e in read],
                origin,
                self._clock().astimezone(self._zone).date(),
                recorded,
            )
            if report.unavailable:
                return report  # nothing read: the same exchanges are tried at the next tick
        await self._record(exchanges[-1].seq, len(read), report)
        return report

    async def run(self) -> None:
        await self.start()
        while True:
            report = await self.tick()
            if report is not None and report.applied:
                log.info("memory.consolidated", applied=report.applied)
            await asyncio.sleep(CHECK_EVERY)

    async def _record(self, through: int, read: int, report: ExtractionReport) -> None:
        await self._journal.append(
            [
                NewEvent(
                    kind=EXTRACTED,
                    payload={
                        "through": through,
                        "exchanges": read,
                        "applied": report.applied,
                        "rejected": len(report.rejected),
                    },
                    provenance=Provenance(source="orion:memory", actor="orion:extractor"),
                    trust=Trust.SYSTEM,
                )
            ]
        )
        await self._runner.catch_up()
