"""Provenance put to work: what depends on a source, and why Orion knows what it knows.

**Containment.** A mail or a page turns out to be an injection: what did it touch? Orion traces
it like contact tracing. The source's own events (its card, a commitment read from it); the
turns where it reached the model (a tool result naming it); every later turn of that window,
until the conversation started afresh, since the model still had it in view; and whatever was
derived from all of those. Containing invalidates what is still live (facts, commitments,
reminders, playbook rules) with the reason, and lists what already left Orion, which cannot be
undone. Unlike forgetting (`MemoryService.forget_source`), nothing is erased: the owner can
still see what happened.

**Audit.** For any fact or commitment: where it came from, who wrote it, at what trust, and
the message or document it was derived from.

    provenance.contained
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import structlog

from orion.context.threads import MESSAGE_KIND, REBASED_KIND
from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.journal import EventStore
from orion.initiative.timers import TIMER_SCHEDULED, Timers
from orion.memory.model import (
    COMMITMENT_CREATED,
    FACT_ASSERTED,
    RULE_LEARNED,
    CommitmentStatus,
)
from orion.memory.service import MemoryService, Origin

log = structlog.get_logger(__name__)

CONTAINED = "provenance.contained"
TURN_STARTS = ("message.received", "initiative.triggered")
EFFECTS = ("outbox.delivered", "mail.sent", "calendar.event.added", "job.started")
"""What leaves Orion (or starts something that does): reported, it cannot be taken back."""
_ID_FIELDS = {
    FACT_ASSERTED: "fact_id",
    COMMITMENT_CREATED: "commitment_id",
    TIMER_SCHEDULED: "timer_id",
    RULE_LEARNED: "rule_id",
}


@dataclass
class Trace:
    source: str
    origins: list[Event] = field(default_factory=list[Event])
    turns: list[str] = field(default_factory=list[str])
    """Correlation ids of the turns that had the source in view."""
    live: dict[str, list[tuple[str, Event]]] = field(
        default_factory=lambda: defaultdict[str, list[tuple[str, Event]]](list)
    )
    """kind → (item id, the event that created it), for what can still be invalidated."""
    gone: list[Event] = field(default_factory=list[Event])
    """What already left Orion."""

    @property
    def empty(self) -> bool:
        return not self.origins

    def describe(self) -> str:
        if self.empty:
            return f"Rien ne vient de {self.source}."
        names = {
            FACT_ASSERTED: "souvenir(s)",
            COMMITMENT_CREATED: "engagement(s)",
            TIMER_SCHEDULED: "rappel(s)",
            RULE_LEARNED: "règle(s) du manuel",
        }
        parts = [f"{len(items)} {names[kind]}" for kind, items in self.live.items() if items]
        lines = [
            f"Source {self.source} : {len(self.origins)} événement(s) d'origine, "
            f"{len(self.turns)} tour(s) de conversation où elle était visible.",
            "Dépend d'elle : " + (", ".join(parts) if parts else "rien en mémoire"),
        ]
        if self.gone:
            lines.append(
                f"Déjà parti (impossible à reprendre) : {len(self.gone)} — "
                + "; ".join(sorted({e.kind for e in self.gone}))
            )
        return "\n".join(lines)


def _markers(origins: list[Event], source: str) -> set[str]:
    """How the source shows up in what the model saw: its card id, its address, itself."""
    found = {source}
    for event in origins:
        for key in ("card_id", "url", "final_url", "envelope_id"):
            value = event.payload.get(key)
            if isinstance(value, str) and len(value) > 6:
                found.add(value)
    return found


def _mentions(event: Event, markers: set[str]) -> bool:
    text = str(event.payload)
    return any(marker in text for marker in markers)


def trace(events: list[Event], source: str) -> Trace:
    result = Trace(source)
    live = [e for e in events if not e.redacted]
    result.origins = [e for e in live if e.provenance.source.startswith(source)]
    if result.empty:
        return result
    markers = _markers(result.origins, source)

    # The turns that saw it, then the rest of each window, until the thread was rebased.
    seen: set[str] = set()
    for e in live:
        if e.correlation_id and (
            (e.kind == MESSAGE_KIND and _mentions(e, markers))
            or (e.kind == "initiative.triggered" and _mentions(e, markers))
        ):
            seen.add(e.correlation_id)
    tainted: set[str] = set()
    open_threads: set[str] = set()
    for e in live:
        thread = str(e.payload.get("thread_id", ""))
        if e.kind == REBASED_KIND:
            open_threads.discard(thread)
        if e.kind in TURN_STARTS and e.correlation_id:
            if e.correlation_id in seen:
                open_threads.add(thread)
            if thread in open_threads:
                tainted.add(e.correlation_id)
    tainted |= seen
    result.turns = sorted(tainted)

    # Everything derived: written in those turns below full trust, or linked to what was.
    doomed = {e.id for e in result.origins}
    doomed |= {
        e.id
        for e in live
        if e.correlation_id in tainted
        and e.trust < Trust.SYSTEM
        and (e.kind in _ID_FIELDS or e.kind in EFFECTS)
    }
    changed = True
    while changed:
        changed = False
        for e in live:
            if e.id in doomed:
                continue
            links: set[str] = set(e.provenance.derived_from)
            if e.causation_id:
                links.add(e.causation_id)
            if links & doomed or (e.correlation_id in tainted and e.kind in EFFECTS):
                doomed.add(e.id)
                changed = True
    for e in live:
        if e.id not in doomed:
            continue
        if e.kind in _ID_FIELDS:
            result.live[e.kind].append((str(e.payload[_ID_FIELDS[e.kind]]), e))
        elif e.kind in EFFECTS:
            result.gone.append(e)
    return result


class Provenances:
    def __init__(self, journal: EventStore, memory: MemoryService, timers: Timers) -> None:
        self._journal = journal
        self._memory = memory
        self._timers = timers

    async def _events(self) -> list[Event]:
        events: list[Event] = []
        cursor = 0
        while batch := await self._journal.read(cursor, 1000):
            events.extend(batch)
            cursor = batch[-1].seq
        return events

    async def trace(self, source: str) -> Trace:
        return trace(await self._events(), source)

    async def contain(self, source: str, reason: str, actor: str) -> Trace:
        """Invalidate what still depends on `source` (facts, commitments, reminders, rules)."""
        found = await self.trace(source)
        if found.empty:
            return found
        origin = Origin(actor=actor, source="provenance:containment", trust=Trust.OWNER)
        why = f"dépend de {source}, mis en quarantaine : {reason}"[:300]
        facts = {f.id for f in await self._memory.store.facts()}
        commitments = {c.id for c in await self._memory.store.commitments()}
        rules = {r.id for r in await self._memory.store.rules()}
        await self._timers.sync()
        timers = {t.id for t in self._timers.projection.pending()}
        for item, _ in found.live.get(FACT_ASSERTED, []):
            if item in facts:
                await self._memory.invalidate(item, why, origin)
        for item, _ in found.live.get(COMMITMENT_CREATED, []):
            if item in commitments:
                await self._memory.close_commitment(
                    item, CommitmentStatus.CANCELLED, origin, evidence=why
                )
        for item, _ in found.live.get(RULE_LEARNED, []):
            if item in rules:
                await self._memory.retire_rule(item, why, origin)
        for item, _ in found.live.get(TIMER_SCHEDULED, []):
            if item in timers:
                await self._timers.cancel(
                    item, why, Provenance(source="provenance:containment", actor=actor)
                )
        await self._journal.append(
            [
                NewEvent(
                    kind=CONTAINED,
                    payload={
                        "source": source,
                        "reason": reason,
                        "turns": found.turns,
                        "live": {k: [i for i, _ in v] for k, v in found.live.items()},
                        "gone": [e.id for e in found.gone],
                    },
                    provenance=Provenance(source="provenance:containment", actor=actor),
                    trust=Trust.OWNER,
                )
            ]
        )
        log.warning("provenance.contained", source=source, turns=len(found.turns))
        return found

    async def lineage(self, item_id: str, depth: int = 4) -> list[str]:
        """Where a fact or commitment comes from, step by step back to its source."""
        events = await self._events()
        by_id = {e.id: e for e in events}
        start = next(
            (
                e
                for e in events
                if e.kind in _ID_FIELDS and e.payload.get(_ID_FIELDS[e.kind]) == item_id
            ),
            None,
        )
        if start is None:
            return []
        lines: list[str] = []
        current: Event | None = start
        for _ in range(depth):
            if current is None:
                break
            lines.append(_line(current))
            parent = (current.provenance.derived_from or (current.causation_id,))[0]
            current = by_id.get(parent) if parent else None
        return lines


def _line(event: Event) -> str:
    payload: dict[str, Any] = event.payload
    text = (
        payload.get("statement")
        or payload.get("what")
        or payload.get("text")
        or payload.get("rule")
        or payload.get("subject")
        or ""
    )
    return (
        f"{event.kind} — source {event.provenance.source}, écrit par {event.provenance.actor}, "
        f"confiance {event.trust.name}" + (f" : « {str(text)[:160]} »" if text else "")
    )


def provenance_tools(provenances: Provenances) -> list[Any]:
    from pydantic import BaseModel, ConfigDict, Field

    from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

    class LineageInput(BaseModel):
        model_config = ConfigDict(extra="forbid")

        item: str = Field(description="A fact (f_…) or commitment (c_…) id")

    class ContainInput(BaseModel):
        model_config = ConfigDict(extra="forbid")

        source: str = Field(
            min_length=4,
            description="The suspicious source, as it appears in provenance: "
            "'mail:<account>:<message id>', 'web:<address>', 'peer:<instance>'",
        )
        apply: bool = Field(
            default=False, description="False: only report what depends on it. True: contain"
        )
        reason: str = Field(default="source suspecte", max_length=200)

    async def lineage(args: Any, _ctx: ToolContext) -> ToolResult:
        lines = await provenances.lineage(args.item)
        if not lines:
            return ToolResult(f"No fact or commitment {args.item}.", is_error=True)
        return ToolResult("D'où ça vient :\n" + "\n".join(f"- {line}" for line in lines))

    async def contain(args: Any, _ctx: ToolContext) -> ToolResult:
        if args.apply:
            found = await provenances.contain(args.source, args.reason, "owner:main")
            return ToolResult(
                ("Mis en quarantaine.\n" if not found.empty else "") + found.describe()
            )
        return ToolResult((await provenances.trace(args.source)).describe())

    return [
        Tool(
            name="provenance",
            description="Where a fact or commitment comes from: source, who wrote it, trust, "
            "and what it was derived from. For « comment tu sais ça ? ».",
            input_model=LineageInput,
            handler=lineage,
            effect=Effect.NONE,
            reversibility=Reversibility.REVERSIBLE,
            concurrency_safe=True,
            accepts_untrusted_context=True,
        ),
        Tool(
            name="contain_source",
            description="When a mail, page or other Orion turns out to be malicious: list "
            "everything that depends on it (memories, commitments, reminders, rules, what was "
            "sent), then, with apply, invalidate what is still live. Report first, then ask.",
            input_model=ContainInput,
            handler=contain,
            effect=Effect.INTERNAL,
            reversibility=Reversibility.REVERSIBLE,
        ),
    ]
