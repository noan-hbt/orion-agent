"""The guardian's federation edge (D29): the only place that holds the instance key, talks to
other Orions, and decides who they are.

- **Pairing** happens between people: one owner creates an invitation (`orion federation
  invite`), gives the code to the other in person or by message, and the other's Orion
  presents itself with it. An invitation works once, for a few days. Nobody else can even
  start a conversation: an unknown sender is refused before anything reads its message.
- **Inbound**: signature checked against the pinned key of an active peer, recipient, clock
  (±10 min), replay (each id once), type allowed by the relationship, body well-formed, a
  quota per peer. What passes is kept in the ledger; the core collects it.
- **Outbound**: signed here, sent only to a paired peer's pinned address, and only if the
  message is one the owner agreed to: a proposal or an acceptance needs the owner's approval,
  checked like any other (D28); a confirmation must match the proposal the owner approved and
  the acceptance received; a refusal reveals nothing and may answer a proposal received.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from orion.channels.inkbox import GRANT_HEADER
from orion.core.events import Trust
from orion.core.ids import new_id
from orion.federation.crypto import Identity, fingerprint, verify
from orion.federation.wire import (
    ACCEPT,
    CONFIRM,
    DECLINE,
    INTRODUCE,
    PROPOSE,
    SCHEDULING,
    Envelope,
    Introduce,
    PeerCard,
    Propose,
    WireError,
    a2a_error,
    clean_title,
    a2a_result,
    agent_card,
    decode_invite,
    encode_invite,
    from_a2a,
    parse_body,
    to_a2a,
)
from orion.guardian.ledger import (
    INVITE_CREATED,
    INVITE_USED,
    PEER_ADDED,
    PEER_REVOKED,
    RECEIVED,
    SENT,
    GuardianState,
    Peer,
)
from orion.guardian.service import GuardianService, refused
from orion.initiative.tools import describe_moment

log = structlog.get_logger(__name__)

ADMIN_HEADER = "X-Orion-Admin"
CLOCK_SKEW = timedelta(minutes=10)
MAX_BODY = 16_000


class SendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    peer: str
    type: str
    context: str = Field(min_length=1, max_length=64)
    body: dict[str, Any]


class InviteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=60)


class AcceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    label: str = Field(min_length=1, max_length=60)


class RevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    peer: str


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _safe_url(url: str) -> bool:
    """Peers are reached over HTTPS (plain HTTP only on this machine, for tests and setup)."""
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return True
    return parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost")


class FederationEdge:
    def __init__(
        self,
        service: GuardianService,
        identity: Identity,
        admin_token: str,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.service = service
        self.identity = identity
        self.admin_token = admin_token
        self.config = service.config
        self.settings = service.config.federation
        self._http = http or httpx.AsyncClient(timeout=20.0)
        self._zone = ZoneInfo(service.config.agent.timezone)
        self._inbound: defaultdict[str, deque[float]] = defaultdict(deque)
        self.card = PeerCard(
            instance=self.config.instance,
            owner_name=self.settings.owner_name,
            public_key=identity.public_key,
            url=self.settings.public_url.rstrip("/") + "/a2a",
        )

    @property
    def state(self) -> GuardianState:
        return self.service.ledger.state

    def install(self) -> None:
        self.service.routes += [
            Route("/v1/federation/identity", self._identity, methods=["GET"]),
            Route("/v1/federation/peers", self._peers, methods=["GET"]),
            Route("/v1/federation/inbox", self._inbox, methods=["GET"]),
            Route("/v1/federation/send", self._send, methods=["POST"]),
            Route("/v1/federation/invites", self._invite, methods=["POST"]),
            Route("/v1/federation/accept", self._accept, methods=["POST"]),
            Route("/v1/federation/revoke", self._revoke, methods=["POST"]),
        ]
        self.service.listeners.append(
            ("federation", self.public_app(), self.settings.listen_host, self.settings.listen_port)
        )
        self.service.renderers["meeting_propose"] = self._render_propose
        self.service.renderers["meeting_accept"] = self._render_accept

    def public_app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/.well-known/agent-card.json", self._card, methods=["GET"]),
                Route("/a2a", self._receive, methods=["POST"]),
            ]
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- who is who ---------------------------------------------------------------------------

    def find(self, name: str) -> Peer | None:
        """An active peer, by instance or by the owner's name for them."""
        wanted = name.strip().casefold()
        for peer in self.state.peers.values():
            if peer.active and wanted in (peer.instance.casefold(), peer.label.casefold()):
                return peer
        return None

    def _moment(self, value: Any) -> datetime | None:
        try:
            moment = datetime.fromisoformat(str(value))
        except ValueError:
            return None
        return moment if moment.tzinfo else moment.replace(tzinfo=self._zone)

    def _human(self, value: Any) -> str:
        moment = self._moment(value)
        return describe_moment(moment, self.config.agent.timezone) if moment else str(value)

    def _render_propose(self, params: dict[str, Any]) -> str:
        peer = self.find(str(params.get("peer", "")))
        who = peer.label if peer else params.get("peer")
        return (
            f"Avant que je le fasse : proposer à l'Orion de {who} « {params.get('title')} » "
            f"({params.get('duration_minutes')} min), entre {self._human(params.get('window_start'))}"
            f" et {self._human(params.get('window_end'))}. Je lui donne tes créneaux libres dans "
            "cette fenêtre, rien d'autre de ton agenda."
        )

    def _render_accept(self, params: dict[str, Any]) -> str:
        peer = self.find(str(params.get("peer", "")))
        proposal = self._received_proposal(peer, str(params.get("meeting_id", "")))
        what = (
            f"« {proposal.title} » ({proposal.duration_minutes} min)" if proposal else "un rendez-vous"
        )
        who = peer.label if peer else params.get("peer")
        return (
            f"L'Orion de {who} propose {what}. Tu es libre {self._human(params.get('slot'))} : "
            "je confirme ce créneau ?"
        )

    # -- core-facing (Unix socket) ------------------------------------------------------------

    def _admin(self, request: Request) -> bool:
        given = request.headers.get(ADMIN_HEADER, "")
        return bool(given) and secrets.compare_digest(given, self.admin_token)

    async def _identity(self, _request: Request) -> Response:
        return JSONResponse(
            {
                "instance": self.card.instance,
                "fingerprint": self.identity.fingerprint,
                "card": agent_card(self.card),
            }
        )

    async def _peers(self, _request: Request) -> Response:
        await self.service.ledger.catch_up()
        return JSONResponse(
            [
                {"instance": p.instance, "label": p.label, "skills": list(p.skills)}
                for p in self.state.peers.values()
                if p.active
            ]
        )

    async def _inbox(self, request: Request) -> Response:
        await self.service.ledger.catch_up()
        after = int(request.query_params.get("after", "0"))
        return JSONResponse(
            {
                "items": [
                    {"seq": item.seq, "envelope": item.envelope}
                    for item in self.state.exchanged
                    if item.direction == "in" and item.seq > after
                ]
            }
        )

    async def _send(self, request: Request) -> Response:
        try:
            asked = SendRequest.model_validate_json(await request.body())
        except ValidationError:
            return refused("malformed send request", 400)
        peer = self.find(asked.peer)
        what = f"federation {asked.type} to {asked.peer}"
        if peer is None:
            return await self.service.deny(what, f"{asked.peer} is not a paired Orion")
        if SCHEDULING not in peer.skills:
            return await self.service.deny(what, "this relationship does not allow scheduling")
        try:
            body = parse_body(asked.type, asked.body)
        except WireError as exc:
            return await self.service.deny(what, str(exc))
        reason = await self._outbound_policy(
            peer, asked.type, asked.context, body, request.headers.get(GRANT_HEADER)
        )
        if reason is not None:
            return await self.service.deny(what, reason)
        try:
            envelope = await self.deliver(peer, asked.type, asked.context, body.model_dump(mode="json"))
        except (httpx.HTTPError, WireError) as exc:
            log.warning("federation.send_failed", peer=peer.instance, error=str(exc))
            return refused(f"{peer.label}'s Orion did not take the message: {exc}", 502)
        return JSONResponse({"id": envelope.id, "signature": envelope.signature})

    async def _outbound_policy(
        self, peer: Peer, kind: str, context: str, body: Any, grant: str | None
    ) -> str | None:
        if kind == PROPOSE:
            return await self.service.use_grant(
                grant,
                "meeting_propose",
                lambda p: self._matches_proposal(p, peer, body),
                consume=True,
            )
        if kind == ACCEPT:
            proposal = self._received_proposal(peer, context)
            if proposal is None or body.slot not in proposal.slots:
                return "there is no such proposal from this peer, or not that slot"
            return await self.service.use_grant(
                grant,
                "meeting_accept",
                lambda p: self.find(str(p.get("peer", ""))) == peer
                and p.get("meeting_id") == context
                and self._moment(p.get("slot")) == body.slot,
                consume=True,
            )
        if kind == DECLINE:
            if self._received_proposal(peer, context) is None:
                return "a refusal only answers a proposal received from this peer"
            return None
        if kind == CONFIRM:
            sent = self._exchanged("out", peer, context, PROPOSE)
            accepted = self._exchanged("in", peer, context, ACCEPT)
            if sent is None or accepted is None:
                return "a confirmation needs the approved proposal and the peer's acceptance"
            slot = body.slot
            proposed = Propose.model_validate(sent["body"])
            if slot not in proposed.slots or self._moment(accepted["body"]["slot"]) != slot:
                return "a confirmation must be for the slot proposed and accepted"
            return None
        return f"{kind} cannot be sent"

    def _matches_proposal(self, params: dict[str, Any], peer: Peer, body: Propose) -> bool:
        start, end = self._moment(params.get("window_start")), self._moment(params.get("window_end"))
        return (
            self.find(str(params.get("peer", ""))) == peer
            and clean_title(str(params.get("title", ""))) == body.title
            and params.get("duration_minutes") == body.duration_minutes
            and start == body.window_start
            and end == body.window_end
            and start is not None
            and end is not None
            and all(
                start <= s and s + timedelta(minutes=body.duration_minutes) <= end
                for s in body.slots
            )
        )

    def _exchanged(
        self, direction: str, peer: Peer | None, context: str, kind: str
    ) -> dict[str, Any] | None:
        if peer is None:
            return None
        other = "recipient" if direction == "out" else "sender"
        for item in reversed(self.state.exchanged):
            envelope = item.envelope
            if (
                item.direction == direction
                and envelope[other] == peer.instance
                and envelope["context"] == context
                and envelope["type"] == kind
            ):
                return envelope
        return None

    def _received_proposal(self, peer: Peer | None, context: str) -> Propose | None:
        found = self._exchanged("in", peer, context, PROPOSE)
        return Propose.model_validate(found["body"]) if found else None

    async def deliver(
        self, peer: Peer | PeerCard, kind: str, context: str, body: dict[str, Any]
    ) -> Envelope:
        """Sign and post; journaled once the peer took it."""
        unsigned = Envelope(
            id=new_id(),
            sender=self.card.instance,
            recipient=peer.instance,
            type=kind,
            context=context,
            sent_at=self.service.clock(),
            body=body,
        )
        envelope = unsigned.model_copy(
            update={"signature": self.identity.sign(unsigned.signed_bytes())}
        )
        if not _safe_url(peer.url):
            raise WireError(f"refusing to reach {peer.url}: not HTTPS")
        response = await self._http.post(peer.url, json=to_a2a(envelope))
        try:
            answer: Any = response.json()
        except ValueError as exc:
            raise WireError(f"answered {response.status_code}, not JSON-RPC") from exc
        if response.status_code != 200 or not isinstance(answer, dict) or "result" not in answer:
            error: Any = cast(dict[str, Any], answer).get("error") if isinstance(answer, dict) else None
            detail = cast(dict[str, Any], error).get("message") if isinstance(error, dict) else None
            raise WireError(f"refused ({response.status_code}): {detail or 'no reason given'}")
        await self.service.ledger.record(SENT, {"envelope": envelope.model_dump(mode="json")})
        log.info("federation.sent", peer=peer.instance, type=kind, context=context)
        return envelope

    # -- pairing (admin: the guardian's own user) ---------------------------------------------

    async def _invite(self, request: Request) -> Response:
        if not self._admin(request):
            return refused("pairing is for the owner, from the guardian's account", 401)
        try:
            asked = InviteRequest.model_validate_json(await request.body())
        except ValidationError:
            return refused("malformed invitation request", 400)
        token = secrets.token_urlsafe(24)
        expires = self.service.clock() + timedelta(days=self.settings.invite_days)
        await self.service.ledger.record(
            INVITE_CREATED,
            {"token_hash": _hash(token), "label": asked.label, "expires_at": expires.isoformat()},
        )
        return JSONResponse(
            {"code": encode_invite(self.card, token), "expires_at": expires.isoformat()}
        )

    async def _accept(self, request: Request) -> Response:
        if not self._admin(request):
            return refused("pairing is for the owner, from the guardian's account", 401)
        try:
            asked = AcceptRequest.model_validate_json(await request.body())
            card, token = decode_invite(asked.code)
        except (ValidationError, WireError) as exc:
            return refused(f"invalid invitation: {exc}", 400)
        if card.instance == self.card.instance:
            return refused("that invitation is this Orion's own", 400)
        introduction = Introduce(token=token, card=self.card)
        try:
            await self.deliver(card, INTRODUCE, f"pair-{new_id()}", introduction.model_dump())
        except (httpx.HTTPError, WireError) as exc:
            return refused(f"the other Orion refused the introduction: {exc}", 502)
        await self._add_peer(card, asked.label)
        return JSONResponse({"instance": card.instance, "fingerprint": _fp(card)})

    async def _revoke(self, request: Request) -> Response:
        if not self._admin(request):
            return refused("only the owner revokes, from the guardian's account", 401)
        try:
            asked = RevokeRequest.model_validate_json(await request.body())
        except ValidationError:
            return refused("malformed revoke request", 400)
        peer = self.find(asked.peer)
        if peer is None:
            return refused(f"{asked.peer} is not a paired Orion", 404)
        await self.service.ledger.record(PEER_REVOKED, {"instance": peer.instance})
        return JSONResponse({"revoked": peer.instance})

    async def _add_peer(self, card: PeerCard, label: str) -> None:
        await self.service.ledger.record(
            PEER_ADDED,
            {
                "instance": card.instance,
                "label": label,
                "public_key": card.public_key,
                "url": card.url,
                "skills": [SCHEDULING],
            },
        )
        log.info("federation.paired", peer=card.instance, label=label)

    # -- public (other Orions) ----------------------------------------------------------------

    async def _card(self, _request: Request) -> Response:
        return JSONResponse(agent_card(self.card))

    async def _receive(self, request: Request) -> Response:
        raw = await request.body()
        if len(raw) > MAX_BODY:
            return JSONResponse(a2a_error(None, "message too large"), status_code=413)
        try:
            rpc_id, envelope = from_a2a(_json(raw))
        except WireError as exc:
            return JSONResponse(a2a_error(None, str(exc)), status_code=400)
        reason, trust = await self._inbound_policy(envelope)
        if reason is not None:
            await self.service.deny(f"federation {envelope.type} from {envelope.sender}", reason)
            return JSONResponse(a2a_error(rpc_id, reason, -32001), status_code=403)
        if envelope.id not in self.state.envelope_ids:
            await self.service.ledger.record(
                RECEIVED,
                {"envelope": envelope.model_dump(mode="json")},
                trust=trust,
                source=f"peer:{envelope.sender}",
            )
            log.info("federation.received", peer=envelope.sender, type=envelope.type)
        return JSONResponse(a2a_result(rpc_id, envelope.id))

    async def _inbound_policy(self, envelope: Envelope) -> tuple[str | None, Trust]:
        await self.service.ledger.catch_up()
        if envelope.recipient != self.card.instance:
            return "this message is for another Orion", Trust.UNKNOWN
        if abs(self.service.clock() - envelope.sent_at) > CLOCK_SKEW:
            return "the message is too old or from the future", Trust.UNKNOWN
        try:
            body = parse_body(envelope.type, envelope.body)
        except WireError as exc:
            return str(exc), Trust.UNKNOWN
        if isinstance(body, Introduce):
            return await self._introduction(envelope, body), Trust.TRUSTED_PEER
        peer = self.state.peer(envelope.sender)
        if peer is None:
            return "unknown Orion: pair through an invitation first", Trust.UNKNOWN
        if not verify(peer.public_key, envelope.signed_bytes(), envelope.signature):
            return "bad signature", Trust.UNKNOWN
        if SCHEDULING not in peer.skills:
            return "this relationship does not allow scheduling", Trust.UNKNOWN
        if envelope.id in self.state.envelope_ids:
            return None, Trust.TRUSTED_PEER  # a retry: acknowledged, not stored twice
        if not self._within_quota(peer.instance):
            return "too many messages from this Orion", Trust.UNKNOWN
        return None, Trust.TRUSTED_PEER

    async def _introduction(self, envelope: Envelope, body: Introduce) -> str | None:
        invite = self.state.invites.get(_hash(body.token))
        if invite is None or invite.used:
            return "unknown or already used invitation"
        if self.service.clock() >= invite.expires_at:
            return "expired invitation"
        if body.card.instance != envelope.sender:
            return "the card is not the sender's"
        if not verify(body.card.public_key, envelope.signed_bytes(), envelope.signature):
            return "bad signature"
        if not _safe_url(body.card.url):
            return "the Orion must be reachable over HTTPS"
        await self.service.ledger.record(INVITE_USED, {"token_hash": invite.token_hash})
        await self._add_peer(body.card, invite.label)
        return None

    def _within_quota(self, instance: str) -> bool:
        now = time.monotonic()
        recent = self._inbound[instance]
        while recent and now - recent[0] > 3600:
            recent.popleft()
        if len(recent) >= self.settings.max_inbound_per_hour:
            return False
        recent.append(now)
        return True


def _fp(card: PeerCard) -> str:
    return fingerprint(card.public_key)


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WireError("not JSON") from exc


def admin_token(directory: Path) -> str:
    """Written where only the guardian's user can read it; `orion federation` reads it."""
    path = directory / "admin.token"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    path.chmod(0o600)
    return token
