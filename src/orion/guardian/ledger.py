"""The guardian's own ledger: an append-only journal only the guardian process writes.

It records what the guardian must know without asking the core: which conversations are the
owner's, which approvals it sent and which were used, what it refused, and (federation) its
invitations, peers and the signed messages it exchanged. Its state is a projection, rebuilt
from the ledger on every start, like the core's.

    guardian.owner_conversation  guardian.approval.requested  guardian.approval.used
    guardian.denied  guardian.core.silent  guardian.core.back
    guardian.calendar.written  guardian.mail.sent
    guardian.autonomy.granted  guardian.autonomy.revoked  guardian.autonomous
    guardian.session.added  guardian.session.removed  guardian.session.lent
    guardian.login.added  guardian.login.removed  guardian.login.lent  guardian.login.code
    guardian.telegram.answer  guardian.app.answer
    federation.invite.created  federation.invite.used  federation.peer.added
    federation.peer.revoked  federation.received  federation.sent
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner

OWNER_CONVERSATION = "guardian.owner_conversation"
APPROVAL_REQUESTED = "guardian.approval.requested"
APPROVAL_USED = "guardian.approval.used"
DENIED = "guardian.denied"
CORE_SILENT = "guardian.core.silent"
CORE_BACK = "guardian.core.back"
WRITTEN = "guardian.calendar.written"
MAIL_SENT = "guardian.mail.sent"
AUTONOMY_GRANTED = "guardian.autonomy.granted"
AUTONOMY_REVOKED = "guardian.autonomy.revoked"
AUTONOMOUS = "guardian.autonomous"
SESSION_ADDED = "guardian.session.added"
SESSION_REMOVED = "guardian.session.removed"
SESSION_LENT = "guardian.session.lent"
LOGIN_ADDED = "guardian.login.added"
LOGIN_REMOVED = "guardian.login.removed"
LOGIN_LENT = "guardian.login.lent"
LOGIN_CODE = "guardian.login.code"
TELEGRAM_ANSWER = "guardian.telegram.answer"
APP_ANSWER = "guardian.app.answer"
CONFIG_CHANGED = "guardian.config.changed"
INVITE_CREATED = "federation.invite.created"
INVITE_USED = "federation.invite.used"
PEER_ADDED = "federation.peer.added"
PEER_REVOKED = "federation.peer.revoked"
RECEIVED = "federation.received"
SENT = "federation.sent"


@dataclass(frozen=True)
class PendingApproval:
    id: str
    action: str
    params: dict[str, Any]
    message_id: str
    conversation_id: str
    expires_at: datetime
    used: bool = False


@dataclass(frozen=True)
class Invite:
    token_hash: str
    label: str
    expires_at: datetime
    used: bool = False


@dataclass(frozen=True)
class Peer:
    instance: str
    label: str
    """How the owner names the other person (e.g. 'Paul')."""
    public_key: str
    url: str
    skills: tuple[str, ...]
    active: bool = True


@dataclass(frozen=True)
class Exchanged:
    """A signed federation message, as sent or received (`seq` orders the inbox)."""

    seq: int
    direction: str
    envelope: dict[str, Any]


@dataclass
class GuardianState:
    name: str = "guardian"
    owner_conversations: set[str] = field(default_factory=set[str])
    approvals: dict[str, PendingApproval] = field(default_factory=dict[str, PendingApproval])
    invites: dict[str, Invite] = field(default_factory=dict[str, Invite])
    peers: dict[str, Peer] = field(default_factory=dict[str, Peer])
    exchanged: list[Exchanged] = field(default_factory=list[Exchanged])
    envelope_ids: set[str] = field(default_factory=set[str])
    written_meetings: set[str] = field(default_factory=set[str])
    autonomous: set[str] = field(default_factory=set[str])
    autonomous_days: dict[str, int] = field(default_factory=dict[str, int])
    """'YYYY-MM-DD' → actions done alone that day."""
    limits: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    """action → the limits its autonomy was granted with (`guardian.limits`)."""
    sessions: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    """Logged-in site sessions the guardian keeps: name → the sites they are for."""
    logins: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    """The owner's logins the guardian keeps (D111): name → the sites they are for."""
    logins_totp: set[str] = field(default_factory=set[str])
    """The logins whose 2FA secret it keeps too."""
    logins_lent_for: set[str] = field(default_factory=set[str])
    """The approvals a login was already lent for."""
    telegram_answers: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    """The owner's answers to the guardian's Telegram questions, as they passed (D89)."""
    telegram_updates: set[int] = field(default_factory=set[int])
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.owner_conversations.clear()
        self.approvals.clear()
        self.invites.clear()
        self.peers.clear()
        self.exchanged.clear()
        self.envelope_ids.clear()
        self.written_meetings.clear()
        self.autonomous.clear()
        self.autonomous_days.clear()
        self.limits.clear()
        self.sessions.clear()
        self.logins.clear()
        self.logins_totp.clear()
        self.logins_lent_for.clear()
        self.telegram_answers.clear()
        self.telegram_updates.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted:
            return
        payload: dict[str, Any] = event.payload
        kind = event.kind
        if kind == AUTONOMY_GRANTED:
            self.autonomous.add(str(payload["action"]))
            self.limits[str(payload["action"])] = dict(payload.get("limits") or {})
        elif kind == AUTONOMY_REVOKED:
            self.autonomous.discard(str(payload["action"]))
            self.limits.pop(str(payload["action"]), None)
        elif kind == SESSION_ADDED:
            self.sessions[str(payload["name"])] = [str(d) for d in payload["domains"]]
        elif kind == SESSION_REMOVED:
            self.sessions.pop(str(payload["name"]), None)
        elif kind == LOGIN_ADDED:
            name = str(payload["name"])
            self.logins[name] = [str(d) for d in payload["domains"]]
            if payload.get("totp"):
                self.logins_totp.add(name)
            else:
                self.logins_totp.discard(name)
        elif kind == LOGIN_REMOVED:
            self.logins.pop(str(payload["name"]), None)
            self.logins_totp.discard(str(payload["name"]))
        elif kind == LOGIN_LENT:
            self.logins_lent_for.add(str(payload["how"]))
        elif kind == TELEGRAM_ANSWER:
            self.telegram_updates.add(int(payload["update_id"]))
            self.telegram_answers.append(dict(payload))
        elif kind == AUTONOMOUS:
            day = str(payload["day"])
            self.autonomous_days[day] = self.autonomous_days.get(day, 0) + 1
        if kind == WRITTEN and payload.get("meeting"):
            self.written_meetings.add(str(payload["meeting"]))
        if kind == OWNER_CONVERSATION:
            self.owner_conversations.add(str(payload["conversation_id"]))
        elif kind == APPROVAL_REQUESTED:
            approval = PendingApproval(
                str(payload["approval_id"]),
                str(payload["action"]),
                dict(payload["params"]),
                str(payload["message_id"]),
                str(payload["conversation_id"]),
                datetime.fromisoformat(str(payload["expires_at"])),
            )
            self.approvals[approval.id] = approval
        elif kind == APPROVAL_USED:
            found = self.approvals.get(str(payload["approval_id"]))
            if found is not None:
                self.approvals[found.id] = _used(found)
        elif kind == INVITE_CREATED:
            invite = Invite(
                str(payload["token_hash"]),
                str(payload["label"]),
                datetime.fromisoformat(str(payload["expires_at"])),
            )
            self.invites[invite.token_hash] = invite
        elif kind == INVITE_USED:
            found_invite = self.invites.get(str(payload["token_hash"]))
            if found_invite is not None:
                self.invites[found_invite.token_hash] = Invite(
                    found_invite.token_hash, found_invite.label, found_invite.expires_at, True
                )
        elif kind == PEER_ADDED:
            peer = Peer(
                str(payload["instance"]),
                str(payload["label"]),
                str(payload["public_key"]),
                str(payload["url"]),
                tuple(str(s) for s in payload["skills"]),
            )
            self.peers[peer.instance] = peer
        elif kind == PEER_REVOKED:
            known = self.peers.get(str(payload["instance"]))
            if known is not None:
                self.peers[known.instance] = Peer(
                    known.instance, known.label, known.public_key, known.url, known.skills, False
                )
        elif kind in (RECEIVED, SENT):
            envelope = dict(payload["envelope"])
            self.envelope_ids.add(str(envelope["id"]))
            direction = "in" if kind == RECEIVED else "out"
            self.exchanged.append(Exchanged(event.seq, direction, envelope))

    def peer(self, instance: str) -> Peer | None:
        found = self.peers.get(instance)
        return found if found is not None and found.active else None


def _used(approval: PendingApproval) -> PendingApproval:
    return PendingApproval(
        approval.id,
        approval.action,
        approval.params,
        approval.message_id,
        approval.conversation_id,
        approval.expires_at,
        used=True,
    )


class Ledger:
    """The ledger and its state, kept caught up."""

    def __init__(self, journal: EventStore) -> None:
        self.journal = journal
        self.state = GuardianState()
        self._runner = ProjectionRunner(journal, [self.state])

    async def catch_up(self) -> None:
        await self._runner.catch_up()

    async def record(
        self,
        kind: str,
        payload: dict[str, object],
        trust: Trust = Trust.SYSTEM,
        source: str = "orion:guardian",
    ) -> None:
        await self.journal.append(
            [
                NewEvent(
                    kind=kind,
                    payload=payload,
                    provenance=Provenance(source=source, actor="orion:guardian"),
                    trust=trust,
                    correlation_id=new_id(),
                )
            ]
        )
        await self._runner.catch_up()
