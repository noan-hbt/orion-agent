"""Playbook tools: how the owner tells Orion to work differently, and how Orion records it.

A rule is a line of the owner's playbook ("Réponses de deux phrases max le matin"). A setting is
a number Orion's code obeys (digest times, quiet hours, attention budget). Both are journaled,
shown to the model in its memory, and can be retired or changed later.
"""

from __future__ import annotations

import re
from datetime import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from orion.memory.model import RuleBasis, RuleScope, SettingKey
from orion.memory.render import format_rule, format_setting
from orion.memory.service import MemoryService
from orion.memory.tools import origin_of
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

_HHMM = re.compile(r"^\s*(\d{1,2})\s*(?:[:hH]\s*(\d{2})?)?\s*$")


def parse_hhmm(value: str) -> time:
    """'07:45', '7:45', '7h45', '19h' → a time of day."""
    match = _HHMM.match(value)
    if match is None:
        raise ValueError(f"expected HH:MM, got {value!r}")
    return time(int(match[1]), int(match[2] or 0))


class AdaptInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule: str = Field(
        min_length=3,
        max_length=300,
        description="How to work with the owner from now on, as a short self-contained rule "
        "in their language, e.g. 'Pas d'emoji.' or 'Le matin, réponses de deux phrases max.'",
    )
    scope: RuleScope = RuleScope.STYLE
    replaces: list[str] = Field(
        default_factory=list[str], description="Ids of rules this one replaces"
    )


class UnlearnInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    reason: str


class InitiativeSettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(max_length=300, description="Why, in a few words (shown to the owner)")
    digests: list[str] | None = Field(
        default=None,
        max_length=6,
        description="Times of the daily digests, 'HH:MM'. An empty list stops digests.",
    )
    quiet_start: str | None = Field(default=None, description="'HH:MM'")
    quiet_end: str | None = Field(default=None, description="'HH:MM'")
    daily_budget: int | None = Field(
        default=None, ge=0, le=30, description="Max unrequested messages per day"
    )

    @field_validator("digests")
    @classmethod
    def _digests(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return sorted({f"{parse_hhmm(v):%H:%M}" for v in value})

    @field_validator("quiet_start", "quiet_end")
    @classmethod
    def _hhmm(cls, value: str | None) -> str | None:
        return None if value is None else f"{parse_hhmm(value):%H:%M}"


class PlaybookInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def adaptation_tools(memory: MemoryService) -> list[Tool]:
    async def adapt(args: Any, ctx: ToolContext) -> ToolResult:
        for rule_id in args.replaces:
            existing = await memory.store.rule(rule_id)
            if existing is None or not existing.active:
                return ToolResult(f"No active rule {rule_id}", is_error=True)
        rule_id = await memory.learn_rule(
            args.rule,
            origin_of(ctx),
            scope=args.scope,
            basis=RuleBasis.EXPLICIT,
            replaces=args.replaces,
        )
        return ToolResult(f"Règle ajoutée ({rule_id}).", data={"rule_id": rule_id})

    async def unlearn(args: Any, ctx: ToolContext) -> ToolResult:
        existing = await memory.store.rule(args.rule_id)
        if existing is None or not existing.active:
            return ToolResult(f"No active rule {args.rule_id}", is_error=True)
        await memory.retire_rule(args.rule_id, args.reason, origin_of(ctx))
        return ToolResult(f"Règle {args.rule_id} retirée.")

    async def initiative_settings(args: Any, ctx: ToolContext) -> ToolResult:
        changes: dict[SettingKey, str | int | list[str]] = {}
        if args.digests is not None:
            changes[SettingKey.DIGESTS] = args.digests
        if args.quiet_start is not None:
            changes[SettingKey.QUIET_START] = args.quiet_start
        if args.quiet_end is not None:
            changes[SettingKey.QUIET_END] = args.quiet_end
        if args.daily_budget is not None:
            changes[SettingKey.DAILY_BUDGET] = args.daily_budget
        if not changes:
            return ToolResult("Nothing to change: pass at least one setting.", is_error=True)
        ids = [
            await memory.change_setting(key, value, args.reason, origin_of(ctx))
            for key, value in changes.items()
        ]
        current = await memory.store.settings()
        return ToolResult(
            "Réglages changés :\n" + "\n".join(format_setting(current[k]) for k in changes),
            data={"change_ids": ids},
        )

    async def playbook(args: Any, ctx: ToolContext) -> ToolResult:
        rules = await memory.store.rules()
        settings = (await memory.store.settings()).values()
        lines = [*(format_rule(r) for r in rules), *(format_setting(c) for c in settings)]
        return ToolResult("\n".join(lines) if lines else "Aucune règle ni réglage appris.")

    write: dict[str, Any] = {
        "effect": Effect.INTERNAL,
        "reversibility": Reversibility.REVERSIBLE,
        "accepts_untrusted_context": False,
    }
    return [
        Tool(
            name="adapt",
            description="Record how the owner wants you to work with them, as soon as they say "
            "it or react to your manner ('trop long', 'arrête les blagues', 'toujours en "
            "euros'). The rule applies from now on, everywhere.",
            input_model=AdaptInput,
            handler=adapt,
            **write,
        ),
        Tool(
            name="unlearn",
            description="Retire a playbook rule that no longer holds or that the owner rejects.",
            input_model=UnlearnInput,
            handler=unlearn,
            **write,
        ),
        Tool(
            name="initiative_settings",
            description="Change when you speak first: digest times, quiet hours, or the daily "
            "number of unrequested messages. Use it when the owner asks for more, less, or "
            "different timing.",
            input_model=InitiativeSettingsInput,
            handler=initiative_settings,
            **write,
        ),
        Tool(
            name="playbook",
            description="Show the rules and settings you have learned about working with the "
            "owner.",
            input_model=PlaybookInput,
            handler=playbook,
            effect=Effect.NONE,
            reversibility=Reversibility.REVERSIBLE,
            concurrency_safe=True,
            accepts_untrusted_context=True,
        ),
    ]
