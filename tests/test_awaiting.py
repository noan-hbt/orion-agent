"""Waiting for an answer, and coming back to it when none comes (D109)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from orion.awaiting.module import (
    APPROVAL_ASKED,
    APPROVAL_DECIDED,
    CONTACT_WROTE,
    MAIL_CAME,
    MEETING_PROPOSED,
    PEER_WROTE,
    Awaiting,
    AwaitingSettings,
    AwaitingState,
    awaiting_tools,
)
from orion.core.events import NewEvent, Provenance, Trust
from orion.core.journal import SqliteJournal
from orion.core.projections import ProjectionRunner
from orion.tools.base import ToolContext, ToolResult

PARIS = ZoneInfo("Europe/Paris")
CTX = ToolContext("owner", "c", context_trust=Trust.OWNER)


def now() -> datetime:
    return datetime.now(PARIS)


def awaiting_for(journal: SqliteJournal, **settings: Any) -> Awaiting:
    return Awaiting(journal, AwaitingSettings(**settings), "Europe/Paris")


async def write(journal: SqliteJournal, kind: str, trust: Trust = Trust.SYSTEM, **p: Any) -> None:
    await journal.append(
        [NewEvent(kind=kind, payload=p, provenance=Provenance(source="t", actor="t"), trust=trust)]
    )


async def call(desk: Awaiting, action: str, **args: Any) -> ToolResult:
    tool = {t.name: t for t in awaiting_tools(desk)}[f"awaiting_{action}"]
    return await tool.handler(tool.parse(json.dumps(args)), CTX)


def local(moment: datetime) -> str:
    return f"{moment.astimezone(PARIS):%Y-%m-%d %H:%M}"


async def keys(awaiting: Awaiting, at: datetime) -> list[str]:
    return [a.key for a in await awaiting.alerts(at)]


async def test_a_question_to_a_contact_comes_back_unless_she_answers(
    journal: SqliteJournal,
) -> None:
    awaiting = awaiting_for(journal)
    soon = now() + timedelta(hours=3)
    made = await call(
        awaiting, "wait", who="contact", name="Angèle", about="si elle vient samedi", at=local(soon)
    )
    assert "J'attends" in made.content
    (wait,) = awaiting.state.pending()

    assert await keys(awaiting, now()) == []  # not yet
    (alert,) = await awaiting.alerts(soon + timedelta(minutes=5))
    assert alert.key == f"awaiting:{wait.id}" and "Angèle" in alert.notice
    assert "contact_message" in alert.notice and "SILENCE" in alert.notice
    assert await keys(awaiting, soon + timedelta(hours=40)) == []  # stale: dropped

    await write(journal, CONTACT_WROTE, Trust.THIRD_PARTY, contact="Paul")
    await awaiting.sync()
    assert [w.id for w in awaiting.state.pending()] == [wait.id]  # someone else wrote
    await write(journal, CONTACT_WROTE, Trust.THIRD_PARTY, contact="angèle")
    await awaiting.sync()
    assert awaiting.state.pending() == []
    assert await keys(awaiting, soon + timedelta(minutes=5)) == []


async def test_an_answer_from_him_another_orion_or_a_mail_closes_the_wait(
    journal: SqliteJournal,
) -> None:
    awaiting = awaiting_for(journal)
    later = local(now() + timedelta(hours=2))
    await write(journal, "federation.chat.sent", peer="orion-victor", label="Victor", text="?")
    await call(awaiting, "wait", who="owner", about="s'il prend le train de 8 h", at=later)
    await call(awaiting, "wait", who="orion", name="Victor", about="le ciné", at=later)
    await call(awaiting, "wait", who="mail", name="agence@immo.fr", about="la visite", at=later)
    assert len(awaiting.state.pending()) == 3
    listing = await call(awaiting, "list")
    assert "l'Orion de Victor" in listing.content and "agence@immo.fr" in listing.content

    await write(journal, PEER_WROTE, Trust.TRUSTED_PEER, envelope={"sender": "orion-victor"})
    await write(journal, MAIL_CAME, Trust.THIRD_PARTY, from_addr="Agence@immo.fr")
    await awaiting.sync()
    (left,) = awaiting.state.pending()  # his next message may be about something else
    assert left.who == "owner"

    rebuilt = AwaitingState()  # projections rebuild from the journal alone
    await ProjectionRunner(journal, [rebuilt]).catch_up()
    assert rebuilt.open == {left.id} and rebuilt.waits == awaiting.state.waits


async def test_nudges_follow_each_other_and_stop(journal: SqliteJournal) -> None:
    awaiting = awaiting_for(journal, max_rounds=2)
    later = local(now() + timedelta(hours=2))
    await call(awaiting, "wait", who="contact", name="Paul", about="le devis", at=later)
    (first,) = awaiting.state.pending()
    await call(
        awaiting, "wait", who="contact", name="Paul", about="le devis", at=later, follows=first.id
    )
    (second,) = awaiting.state.pending()
    assert second.round == 2  # the nudge took over
    await call(
        awaiting, "wait", who="contact", name="Paul", about="le devis", at=later, follows=second.id
    )
    (third,) = awaiting.state.pending()
    (alert,) = await awaiting.alerts(third.at)
    assert "dernière relance" in alert.notice and "follows" not in alert.notice

    refused = await call(
        awaiting, "wait", who="contact", name="Paul", about="le devis", at=later, follows=third.id
    )
    assert refused.is_error and "n'insiste plus" in refused.content
    past = await call(
        awaiting, "wait", who="owner", about="x" * 5, at=local(now() - timedelta(hours=1))
    )
    assert past.is_error
    nobody = await call(awaiting, "wait", who="contact", about="le devis", at=later)
    assert nobody.is_error

    assert (await call(awaiting, "cancel", awaiting=third.id)).content == "Attente retirée."
    assert awaiting.state.pending() == []


async def test_an_unanswered_approval_comes_back_once_and_an_answer_ends_it(
    journal: SqliteJournal,
) -> None:
    awaiting = awaiting_for(journal, approval_after_hours=3)
    await write(
        journal,
        APPROVAL_ASKED,
        tool="contact_message",
        arguments='{"contact":"Angèle"}',
        approval_id="a1",
    )
    await write(
        journal, APPROVAL_DECIDED, Trust.OWNER, approval_id="a1", approved=False, how="timeout"
    )
    await write(journal, "message.received", Trust.OWNER, text="au fait, il pleut")
    await awaiting.sync()
    (wait,) = awaiting.state.pending()  # a message about something else is not an answer
    assert wait.who == "owner" and wait.auto == "approval"
    assert await keys(awaiting, datetime.now(UTC) + timedelta(hours=1)) == []
    (alert,) = await awaiting.alerts(datetime.now(UTC) + timedelta(hours=3, minutes=1))
    assert "contact_message" in alert.notice and "s'il le veut toujours" in alert.notice

    # Asked again and answered: the old question is moot.
    await write(journal, APPROVAL_ASKED, tool="contact_message", arguments="{}", approval_id="a2")
    await write(journal, APPROVAL_DECIDED, Trust.OWNER, approval_id="a2", approved=True, how="word")
    await awaiting.sync()
    assert awaiting.state.pending() == []

    # An approval that could not be sent is not a question left unanswered.
    await write(journal, APPROVAL_ASKED, tool="mail_send", arguments="{}", approval_id="a3")
    await write(
        journal, APPROVAL_DECIDED, Trust.OWNER, approval_id="a3", approved=False, how="unsent"
    )
    await awaiting.sync()
    assert awaiting.state.pending() == []


async def test_a_meeting_another_orion_leaves_unanswered_comes_back(
    journal: SqliteJournal,
) -> None:
    awaiting = awaiting_for(journal, meeting_after_hours=24)
    await write(
        journal,
        MEETING_PROPOSED,
        meeting_id="m1",
        peer="orion-camille",
        label="Camille",
        title="Dîner",
        poll=True,
    )
    await awaiting.sync()
    (wait,) = awaiting.state.pending()
    assert await keys(awaiting, datetime.now(UTC) + timedelta(hours=2)) == []
    (alert,) = await awaiting.alerts(datetime.now(UTC) + timedelta(hours=25))
    assert "l'Orion de Camille" in alert.notice and "Dîner" in alert.notice
    assert "peer_message" in alert.notice
    await write(journal, PEER_WROTE, Trust.TRUSTED_PEER, envelope={"sender": "orion-camille"})
    await awaiting.sync()
    assert wait.id not in awaiting.state.open
