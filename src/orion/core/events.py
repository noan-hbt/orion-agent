"""Event model shared by the whole system.

Every fact Orion holds is either an event in the journal or a projection derived from events.
Each event carries its provenance and a trust level; anything derived from several events is
never more trusted than its least trusted input.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import IntEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orion.core.ids import new_id


class Trust(IntEnum):
    """How much Orion may rely on a piece of content. Higher is more trusted."""

    UNKNOWN = 0
    WEB = 10
    THIRD_PARTY = 20
    TRUSTED_PEER = 30
    OWNER_INSTANCE = 40
    SYSTEM = 50
    OWNER = 60


def derive_trust(inputs: Iterable[Trust]) -> Trust:
    """Trust of derived content: the minimum of its inputs. No inputs means UNKNOWN."""
    return min(inputs, default=Trust.UNKNOWN)


class Provenance(BaseModel):
    """Where a piece of content came from and what it was derived from."""

    model_config = ConfigDict(frozen=True)

    source: str = Field(
        description="Origin, as 'kind:id', e.g. 'imessage:msg_123' or 'web:https://…'"
    )
    actor: str = Field(
        description="Who produced it, as 'kind:id', e.g. 'owner:main' or 'orion:core'"
    )
    derived_from: tuple[str, ...] = Field(
        default=(), description="Ids of the events it derives from"
    )


class NewEvent(BaseModel):
    """An event before it is appended. The journal assigns sequence and timestamp."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=new_id)
    kind: str = Field(min_length=1, description="Dotted event type, e.g. 'message.received'")
    payload: dict[str, Any] = Field(default_factory=dict)
    provenance: Provenance
    trust: Trust
    causation_id: str | None = Field(
        default=None, description="Event that directly caused this one"
    )
    correlation_id: str | None = Field(default=None, description="Shared by all events of one flow")


class Event(NewEvent):
    """An event as stored in the journal."""

    seq: int = Field(description="Journal position, strictly increasing, gap-free per journal")
    hlc: str = Field(description="Encoded hybrid logical clock timestamp")
    redacted: bool = False
