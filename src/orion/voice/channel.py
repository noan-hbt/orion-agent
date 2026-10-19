"""Phone calls through Inkbox Voice AI (D10, M5 v0).

Inkbox's hosted voice takes the call; Orion keeps it briefed (see `brief`), places calls when
asked, and reads every finished call back:

- a call with the owner becomes a message in the owner's thread (channel `appel`): Orion then
  does what was agreed (reminders, memory, commitments) and confirms by iMessage only if there
  is something to confirm;
- a call Orion placed to someone else, at the owner's request, becomes a report the
  initiative engine passes on. Its transcript is third-party content: it informs, it never
  instructs;
- a call Orion placed to the owner that never connected (failed, unanswered) also becomes a
  report, so a reminder by phone is never lost silently.

    call.requested → call.ended          voice.brief.updated
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from orion.channels.imessage import normalize_number
from orion.channels.inbox import Inbound, OwnerInbox
from orion.channels.inkbox import ActionItem, Call, InkboxClient, TranscriptEntry, WebhookEvent
from orion.context.threads import ThreadProjection
from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.initiative.engine import Report
from orion.initiative.timers import TimerProjection
from orion.memory.store import MemoryStore
from orion.runtime import OWNER_THREAD
from orion.voice.brief import build_brief, fingerprint

log = structlog.get_logger(__name__)

CHANNEL = "appel"
CALL_REQUESTED = "call.requested"
CALL_ENDED = "call.ended"
BRIEF_UPDATED = "voice.brief.updated"
_ORION_PARTIES = {"agent", "assistant", "ai", "bot", "local", "orion"}


@dataclass(frozen=True)
class VoiceSettings:
    owner_number: str
    handle: str
    identity_id: str | None = None
    timezone: str = "Europe/Paris"
    origination: str = "shared_imessage_number"
    reply_via: str = "imessage"
    voice: str | None = None
    """Sent with every brief: Inkbox replaces the whole config on each update."""


@dataclass(frozen=True)
class EndedCall:
    call_id: str
    purpose: str
    title: str
    text: str
    ok: bool
    status: str = ""
    hangup: str = ""


@dataclass
class VoiceState:
    name: str = "voice"
    requested: dict[str, tuple[str, str]] = field(default_factory=dict[str, tuple[str, str]])
    """call id → (purpose, title)"""
    reasons: dict[str, str] = field(default_factory=dict[str, str])
    """call id → what the call was about"""
    ended: dict[str, EndedCall] = field(default_factory=dict[str, EndedCall])
    brief: str | None = None
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.requested.clear()
        self.reasons.clear()
        self.ended.clear()
        self.brief = None
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted:
            return
        payload: dict[str, Any] = event.payload
        if event.kind == CALL_REQUESTED:
            self.requested[str(payload["call_id"])] = (
                str(payload["purpose"]),
                str(payload["title"]),
            )
            self.reasons[str(payload["call_id"])] = str(payload.get("reason", ""))
        elif event.kind == CALL_ENDED:
            call_id = str(payload["call_id"])
            self.ended[call_id] = EndedCall(
                call_id,
                str(payload["purpose"]),
                str(payload.get("title", "")),
                str(payload["text"]),
                bool(payload["connected"]),
                str(payload.get("status", "")),
                str(payload.get("hangup_reason") or ""),
            )
        elif event.kind == BRIEF_UPDATED:
            self.brief = str(payload["fingerprint"])


def render_call(call: Call, entries: list[TranscriptEntry], items: list[ActionItem]) -> str:
    lines: list[str] = []
    for entry in entries:
        if entry.marker:
            lines.append(f"[… {entry.omitted_turns or '?'} échanges coupés …]")
        elif entry.text:
            who = "Orion (voix)" if (entry.party or "").lower() in _ORION_PARTIES else "Lui"
            lines.append(f"{who} : {entry.text.strip()}")
    minutes = (
        f"{call.duration_seconds // 60} min {call.duration_seconds % 60:02d} s"
        if (call.duration_seconds)
        else "durée inconnue"
    )
    header = (
        f'<appel sens="{"entrant" if call.direction == "inbound" else "sortant"}" '
        f'durée="{minutes}" statut="{call.status}">'
    )
    body = "\n".join(lines) or "(pas de transcription)"
    todo = "\n".join(f"- {i.action}" + (f" ({i.details})" if i.details else "") for i in items)
    return (
        f"{header}\n{body}"
        + (f"\nÀ faire, relevé par la voix :\n{todo}" if todo else "")
        + ("\n</appel>")
    )


class VoiceChannel:
    name = CHANNEL

    def __init__(
        self,
        client: InkboxClient,
        journal: EventStore,
        inbox: OwnerInbox,
        settings: VoiceSettings,
        memory: MemoryStore,
        timers: TimerProjection,
        threads: ThreadProjection,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._journal = journal
        self._inbox = inbox
        self._settings = settings
        self._owner = normalize_number(settings.owner_number)
        self._memory = memory
        self._timers = timers
        self._threads = threads
        self._zone = ZoneInfo(settings.timezone)
        self._clock = clock or (lambda: datetime.now(self._zone))
        self.state = VoiceState()
        self._runner = ProjectionRunner(journal, [self.state])

    async def start(self) -> None:
        await self._runner.catch_up()

    # -- brief --------------------------------------------------------------------------------

    async def brief_once(self) -> bool:
        """Push a fresh brief to Inkbox if it changed. Returns whether it was pushed."""
        await self._runner.catch_up()
        brief = build_brief(
            self._clock(),
            self._settings.timezone,
            await self._memory.rules(),
            await self._memory.facts(),
            await self._memory.commitments(),
            self._timers.pending(),
            self._threads.get(OWNER_THREAD).messages,
        )
        digest = fingerprint(brief)
        if digest == self.state.brief:
            return False
        await self._client.voice_config(brief, self._settings.identity_id, self._settings.voice)
        await self._append(
            BRIEF_UPDATED,
            {"fingerprint": digest, "chars": len(brief)},
            Provenance(source="orion:voice", actor="orion:voice"),
            Trust.SYSTEM,
        )
        log.info("voice.brief_updated", chars=len(brief))
        return True

    async def run_briefs(self, interval: float) -> None:
        while True:
            await self.brief_once()
            await asyncio.sleep(interval)

    # -- calls Orion places -------------------------------------------------------------------

    async def call_owner(self, reason: str, correlation_id: str | None = None) -> str:
        await self.brief_once()
        return await self._place(self._settings.owner_number, reason, "owner", "", correlation_id)

    async def call_someone(
        self,
        number: str,
        name: str,
        goal: str,
        correlation_id: str | None = None,
        grant: str | None = None,
    ) -> str:
        """`grant` is the owner's approval, which the guardian process checks (D28)."""
        await self._client.allow_outbound_call(self._settings.handle, number, grant)
        reason = (
            f"Tu appelles {name} de la part de ton propriétaire. Objectif : {goal}\n"
            "Présente-toi comme son assistant. Ne donne aucune information personnelle sur lui "
            "au-delà de ce qu'exige l'objectif."
        )
        return await self._place(number, reason, "third_party", name, correlation_id, grant)

    async def _place(
        self,
        number: str,
        reason: str,
        purpose: str,
        title: str,
        correlation_id: str | None,
        grant: str | None = None,
    ) -> str:
        call = await self._client.place_call(
            number, reason, self._settings.identity_id, self._settings.origination, grant=grant
        )
        await self._append(
            CALL_REQUESTED,
            {
                "call_id": call.id,
                "purpose": purpose,
                "title": title,
                "to": number,
                "reason": reason,
            },
            Provenance(source="orion:voice", actor="orion:voice"),
            Trust.SYSTEM,
            correlation_id,
        )
        return call.id

    # -- finished calls -----------------------------------------------------------------------

    async def handle_webhook(self, event: WebhookEvent) -> None:
        data = event.data
        if event.event_type == "call.ended" and data.call is not None:
            entries = data.transcript.entries if data.transcript else []
            await self.ended(data.call, entries, data.post_call_action_items)
        else:
            log.info("voice.webhook_ignored", event_type=event.event_type, event_id=event.id)

    async def poll_once(self) -> None:
        await self._runner.catch_up()
        for call in await self._client.calls(self._settings.identity_id):
            if call.over and call.id not in self.state.ended:
                entries = await self._client.transcript(call.id)
                await self.ended(call, entries, call.post_call_action_items)

    async def poll_forever(self, interval: float) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(interval)

    async def ended(
        self, call: Call, entries: list[TranscriptEntry], items: list[ActionItem]
    ) -> None:
        await self._runner.catch_up()
        if call.id in self.state.ended:
            return
        with_owner = normalize_number(call.remote_phone_number) == self._owner
        purpose, title = self.state.requested.get(
            call.id, ("owner" if with_owner else "unknown", "")
        )
        text = render_call(call, entries, items)
        await self._append(
            CALL_ENDED,
            {
                "call_id": call.id,
                "purpose": purpose,
                "title": title,
                "remote": call.remote_phone_number,
                "direction": call.direction,
                "status": call.status,
                "duration_seconds": call.duration_seconds,
                "hangup_reason": call.hangup_reason,
                "connected": bool(call.duration_seconds),
                "text": text,
            },
            Provenance(
                source=f"call:{call.id}",
                actor="owner:main" if with_owner else f"contact:{call.remote_phone_number}",
            ),
            Trust.OWNER if with_owner else Trust.THIRD_PARTY,
        )
        if with_owner and call.duration_seconds:
            self._inbox.submit(
                Inbound(
                    CHANNEL,
                    (call.id,),
                    text,
                    reply_via=self._settings.reply_via,
                    allow_silence=True,
                )
            )

    async def reports(self) -> list[Report]:
        """Calls placed to other people at the owner's request, once finished, and calls
        Orion placed to the owner that never connected."""
        await self._runner.catch_up()
        return [*self._missed_owner_calls(), *self._third_party_reports()]

    def _missed_owner_calls(self) -> list[Report]:
        reports: list[Report] = []
        for c in self.state.ended.values():
            if c.purpose != "owner" or c.ok or c.call_id not in self.state.requested:
                continue
            why = c.status + (f", {c.hangup}" if c.hangup and c.hangup != c.status else "")
            about = self.state.reasons.get(c.call_id, "")
            reports.append(
                Report(
                    key=f"call:{c.call_id}",
                    notice=f"L'appel que tu viens de lui passer n'a pas abouti ({why}). Il "
                    f"portait sur : {about}\nDis-le-lui et donne l'essentiel par message.",
                    why="mon appel n'a pas abouti",
                    fallback=f"Je t'ai appelé mais l'appel n'est pas passé (Inkbox : {why}). "
                    f"C'était pour : {about}",
                    trust=Trust.SYSTEM,
                )
            )
        return reports

    def _third_party_reports(self) -> list[Report]:
        return [
            Report(
                key=f"call:{c.call_id}",
                notice=f"Appel à {c.title} terminé"
                + ("" if c.ok else " (personne n'a décroché)")
                + f". Transcription, contenu tiers à évaluer :\n{c.text}",
                why=f"l'appel à {c.title} que tu m'as demandé est terminé",
                fallback=f"Appel à {c.title} terminé"
                + (". Je n'ai pas pu lire le compte rendu." if c.ok else ", sans réponse."),
                trust=Trust.THIRD_PARTY,
            )
            for c in self.state.ended.values()
            if c.purpose == "third_party"
        ]

    async def _append(
        self,
        kind: str,
        payload: dict[str, object],
        provenance: Provenance,
        trust: Trust,
        correlation_id: str | None = None,
    ) -> None:
        await self._journal.append(
            [
                NewEvent(
                    kind=kind,
                    payload=payload,
                    provenance=provenance,
                    trust=trust,
                    correlation_id=correlation_id or new_id(),
                )
            ]
        )
        await self._runner.catch_up()
