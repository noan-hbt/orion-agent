"""Who may write to Orion besides the owner, and what Orion may tell them.

Read by the core (the secretary module) and by the guardian process, which only lets Orion
write to these people: the list is the owner's. Orion changes it by message only with his yes
to the exact change, which the guardian checks itself when Orion runs split (D92).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from orion.modules.base import ModuleSettings

REPLY_WINDOW_HOURS = 12
"""Orion answers a contact only this long after their last message: it never writes first
on its own."""


class Contact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60, description="How the owner calls them")
    number: str = Field(
        default="", description="Their phone number, E.164 (e.g. +33612345678), for iMessage"
    )
    telegram_id: int = Field(
        default=0,
        description="Their Telegram user id, for Telegram: Orion tells the owner when someone "
        "unknown writes to the bot, with their id",
    )
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

    @model_validator(mode="after")
    def _reachable(self) -> Contact:
        if not self.number and not self.telegram_id:
            raise ValueError(f"{self.name}: give a `number` (iMessage) or a `telegram_id`")
        return self

    def addresses(self) -> list[str]:
        """How the secretary and the guardian key this person: their number for iMessage,
        `telegram:<id>` for Telegram."""
        from orion.channels.imessage import normalize_number

        found = [normalize_number(self.number)] if self.number else []
        return [*found, f"telegram:{self.telegram_id}"] if self.telegram_id else found


class SecretarySettings(ModuleSettings):
    enabled: bool = False
    contacts: list[Contact] = Field(default_factory=list[Contact])
    max_replies_per_day: int = Field(default=20, ge=1, le=200, description="Per contact")
    model: str | None = Field(default=None, description="Who answers them; default: agent.model")
