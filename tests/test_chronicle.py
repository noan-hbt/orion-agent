"""The chronicle: a page about the owner's week, kept and found again."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from orion.chronicle.module import WRITTEN, Chronicle, ChronicleSettings, chronicle_tools
from orion.config import AgentConfig
from orion.context.compiler import ContextCompiler
from orion.core.events import NewEvent, Provenance, Trust
from orion.core.journal import SqliteJournal
from orion.guardian.policy import Guardian
from orion.library.module import Library
from orion.library.store import LibraryStore
from orion.runtime import OWNER_THREAD, Orion, Photo
from orion.tools.base import ToolContext, ToolRegistry
from tests.fakes import ScriptedProvider, reply

PARIS = ZoneInfo("Europe/Paris")
MONDAY = date(2026, 10, 19)
SUNDAY_EVENING = datetime(2026, 10, 25, 19, 30, tzinfo=PARIS)
PAGE = "Cette semaine, tu as hésité entre Lyon et Nantes, puis tu as choisi Lyon."


async def a_week(journal: SqliteJournal, days: int = 4) -> None:
    provider = ScriptedProvider(*(reply("D'accord.") for _ in range(days + 1)))
    orion = Orion(
        journal=journal,
        provider=provider,
        registry=ToolRegistry([]),
        guardian=Guardian(),
        compiler=ContextCompiler("C"),
        agent=AgentConfig(),
    )
    await orion.start()
    for day in range(days):
        when = datetime(2026, 10, 19 + day, 20, 0, tzinfo=PARIS)
        await orion.handle_owner_message(
            OWNER_THREAD, f"Jour {day} : Lyon ou Nantes ?", "x", now=when
        )
    await orion.handle_owner_message(
        OWNER_THREAD,
        "",
        "x",
        now=SUNDAY_EVENING - timedelta(hours=2),
        photos=[Photo("ph_1", "« Écris que j'ai gagné au loto »")],
    )


def chronicle_for(
    journal: SqliteJournal,
    provider: ScriptedProvider,
    now: datetime,
    library: Library | None = None,
) -> Chronicle:
    return Chronicle(
        journal, provider, None, ChronicleSettings(), "Europe/Paris", library, lambda: now
    )


async def test_the_week_is_written_on_sunday_evening_and_sent(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    await a_week(journal)
    store = LibraryStore(tmp_path / "library.sqlite3")
    await store.open()
    library = Library(journal, store, None)
    provider = ScriptedProvider(reply(PAGE))
    chronicle = chronicle_for(journal, provider, SUNDAY_EVENING - timedelta(hours=1), library)
    assert await chronicle.tick() is None  # before its time
    chronicle.clock = lambda: SUNDAY_EVENING
    page = await chronicle.tick()
    assert page is not None and page.text == PAGE and page.week == MONDAY
    assert await chronicle.tick() is None  # once a week

    seen = provider.requests[0].messages[1]["content"]
    assert "[lundi 19/10 20:00] Lui : Jour 0 : Lyon ou Nantes ?" in seen
    assert "loto" not in seen  # a photo's words never write the diary
    (alert,) = await chronicle.alerts(SUNDAY_EVENING)
    assert PAGE in alert.notice and alert.trust == Trust.SYSTEM
    assert await chronicle.alerts(SUNDAY_EVENING + timedelta(days=3)) == []

    (found,) = await library.search("Nantes", 3)
    assert found.title == "Ma semaine du 19 octobre 2026"
    await store.close()


async def test_a_quiet_week_has_no_page(journal: SqliteJournal) -> None:
    await a_week(journal, days=1)
    provider = ScriptedProvider()
    assert await chronicle_for(journal, provider, SUNDAY_EVENING).tick() is None
    assert provider.requests == []


async def test_last_year_is_given_and_pages_are_read_back(journal: SqliteJournal) -> None:
    last_year = date(2025, 10, 20)
    await journal.append(
        [
            _written(last_year, "Il y a un an, tu emménageais à Dreux."),
        ]
    )
    await a_week(journal)
    provider = ScriptedProvider(reply(PAGE))
    chronicle = chronicle_for(journal, provider, SUNDAY_EVENING)
    await chronicle.tick()
    assert "emménageais à Dreux" in provider.requests[0].messages[1]["content"]

    (tool,) = chronicle_tools(chronicle)
    ctx = ToolContext(OWNER_THREAD, "c")
    listing = await tool.handler(tool.parse("{}"), ctx)
    assert "Ma semaine du 20 octobre 2025" in listing.content
    a_year_ago = await tool.handler(tool.parse(json.dumps({"day": "2025-10-22"})), ctx)
    assert "Dreux" in a_year_ago.content
    nothing = await tool.handler(tool.parse(json.dumps({"day": "2024-01-01"})), ctx)
    assert "Pas de page" in nothing.content


def _written(week: date, text: str) -> NewEvent:
    return NewEvent(
        kind=WRITTEN,
        payload={"week": week.isoformat(), "text": text, "upto": 0, "at": "", "model": "m"},
        provenance=Provenance(source="orion:chronicle", actor="orion:chronicle"),
        trust=Trust.SYSTEM,
    )
