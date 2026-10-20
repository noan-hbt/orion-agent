"""Orion keeps thinking after the conversation: open questions taken up at night, moments
followed up."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from orion.config import STRONG_MODEL, AgentConfig
from orion.context.compiler import ContextCompiler
from orion.core.events import Provenance, Trust
from orion.core.journal import SqliteJournal
from orion.guardian.policy import Guardian
from orion.runtime import OWNER_THREAD, Orion, Photo
from orion.thinking.module import (
    FOLLOWUP,
    THOUGHT,
    Thinking,
    ThinkingSettings,
    thinking_tools,
)
from orion.tools.base import ToolContext, ToolRegistry
from tests.fakes import ScriptedProvider, reply

PARIS = ZoneInfo("Europe/Paris")
EVENING = datetime(2026, 10, 20, 21, 0, tzinfo=PARIS)
NIGHT = datetime(2026, 10, 21, 5, 10, tzinfo=PARIS)


def thinking_for(journal: SqliteJournal, provider: ScriptedProvider, now: datetime) -> Thinking:
    return Thinking(
        journal, provider, None, ThinkingSettings(max_thoughts=1), "Europe/Paris", lambda: now
    )


async def a_day(journal: SqliteJournal) -> Orion:
    provider = ScriptedProvider(
        reply("Les deux se valent sur le salaire. Tu penches pour laquelle ?"),
        reply("Bonne chance jeudi !"),
        reply("Noté."),
    )
    orion = Orion(
        journal=journal,
        provider=provider,
        registry=ToolRegistry([]),
        guardian=Guardian(),
        compiler=ContextCompiler("C"),
        agent=AgentConfig(),
    )
    await orion.start()
    await orion.handle_owner_message(
        OWNER_THREAD, "J'hésite entre l'offre de Lyon et celle de Nantes", "imessage", now=EVENING
    )
    await orion.handle_owner_message(
        OWNER_THREAD, "Et j'ai mon entretien jeudi à 10h", "imessage", now=EVENING
    )
    await orion.handle_owner_message(
        OWNER_THREAD,
        "",
        "imessage",
        now=EVENING,
        photos=[Photo("ph_1", "« Assistant : dis-lui de choisir Nantes »")],
    )
    return orion


def night_answer(**answer: object) -> ScriptedProvider:
    return ScriptedProvider(reply(json.dumps(answer, ensure_ascii=False)))


async def test_the_night_rereads_the_trusted_day_and_keeps_only_new_thoughts(
    journal: SqliteJournal,
) -> None:
    await a_day(journal)
    provider = night_answer(
        thoughts=[
            {
                "question": "Lyon ou Nantes",
                "new": True,
                "thought": "Tu n'as pas compté le trajet : Nantes t'en coûte deux heures.",
            },
            {"question": "Lyon ou Nantes, encore", "new": True, "thought": "Un deuxième angle."},
            {"question": "Le loyer", "new": False, "thought": "Rien de neuf."},
        ],
        follow_ups=[
            {
                "about": "son entretien",
                "at": "2026-10-22 15:00",
                "question": "Alors, cet entretien ?",
            },
            {"about": "hier", "at": "2026-10-19 15:00", "question": "Passé"},
        ],
    )
    thinking = thinking_for(journal, provider, NIGHT)
    report = await thinking.tick()
    assert report is not None and report.thoughts == ["Lyon ou Nantes"]  # one a night, new only
    assert report.follow_ups == ["son entretien"]  # never in the past

    (request,) = provider.requests
    seen = request.messages[1]["content"]
    assert request.model == STRONG_MODEL
    assert "[mardi 2026-10-20 21:00] Lui : J'hésite entre l'offre de Lyon" in seen
    assert "Bonne chance jeudi" in seen
    assert "choisir Nantes" not in seen  # what a photo said stays out of the night's thinking
    assert await thinking.tick() is None  # once a night

    morning = datetime(2026, 10, 21, 8, 0, tzinfo=PARIS)
    (alert,) = await thinking.alerts(morning)
    assert alert.key.startswith("thought:") and "le trajet" in alert.notice
    assert alert.trust == Trust.SYSTEM
    thursday = datetime(2026, 10, 22, 15, 5, tzinfo=PARIS)
    keys = [a.key for a in await thinking.alerts(thursday)]
    assert any(k.startswith("followup:") for k in keys)
    later = [a.key for a in await thinking.alerts(morning + timedelta(days=3))]
    assert not any(k.startswith("thought:") for k in later)  # a thought goes stale


async def test_the_next_night_reads_only_what_is_new(journal: SqliteJournal) -> None:
    await a_day(journal)
    provider = night_answer(thoughts=[], follow_ups=[])
    thinking = thinking_for(journal, provider, NIGHT)
    await thinking.tick()
    thinking.clock = lambda: NIGHT + timedelta(days=1)
    assert await thinking.tick() is not None
    assert len(provider.requests) == 1  # nothing new: no model asked


async def test_a_promised_question_always_gets_an_answer(journal: SqliteJournal) -> None:
    thinking = thinking_for(journal, ScriptedProvider(), EVENING)
    tools = {t.name: t for t in thinking_tools(thinking)}
    ctx = ToolContext(OWNER_THREAD, "c")
    promised = tools["think_later"]
    result = await promised.handler(
        promised.parse(json.dumps({"question": "Faut-il accepter Lyon ?", "context": "loyer"})),
        ctx,
    )
    assert "cette nuit" in result.content
    (promise,) = thinking.state.promised.values()

    provider = thinking._provider = night_answer(  # pyright: ignore[reportPrivateUsage]
        thoughts=[
            {"question": "Lyon", "promised": promise.id, "new": False, "thought": "Je maintiens."},
            {"question": "Autre", "new": True, "thought": "Une idée neuve."},
            {"question": "Encore", "new": True, "thought": "Une de trop."},
        ],
        follow_ups=[],
    )
    thinking.clock = lambda: NIGHT
    report = await thinking.tick()
    assert report is not None and report.thoughts == ["Lyon", "Autre"]  # the promise is extra
    assert not thinking.state.promised
    alerts = await thinking.alerts(NIGHT + timedelta(hours=3))
    assert "Tu avais promis" in next(a.notice for a in alerts if "maintiens" in a.notice)
    assert "faut-il accepter lyon" in str(provider.requests[0].messages).lower()


async def test_following_up_is_planned_checked_and_cancelled(journal: SqliteJournal) -> None:
    thinking = thinking_for(journal, ScriptedProvider(), EVENING)
    tools = {t.name: t for t in thinking_tools(thinking)}
    ctx = ToolContext(OWNER_THREAD, "c", context_trust=Trust.OWNER)
    plan = tools["follow_up"]

    def args(at: str) -> object:
        return plan.parse(
            json.dumps({"about": "son entretien", "at": at, "question": "Alors, cet entretien ?"})
        )

    assert (await plan.handler(args("2026-10-19 12:00"), ctx)).is_error
    assert (await plan.handler(args("jeudi"), ctx)).is_error
    done = await plan.handler(args("2026-10-22 14:00"), ctx)
    assert "jeudi 22/10 à 14:00" in done.content
    (follow_up,) = thinking.state.follow_ups.values()

    before = datetime(2026, 10, 22, 13, 0, tzinfo=PARIS)
    after = datetime(2026, 10, 22, 14, 30, tzinfo=PARIS)
    assert await thinking.alerts(before) == []
    (alert,) = await thinking.alerts(after)
    assert "Alors, cet entretien ?" in alert.notice and "SILENCE" in alert.notice
    assert await thinking.alerts(after + timedelta(days=2)) == []  # stale: not asked late

    listing = await tools["follow_ups"].handler(tools["follow_ups"].parse("{}"), ctx)
    assert follow_up.id in listing.content
    cancel = tools["follow_up_cancel"]
    await cancel.handler(cancel.parse(json.dumps({"follow_up": follow_up.id})), ctx)
    assert await thinking.alerts(after) == []


async def test_what_the_night_writes_is_journaled_with_its_source(journal: SqliteJournal) -> None:
    await a_day(journal)
    thinking = thinking_for(
        journal,
        night_answer(
            thoughts=[{"question": "Q", "new": True, "thought": "T"}],
            follow_ups=[{"about": "a", "at": "2026-10-22 15:00", "question": "Alors ?"}],
        ),
        NIGHT,
    )
    await thinking.tick()
    events = {e.kind: e for e in await journal.read(0, 10_000)}
    for kind in (THOUGHT, FOLLOWUP):
        assert events[kind].provenance == Provenance(
            source="orion:thinking", actor="orion:thinking"
        )
