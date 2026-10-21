"""What two Orions say to each other, and how it travels.

Every message is an `Envelope` signed by its sender's instance key. It travels as an A2A
JSON-RPC `message/send` whose single data part carries the envelope, so an A2A-speaking peer
sees an ordinary agent message. The wire layer is kept here, apart from the logic, to follow
the A2A specification as it settles.

Bodies are typed per message: free text is short and bounded (a meeting title), everything
else is structure. Another Orion is never trusted to give instructions (D8): what it sends is
data that code checks.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from orion.federation.crypto import b64, canonical, fingerprint, unb64

INTRODUCE = "orion.introduce"
PROPOSE = "meeting.propose"
ACCEPT = "meeting.accept"
DECLINE = "meeting.decline"
CONFIRM = "meeting.confirm"
POLL = "meeting.poll"
AVAILABLE = "meeting.available"
CANCEL = "meeting.cancel"
MAX_SLOTS = 8
MAX_GROUP = 5
"""Other Orions invited to the same meeting, besides the one a message goes to."""
SCHEDULING = "scheduling"
INVITE_PREFIX = "orion-invite:"


class WireError(ValueError):
    """A message that is not well-formed: refused before anything reads it."""


def _clean(text: str, limit: int) -> str:
    return " ".join("".join(c for c in text if c.isprintable()).split())[:limit]


def clean_title(text: str) -> str:
    """A meeting title as it travels: printable, one line, 80 characters at most."""
    return _clean(text, 80) or "rendez-vous"


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PeerCard(_Body):
    instance: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$", max_length=64)
    owner_name: str = Field(max_length=60)
    public_key: str = Field(max_length=64)
    url: str = Field(max_length=300, description="The A2A endpoint")

    @field_validator("owner_name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _clean(value, 60)


class Introduce(_Body):
    token: str = Field(max_length=100)
    card: PeerCard


class Propose(_Body):
    title: str = Field(min_length=1, max_length=80)
    duration_minutes: int = Field(ge=15, le=480)
    window_start: datetime
    window_end: datetime
    slots: tuple[datetime, ...] = Field(min_length=1, max_length=MAX_SLOTS)
    group: tuple[str, ...] = Field(default=(), max_length=MAX_GROUP)
    """A meeting of several: the names of the others invited, as the sender knows them. Shown
    to the owner asked, nothing more."""

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        return clean_title(value)

    @field_validator("group")
    @classmethod
    def _group(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_clean(name, 60) or "?" for name in value)


class Accept(_Body):
    slot: datetime


class Confirm(_Body):
    slot: datetime


class Available(_Body):
    """The answer to a poll: which of the starts offered are free. Code answers it, the owner
    is asked once the group has a common slot (D99)."""

    slots: tuple[datetime, ...] = Field(max_length=MAX_SLOTS)


class Cancel(_Body):
    """From the one who proposed: the group meeting will not happen, forget the slot."""

    reason: Literal["group_failed"]


class Decline(_Body):
    reason: Literal["no_slot", "declined", "unreachable"]
    """`unreachable`: nobody could be asked on that side; nothing was refused (D98)."""


BODIES: dict[str, type[_Body]] = {
    INTRODUCE: Introduce,
    PROPOSE: Propose,
    ACCEPT: Accept,
    DECLINE: Decline,
    CONFIRM: Confirm,
    POLL: Propose,
    AVAILABLE: Available,
    CANCEL: Cancel,
}


def parse_body(kind: str, body: dict[str, Any]) -> _Body:
    model = BODIES.get(kind)
    if model is None:
        raise WireError(f"unknown message type {kind!r}")
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        raise WireError(f"malformed {kind}: {exc.error_count()} errors") from exc


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=64)
    sender: str = Field(max_length=64)
    recipient: str = Field(max_length=64)
    type: str = Field(max_length=40)
    context: str = Field(max_length=64, description="The negotiation this message belongs to")
    sent_at: datetime
    body: dict[str, Any]
    signature: str = ""

    def signed_bytes(self) -> bytes:
        return canonical(self.model_dump(mode="json", exclude={"signature"}))


def to_a2a(envelope: Envelope) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": envelope.id,
        "method": "message/send",
        "params": {
            "message": {
                "kind": "message",
                "role": "agent",
                "messageId": envelope.id,
                "contextId": envelope.context,
                "parts": [{"kind": "data", "data": {"orion": envelope.model_dump(mode="json")}}],
            }
        },
    }


def from_a2a(payload: Any) -> tuple[Any, Envelope]:
    """(JSON-RPC id, envelope) from a `message/send` request."""
    if not isinstance(payload, dict):
        raise WireError("not a JSON-RPC request")
    request = cast(dict[str, Any], payload)
    rpc_id = request.get("id")
    if request.get("jsonrpc") != "2.0" or request.get("method") != "message/send":
        raise WireError("only JSON-RPC message/send is understood")
    try:
        part = request["params"]["message"]["parts"][0]
        envelope = Envelope.model_validate(part["data"]["orion"])
    except (KeyError, IndexError, TypeError, ValidationError) as exc:
        raise WireError("no Orion envelope in the message") from exc
    return rpc_id, envelope


def a2a_result(rpc_id: Any, envelope_id: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "result": {
            "kind": "message",
            "role": "agent",
            "messageId": f"ack-{envelope_id}",
            "parts": [{"kind": "data", "data": {"received": envelope_id}}],
        },
    }


def a2a_error(rpc_id: Any, message: str, code: int = -32600) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def agent_card(card: PeerCard) -> dict[str, Any]:
    """An A2A agent card, plus Orion's own block (instance and key)."""
    return {
        "name": f"Orion de {card.owner_name}" if card.owner_name else "Orion",
        "description": "Agent personnel. Ne parle qu'aux Orion présentés par leur propriétaire.",
        "url": card.url,
        "version": "0.1.0",
        "protocolVersion": "0.3.0",
        "preferredTransport": "JSONRPC",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            {
                "id": SCHEDULING,
                "name": "Rendez-vous",
                "description": "Trouver un créneau commun et le confirmer, avec l'accord des "
                "deux propriétaires.",
                "tags": ["calendar", "scheduling"],
            }
        ],
        "orion": {**card.model_dump(), "fingerprint": fingerprint(card.public_key)},
    }


def encode_invite(card: PeerCard, token: str) -> str:
    return INVITE_PREFIX + b64(canonical({"card": card.model_dump(), "token": token}))


def decode_invite(code: str) -> tuple[PeerCard, str]:
    if not code.startswith(INVITE_PREFIX):
        raise WireError("not an Orion invitation")
    try:
        data = json.loads(unb64(code.removeprefix(INVITE_PREFIX).strip()))
        return PeerCard.model_validate(data["card"]), str(data["token"])
    except (ValueError, KeyError, TypeError) as exc:
        raise WireError("damaged invitation") from exc
