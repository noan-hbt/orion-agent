"""Signals about the owner, read back from the journal.

Everything Orion learns about how to work with its owner comes from three kinds of evidence,
all of them already journaled:

- what the owner writes, next to what Orion answered;
- tapbacks the owner puts on Orion's messages;
- whether the owner engaged after Orion spoke first (a message or a reaction within a few
  hours).

Only the owner's own events count as evidence: content from third parties never shapes how
Orion behaves.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from orion.context.threads import MESSAGE_KIND
from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.hlc import Timestamp
from orion.core.ids import new_id
from orion.guardian.outbox import DELIVERED as OUTBOX_DELIVERED
from orion.guardian.outbox import QUEUED as OUTBOX_QUEUED
from orion.initiative.engine import DELIVERED as INITIATIVE_DELIVERED

FEEDBACK_REACTION = "feedback.reaction"
OWNER_MESSAGE = "message.received"
OWNER_ACTOR = "owner:main"
ENGAGEMENT_WINDOW = timedelta(hours=3)

_REACTION_FR = {
    "love": "❤️",
    "like": "👍",
    "dislike": "👎",
    "laugh": "😂",
    "emphasize": "‼️",
    "question": "❓",
    "eyes": "👀",
}


@dataclass(frozen=True)
class Signal:
    id: str
    """Journal event id: what a learned rule cites as evidence."""
    kind: Literal["message", "reaction", "initiative"]
    at: datetime
    text: str
    reply: str | None = None
    engaged: bool | None = None
    key: str | None = None


def event_time(event: Event, zone: ZoneInfo) -> datetime:
    wall_ms = Timestamp.decode(event.hlc).wall_ms
    return datetime.fromtimestamp(wall_ms / 1000, zone)


def reaction_event(
    remote_id: str, reaction: str, emoji: str | None, reaction_id: str, source: str
) -> NewEvent:
    return NewEvent(
        kind=FEEDBACK_REACTION,
        payload={
            "remote_id": remote_id,
            "reaction": reaction,
            "emoji": emoji,
            "reaction_id": reaction_id,
        },
        provenance=Provenance(source=source, actor=OWNER_ACTOR),
        trust=Trust.OWNER,
        correlation_id=new_id(),
    )


def _is_owner_engagement(event: Event) -> bool:
    if event.redacted:
        return False
    if event.kind == FEEDBACK_REACTION:
        return True
    return event.kind == OWNER_MESSAGE and event.provenance.actor == OWNER_ACTOR


def engagement(events: Sequence[Event], zone: ZoneInfo, now: datetime) -> dict[str, bool | None]:
    """For each proactive message: did the owner engage with it? A message or tapback counts
    for the latest proactive message sent before it, within a few hours. None: too early."""
    sent: list[tuple[datetime, str]] = []
    result: dict[str, bool | None] = {}
    for e in events:
        if e.redacted:
            continue
        at = event_time(e, zone)
        if e.kind == INITIATIVE_DELIVERED:
            sent.append((at, e.id))
            result[e.id] = None if now - at < ENGAGEMENT_WINDOW else False
        elif _is_owner_engagement(e) and sent:
            last_at, last_id = sent[-1]
            if at - last_at <= ENGAGEMENT_WINDOW:
                result[last_id] = True
    return result


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def collect(
    events: Sequence[Event], zone: ZoneInfo, now: datetime, after_seq: int = 0
) -> list[Signal]:
    """Owner signals after `after_seq`, oldest first. `events` must be the whole journal (older
    events are needed to know which message a reaction was about)."""
    replies: dict[str, str] = {}
    queued_text: dict[str, str] = {}
    remote_text: dict[str, str] = {}
    for e in events:
        if e.redacted:
            continue
        payload: dict[str, Any] = e.payload
        if e.kind == MESSAGE_KIND and e.correlation_id:
            message: dict[str, Any] = payload.get("message", {})
            content = message.get("content")
            if message.get("role") == "assistant" and isinstance(content, str) and content:
                replies[e.correlation_id] = content
        elif e.kind == OUTBOX_QUEUED:
            queued_text[str(payload["outbox_id"])] = str(payload["text"])
        elif e.kind == OUTBOX_DELIVERED and "remote_id" in payload:
            text = queued_text.get(str(payload["outbox_id"]))
            if text is not None:
                remote_text[str(payload["remote_id"])] = text

    engaged = engagement(events, zone, now)
    signals: list[Signal] = []
    for e in events:
        if e.seq <= after_seq or e.redacted:
            continue
        payload = e.payload
        at = event_time(e, zone)
        if e.kind == OWNER_MESSAGE and e.provenance.actor == OWNER_ACTOR:
            reply = replies.get(e.correlation_id or "")
            signals.append(
                Signal(
                    e.id,
                    "message",
                    at,
                    _clip(str(payload.get("text", "")), 600),
                    reply=_clip(reply, 400) if reply else None,
                )
            )
        elif e.kind == FEEDBACK_REACTION:
            reaction = str(payload.get("reaction"))
            mark = payload.get("emoji") or _REACTION_FR.get(reaction, reaction)
            about = remote_text.get(str(payload.get("remote_id")), "(message inconnu)")
            signals.append(Signal(e.id, "reaction", at, f"{mark} sur : {_clip(about, 300)}"))
        elif e.kind == INITIATIVE_DELIVERED:
            key = str(payload.get("key"))
            signals.append(
                Signal(
                    e.id,
                    "initiative",
                    at,
                    f"Orion a écrit de lui-même ({key}) : {payload.get('why')}",
                    engaged=engaged.get(e.id),
                    key=key,
                )
            )
    return signals
