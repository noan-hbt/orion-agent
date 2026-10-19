"""Memory event kinds and the shapes of their payloads.

Memory is never written directly: every change is an event in the journal, and the memory
store is a projection of those events. That keeps memory rebuildable, auditable, and lets
"forget this source" be proven by replaying the journal without it.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

FACT_ASSERTED = "memory.fact.asserted"
FACT_INVALIDATED = "memory.fact.invalidated"
COMMITMENT_CREATED = "commitment.created"
COMMITMENT_CLOSED = "commitment.closed"

MEMORY_KINDS = frozenset({FACT_ASSERTED, FACT_INVALIDATED, COMMITMENT_CREATED, COMMITMENT_CLOSED})


class Category(StrEnum):
    PROFILE = "profile"  # who the owner is: identity, situation, health, finances…
    PREFERENCE = "preference"  # how they like things done
    PERSON = "person"  # facts about someone in their life
    PROJECT = "project"  # ongoing work, goals, plans
    OTHER = "other"


class FactAsserted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_id: str
    statement: str = Field(min_length=1, max_length=1000)
    about: list[str] = Field(default_factory=list[str], description="Names of people/things")
    category: Category = Category.OTHER
    valid_from: str | None = Field(default=None, description="ISO date the fact became true")
    supersedes: list[str] = Field(
        default_factory=list[str], description="Fact ids this one replaces (they get invalidated)"
    )


class FactInvalidated(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_id: str
    reason: str
    valid_to: str | None = Field(default=None, description="ISO date the fact stopped being true")


class CommitmentStatus(StrEnum):
    OPEN = "open"
    DONE = "done"
    CANCELLED = "cancelled"


class CommitmentCreated(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commitment_id: str
    debtor: str = Field(description="Who owes it: 'owner', 'orion', or a person's name")
    creditor: str = Field(description="Who it is owed to: 'owner', 'orion', or a person's name")
    what: str = Field(min_length=1, max_length=500)
    due: str | None = Field(default=None, description="ISO date or datetime, if any")


class CommitmentClosed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commitment_id: str
    status: CommitmentStatus
    evidence: str | None = None
