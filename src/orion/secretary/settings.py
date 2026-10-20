"""Who may write to Orion besides the owner, and what Orion may tell them.

Read by the core (the secretary module) and by the guardian process, which only lets Orion
write to these people: the list lives in the owner's configuration, never in what the core
can change.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from orion.modules.base import ModuleSettings

REPLY_WINDOW_HOURS = 12
"""Orion answers a contact only this long after their last message: it never writes first
on its own."""


class Contact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60, description="How the owner calls them")
    number: str = Field(description="Their phone number, E.164 (e.g. +33612345678)")
    about: str = Field(
        default="",
        max_length=500,
        description="Who they are and how to treat them, in the owner's words",
    )
    share: list[Literal["availability"]] = Field(
        default_factory=list[Literal["availability"]],
        description="What Orion may tell them: 'availability' = when the owner is busy or free "
        "(never what they are doing)",
    )


class SecretarySettings(ModuleSettings):
    enabled: bool = False
    contacts: list[Contact] = Field(default_factory=list[Contact])
    max_replies_per_day: int = Field(default=20, ge=1, le=200, description="Per contact")
    model: str = Field(default="", description="Who answers them; empty: the everyday model")
