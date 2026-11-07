"""The week's report (chantier 4): turns stopped short, tool errors, corrections and cost, read
from the journal, beside the week before."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from orion.adaptation.misses import MISSED, Miss, drafts, misses
from orion.adaptation.week import report, week, weeks
from orion.core.events import NewEvent
from orion.core.journal import SqliteJournal
from orion.runtime import OWNER_THREAD
from tests.conftest import make_event
from tests.fakes import ScriptedProvider, reply
from tests.test_runtime_cache import at, make_orion

PARIS = ZoneInfo("Europe/Paris")


async def test_the_week_says_what_went_wrong_and_what_it_cost(journal: SqliteJournal) -> None:
    provider = ScriptedProvider(
        reply("C'est lancé : une mission toutes les 6 h."), reply("Pardon.")
    )
    orion = make_orion(journal, provider)
    await orion.start()
    await orion.handle_owner_message(OWNER_THREAD, "le resto est ouvert ?", "app", now=at(0))
    await orion.handle_owner_message(
        OWNER_THREAD, "non c'est pas ce que je voulais", "app", now=at(1)
    )
    await journal.append(
        [
            make_event(
                MISSED,
                at=datetime.now(PARIS).isoformat(),
                asked="le resto est ouvert ?",
                answered="C'est lancé : une mission toutes les 6 h.",
                said="non c'est pas ce que je voulais",
                why="J'ai lancé une mission au lieu de répondre.",
            ),
            make_event("turn.finished", thread_id=OWNER_THREAD, reason="max_steps"),
            make_event("turn.finished", thread_id="veille", reason="budget_exhausted"),
            make_event("tool.executed", tool="web_search", is_error=True),
            make_event("tool.executed", tool="web_search", is_error=True),
            make_event("tool.executed", tool="mail", is_error=False),
            make_event("spend.recorded", cost_usd=0.30, foreground=True, session="orion:owner"),
            make_event("spend.recorded", cost_usd=0.20, foreground=False, session="orion:veille"),
        ]
    )
    now = datetime.now(PARIS) + timedelta(minutes=1)
    this, before = weeks(await journal.read(0, 100_000), PARIS, now)
    assert this.owner_turns == 3 and this.turns == 4  # two real turns, two written above
    assert this.stopped == {"max_steps": 1, "budget_exhausted": 1} and this.stopped_owner == 1
    assert this.tool_errors == {"web_search": 2} and this.tool_calls == 3
    assert this.corrected == 1 and this.misses[0].asked == "le resto est ouvert ?"
    assert this.background_usd >= 0.20 and this.cost_by["veille"] >= 0.20
    assert before.turns == 0 and before.cost_usd == 0

    text = report(this, before)
    assert "à court de pas : 1" in text and "web_search : 2" in text
    payload = this.payload(before)
    assert payload["corrected"] == 1 and isinstance(payload["before"], dict)

    later = week(await journal.read(0, 100_000), PARIS, now + timedelta(days=8))
    assert later.turns == 0 and not later.misses  # a week later, all of it is the week before


async def test_an_exchange_noted_by_two_nights_is_one_miss(journal: SqliteJournal) -> None:
    when = datetime(2026, 10, 28, 21, 1, tzinfo=PARIS).isoformat()
    exchange = {"at": when, "asked": "moins d'emojis", "answered": "ok 👌", "said": "j'avais dit"}
    await journal.append(
        [
            make_event(MISSED, **exchange, why="J'ai encore mis un emoji."),
            make_event(MISSED, **exchange, why="J'ai remis un emoji."),  # the pass, run again
        ]
    )
    (miss,) = misses(await journal.read(0, 100), PARIS)
    assert "encore" in miss.why


async def test_the_weeks_misses_come_out_as_cases_to_accept(journal: SqliteJournal) -> None:
    """ORION-8: 89 misses in 14 days, none became a case. Each one of the week is a draft."""
    now = datetime(2026, 11, 6, 9, 0, tzinfo=PARIS)

    def missed(days_ago: int, asked: str) -> NewEvent:
        when = (now - timedelta(days=days_ago)).isoformat()
        return make_event(
            MISSED, at=when, asked=asked, answered="Voilà !\nC'est fait.", said="non", why="Rien."
        )

    await journal.append([missed(10, "le vieux"), missed(2, "le récent"), missed(1, "l'autre")])
    found = misses(await journal.read(0, 100), PARIS)
    text = drafts(found, now - timedelta(days=7), now)
    assert "le récent" in text and "l'autre" in text and "le vieux" not in text
    cases = ast.parse(f"CASES = (\n{text}\n)").body[0]  # pastes into CASES as it is
    assert isinstance(cases, ast.Assign) and isinstance(cases.value, ast.Tuple)
    assert len(cases.value.elts) == 2


def test_two_misses_in_the_same_minute_get_two_case_names() -> None:
    now = datetime(2026, 11, 6, 9, 0, 10, tzinfo=PARIS)
    found = [
        Miss(now, "un", "Réponse A", "non", "?"),
        Miss(now + timedelta(seconds=30), "deux", "Réponse B", "non", "?"),
    ]
    text = drafts(found, now - timedelta(days=1), now + timedelta(days=1))
    cases = ast.parse(f"CASES = (\n{text}\n)").body[0]
    assert isinstance(cases, ast.Assign) and isinstance(cases.value, ast.Tuple)
    names = [
        k.value.value
        for case in cases.value.elts
        if isinstance(case, ast.Call)
        for k in case.keywords
        if k.arg == "name" and isinstance(k.value, ast.Constant)
    ]
    assert len(names) == 2 and len(set(names)) == 2


async def test_the_week_shows_what_was_really_done_and_the_noise(journal: SqliteJournal) -> None:
    """ORI-013: confirmed, failed, uncertain, and how often Orion spoke for nothing."""
    await journal.append(
        [
            make_event("tool.executed", tool="mail", outcome="CONFIRMED", action_id="mail:1"),
            make_event("tool.executed", tool="book", outcome="UNCERTAIN", action_id="book:c2"),
            make_event("goal.reached", goal_id="g1"),
            make_event("initiative.delivered", key="watch:x", day="2026-11-07"),
            make_event("initiative.suppressed", key="watch:y", day="2026-11-07"),
        ]
    )
    now = datetime.now(PARIS) + timedelta(minutes=1)
    this, before = weeks(await journal.read(0, 100_000), PARIS, now)
    assert this.outcomes == {"CONFIRMED": 1, "UNCERTAIN": 1}
    assert this.goals_reached == 1 and this.spoke == 1 and this.held == 1
    assert "book: book:c2" in report(this, before)


async def test_a_turn_that_went_wrong_lowers_its_roles_rate_on_its_model(
    journal: SqliteJournal,
) -> None:
    """ORI-020: measure before routing; success per role and model, read from the journal."""
    from orion.core.events import Provenance, Trust

    def on(turn: str, kind: str, **payload: object) -> NewEvent:
        prov = Provenance(source="test:1", actor="test:runner")
        return NewEvent(kind=kind, payload=payload, provenance=prov, trust=Trust.SYSTEM,
                        correlation_id=turn)  # fmt: skip

    await journal.append(
        [
            on("t1", "model.usage", model="fast"),
            on("t1", "turn.finished", thread_id="tasks:abc", reason="completed"),
            on("t2", "model.usage", model="fast"),
            on("t2", "tool.executed", tool="mail_send", outcome="FAILED"),
            on("t2", "turn.finished", thread_id="tasks:def", reason="completed"),
        ]
    )
    this = week(await journal.read(0, 1000), PARIS, datetime.now(PARIS) + timedelta(minutes=1))
    assert this.success[("tasks", "fast")] == [1, 2]
