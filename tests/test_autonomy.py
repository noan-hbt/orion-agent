"""Autonomy is earned action by action, granted by the owner, and taken back with a word."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from orion.calendar.service import CalendarAccount
from orion.calendar.write import NewCalendarEvent
from orion.core.events import NewEvent, Provenance, Trust
from orion.core.journal import SqliteJournal
from orion.guardian.autonomy import GRANTED, Autonomy, autonomy_tools
from orion.guardian.client import GuardianUnavailableError
from orion.guardian.policy import ActionProposal, Approval, Guardian
from orion.tools.base import Effect, Tool, ToolContext, ToolResult
from tests.fake_guardian import running_guardian
from tests.fake_inkbox import CONVERSATION
from tests.test_writes import EVENT, writable_config

PARIS = ZoneInfo("Europe/Paris")
NOON = datetime(2026, 11, 2, 12, 0, tzinfo=PARIS)


class Owner:
    def __init__(self, says: bool = True) -> None:
        self.says = says
        self.asked: list[ActionProposal] = []

    async def approve(self, proposal: ActionProposal) -> Approval:
        self.asked.append(proposal)
        return Approval(self.says, "g-1" if self.says else None)


async def approvals(journal: SqliteJournal, tool: str, answers: list[bool]) -> None:
    events: list[NewEvent] = []
    for n, answer in enumerate(answers):
        events += [
            NewEvent(
                kind="approval.requested",
                payload={"approval_id": f"a{tool}{n}", "tool": tool, "arguments": {}},
                provenance=Provenance(source="imessage", actor="orion:core"),
                trust=Trust.SYSTEM,
            ),
            NewEvent(
                kind="approval.decided",
                payload={"approval_id": f"a{tool}{n}", "approved": answer, "how": "reaction"},
                provenance=Provenance(source="imessage", actor="owner:main"),
                trust=Trust.OWNER,
            ),
        ]
    await journal.append(events)


async def handler(_args: Any, _ctx: ToolContext) -> ToolResult:
    return ToolResult("ok")


def tool(name: str) -> Tool:
    from orion.calendar.tools import CalendarAddInput

    return Tool(
        name, "d", CalendarAddInput, handler, Effect.EXTERNAL, accepts_untrusted_context=True
    )


def never_quiet(_moment: time) -> bool:
    return False


async def test_it_is_offered_after_a_run_of_yeses_once_a_month(journal: SqliteJournal) -> None:
    now = [NOON]
    autonomy = Autonomy(journal, ["calendar_add"], offer_after=3, clock=lambda: now[0])
    await autonomy.start()
    [grant_tool, *_] = autonomy_tools(autonomy)
    await approvals(journal, "calendar_add", [True, True, False, True, True])
    await approvals(journal, "call_someone", [True] * 9)  # never autonomable
    assert await autonomy.candidates() == []  # a no broke the run
    await approvals(journal, "calendar_add", [True])
    assert await autonomy.candidates() == [("calendar_add", 3)]

    owner = Owner(says=False)
    assert await autonomy.ritual(owner, grant_tool, never_quiet) is None
    [asked] = owner.asked
    assert asked.arguments.model_dump() == {
        "action": "calendar_add",
        "approved_times": 3,
        "recipients": [],
    }
    now[0] += timedelta(days=3)
    assert await autonomy.ritual(owner, grant_tool, never_quiet) is None
    assert len(owner.asked) == 1  # a ritual, not a nag
    now[0] += timedelta(days=30)
    owner.says = True
    assert await autonomy.ritual(owner, grant_tool, never_quiet) == "calendar_add"
    assert await autonomy.allows("calendar_add")


async def test_granted_actions_run_alone_only_in_a_clean_conversation(
    journal: SqliteJournal,
) -> None:
    autonomy = Autonomy(journal, ["calendar_add"])
    await autonomy.start()
    await autonomy.grant("calendar_add", "g", None)
    owner = Owner()
    guardian = Guardian(owner, autonomy.allows)
    add = tool("calendar_add")
    args = add.parse('{"title": "x", "start": "2026-11-03T10:00", "end": "2026-11-03T11:00"}')
    decision = await guardian.decide(ActionProposal(add, args, Trust.OWNER, "owner"))
    assert decision.allowed and owner.asked == []
    await guardian.decide(ActionProposal(add, args, Trust.THIRD_PARTY, "owner"))
    assert len(owner.asked) == 1  # outside content in the conversation: asked again
    other = tool("call_someone")
    await guardian.decide(ActionProposal(other, args, Trust.OWNER, "owner"))
    assert len(owner.asked) == 2
    with pytest.raises(ValueError, match="never be done without asking"):
        await autonomy.grant("call_someone", "g", None)

    [_, _, revoke] = autonomy_tools(autonomy)
    await revoke.handler(revoke.parse('{"action": "calendar_add"}'), ToolContext("o", "c"))
    await guardian.decide(ActionProposal(add, args, Trust.OWNER, "owner"))
    assert len(owner.asked) == 3
    assert [e.kind for e in await journal.read()].count(GRANTED) == 1


async def test_the_guardian_records_the_grant_and_enforces_it(tmp_path: Path) -> None:
    written: list[NewCalendarEvent] = []

    async def write(_account: CalendarAccount, event: NewCalendarEvent) -> str:
        written.append(event)
        return "uid"

    config = writable_config(tmp_path)
    config.guardian.max_autonomous_per_day = 2
    async with running_guardian(config, write_calendar=write) as g:
        g.fake.inbound("salut")
        with pytest.raises(GuardianUnavailableError, match="needs the owner's approval"):
            await g.client.calendar_add("perso", EVENT, None)
        with pytest.raises(GuardianUnavailableError, match="never be done without asking"):
            await g.client.post("/v1/autonomy/grant", {"action": "call_someone"})
        with pytest.raises(GuardianUnavailableError, match="needs the owner's approval"):
            await g.client.post("/v1/autonomy/grant", {"action": "calendar_add"})
        grant, message_id = await g.client.request_approval(
            "autonomy_grant", {"action": "calendar_add", "approved_times": 5}, "", CONVERSATION
        )
        assert "oui 5 fois de suite pour ajouter des événements" in g.fake.texts()[-1]
        g.fake.react(message_id, "like")
        await g.client.post(
            "/v1/autonomy/grant", {"action": "calendar_add"}, headers={"X-Orion-Grant": grant}
        )
        await g.client.calendar_add("perso", EVENT, None)
        await g.client.calendar_add("perso", EVENT, None)
        with pytest.raises(GuardianUnavailableError):  # today's allowance is spent
            await g.client.calendar_add("perso", EVENT, None)
        assert len(written) == 2
        await g.client.post("/v1/autonomy/revoke", {"action": "calendar_add"})
        assert (await g.client.get("/v1/autonomy"))["autonomous"] == []
