"""Attention: learn from what the owner actually reads, without asking a model.

A digest slot whose last few deliveries all went unanswered is dropped from the schedule. The
change is a journaled setting with its reason, shown to the model in its memory (so Orion can
say why it stopped), and the owner can bring the slot back with one sentence.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from orion.adaptation.signals import engagement
from orion.core.events import Event
from orion.initiative.engine import DELIVERED as INITIATIVE_DELIVERED

IGNORED_STREAK = 5


@dataclass(frozen=True)
class MuteDecision:
    slot: time
    evidence: tuple[str, ...]
    reason: str


def slot_of(key: str) -> time | None:
    """'digest:2026-10-20:13:00' → 13:00."""
    parts = key.split(":")
    if len(parts) != 4 or parts[0] != "digest":
        return None
    try:
        return time(int(parts[2]), int(parts[3]))
    except ValueError:
        return None


def ignored_slots(
    events: Sequence[Event],
    digests: Sequence[time],
    zone: ZoneInfo,
    now: datetime,
    since_seq: int = 0,
    streak: int = IGNORED_STREAK,
) -> list[MuteDecision]:
    """Digest slots whose last `streak` deliveries after `since_seq` (the last time the owner or
    Orion changed the schedule) all went unanswered."""
    engaged = engagement(events, zone, now)
    per_slot: dict[time, list[tuple[str, bool | None]]] = {}
    for e in events:
        if e.kind != INITIATIVE_DELIVERED or e.redacted or e.seq <= since_seq:
            continue
        slot = slot_of(str(e.payload.get("key", "")))
        if slot is not None and slot in digests:
            per_slot.setdefault(slot, []).append((e.id, engaged.get(e.id)))
    decisions: list[MuteDecision] = []
    for slot, deliveries in sorted(per_slot.items()):
        settled = [(event_id, ok) for event_id, ok in deliveries if ok is not None]
        recent = settled[-streak:]
        if len(recent) == streak and not any(ok for _, ok in recent):
            decisions.append(
                MuteDecision(
                    slot,
                    tuple(event_id for event_id, _ in recent),
                    f"les {streak} derniers points de {slot:%H:%M} sont restés sans réponse",
                )
            )
    return decisions
