"""The week's report (chantier 4): turns stopped short, tool errors, corrections and cost, read
from the journal, beside the week before."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from orion.adaptation.misses import MISSED, misses
from orion.adaptation.week import report, week, weeks
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
