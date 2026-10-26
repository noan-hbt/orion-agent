"""Memory event kinds and the shapes of their payloads.

Memory is never written directly: every change is an event in the journal, and the memory
store is a projection of those events. That keeps memory rebuildable, auditable, and lets
"forget this source" be proven by replaying the journal without it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FACT_ASSERTED = "memory.fact.asserted"
FACT_INVALIDATED = "memory.fact.invalidated"
COMMITMENT_CREATED = "commitment.created"
COMMITMENT_CLOSED = "commitment.closed"
RULE_LEARNED = "playbook.rule.learned"
RULE_RETIRED = "playbook.rule.retired"
SETTING_CHANGED = "settings.changed"
PERSON_ALIASED = "person.aliased"

MEMORY_KINDS = frozenset(
    {
        FACT_ASSERTED,
        FACT_INVALIDATED,
        COMMITMENT_CREATED,
        COMMITMENT_CLOSED,
        RULE_LEARNED,
        RULE_RETIRED,
        SETTING_CHANGED,
        PERSON_ALIASED,
    }
)


class Category(StrEnum):
    PROFILE = "profile"  # who the owner is: identity, situation, health, finances…
    PREFERENCE = "preference"  # how they like things done
    PERSON = "person"  # facts about someone in their life
    PROJECT = "project"  # ongoing work, goals, plans
    OTHER = "other"


MAX_STATEMENT = 1000
"""Characters a fact's statement may hold."""


class FactAsserted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_id: str
    statement: str = Field(min_length=1, max_length=MAX_STATEMENT)
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


Closing = Literal[CommitmentStatus.DONE, CommitmentStatus.CANCELLED]
"""How a commitment closes: never "open" again."""


class CommitmentClosed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commitment_id: str
    status: CommitmentStatus
    evidence: str | None = None


class RuleScope(StrEnum):
    STYLE = "style"  # how Orion talks to the owner: length, tone, humour, format
    INITIATIVE = "initiative"  # when and about what Orion speaks first
    WORK = "work"  # how the owner likes things done: tools, habits, defaults
    OTHER = "other"


class RuleBasis(StrEnum):
    EXPLICIT = "explicit"  # the owner said so
    INFERRED = "inferred"  # Orion noticed it from several signals


class RuleLearned(BaseModel):
    """One line of the owner's playbook: how to work with this person."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    rule: str = Field(min_length=3, max_length=300)
    scope: RuleScope = RuleScope.OTHER
    basis: RuleBasis = RuleBasis.EXPLICIT
    replaces: list[str] = Field(default_factory=list[str], description="Rule ids this one retires")


class RuleRetired(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    reason: str


class SettingKey(StrEnum):
    DIGESTS = "initiative.digests"  # list of "HH:MM"
    QUIET_START = "initiative.quiet_start"  # "HH:MM"
    QUIET_END = "initiative.quiet_end"  # "HH:MM"
    DAILY_BUDGET = "initiative.daily_budget"  # int


class SettingChanged(BaseModel):
    """A behaviour setting changed by the owner or learned by Orion; overrides the config file."""

    model_config = ConfigDict(extra="forbid")

    change_id: str
    key: SettingKey
    value: str | int | list[str]
    reason: str = Field(max_length=300)


class PersonAliased(BaseModel):
    """Two names for the same person ("Paul" and "Paul Martin"): facts about either are about
    `canonical`."""

    model_config = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1, max_length=120)
    canonical: str = Field(min_length=1, max_length=120)
    reason: str = Field(max_length=300)
