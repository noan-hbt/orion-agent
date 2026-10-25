"""Orion is curious: once a week a lead of its own, a find only when it is worth it (D76)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.curiosity.module import FOUND, Curiosity, CuriositySettings, curiosity_tools
from orion.memory.model import Category
from orion.memory.service import MemoryService, Origin
from orion.memory.store import MemoryStore
from orion.tools.base import ToolContext
from orion.web.fetch import FetchError
from orion.web.search import Findings, Reading, Result
from tests.fakes import ScriptedProvider, fail, reply

PARIS = ZoneInfo("Europe/Paris")
SATURDAY = datetime(2026, 10, 24, 6, 5, tzinfo=PARIS)
OWNER = Origin(actor="owner:main", source="imessage:owner", trust=Trust.OWNER)
CTX = ToolContext("owner", "c")
LEAD = {
    "piste": "Comment les coureurs d'ultra gèrent le sommeil",
    "pourquoi": "Il veut se remettre à courir et dort mal.",
    "recherches": ["ultra-trail privation de sommeil étude"],
}


class FakeWeb:
    def __init__(self, *results: Result) -> None:
        self.results = list(results)
        self.searched: list[str] = []
        self.read_urls: list[str] = []

    async def search(self, query: str, correlation_id: str | None = None) -> Findings:
        self.searched.append(query)
        return Findings(self.results)

    async def read(self, url: str, correlation_id: str | None = None) -> Reading:
        self.read_urls.append(url)
        return Reading(url, "Le sommeil des ultra-traileurs", "Ils dorment par siestes de 20 min.")


ARTICLE = Result("https://example.org/ultra-sommeil", "Ultra et sommeil")


@pytest.fixture
async def memory(journal: SqliteJournal) -> AsyncIterator[MemoryService]:
    store = MemoryStore(":memory:")
    await store.open()
    service = MemoryService(journal, store)
    await service.remember(
        "Veut se remettre à courir.", OWNER, about=["owner"], category=Category.PREFERENCE
    )
    yield service
    await store.close()


def curious(
    journal: SqliteJournal,
    memory: MemoryService | None,
    provider: ScriptedProvider,
    web: FakeWeb,
    now: list[datetime],
) -> Curiosity:
    return Curiosity(
        journal, provider, web, memory, CuriositySettings(), "Europe/Paris", lambda: now[0]
    )


async def test_a_lead_a_search_and_one_find_worth_sharing(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    provider = ScriptedProvider(
        reply(json.dumps(LEAD, ensure_ascii=False)),
        reply(json.dumps({"garder": True, "page": 0, "question": "q", "pour_lui": "il dort mal"})),
    )
    web = FakeWeb(ARTICLE)
    now = [SATURDAY]
    curiosity = curious(journal, memory, provider, web, now)
    hunt = await curiosity.tick()
    assert hunt is not None and hunt.outcome == "found" and hunt.found is not None
    assert "Veut se remettre à courir." in str(provider.requests[0].messages[1]["content"])
    assert web.read_urls == [ARTICLE.url]
    found = [e for e in await journal.read() if e.kind == FOUND]
    assert found[0].trust is Trust.WEB  # what a page said stays web content
    assert await curiosity.tick() is None  # once a week

    assert await curiosity.alerts(SATURDAY) == []  # not at dawn
    (alert,) = await curiosity.alerts(SATURDAY.replace(hour=11))
    assert ARTICLE.url in alert.notice and "ton avis" in alert.notice
    assert alert.trust is Trust.WEB and alert.importance < 0.3

    _, _, verdict, listing = curiosity_tools(curiosity)
    await verdict.handler(
        verdict.parse(json.dumps({"find": hunt.found.id, "verdict": "plus de ça"})), CTX
    )
    assert await curiosity.alerts(SATURDAY.replace(hour=11)) == []  # answered
    shown = await listing.handler(listing.parse("{}"), CTX)
    assert "il a dit « plus de ça »" in shown.content


async def test_most_weeks_nothing_is_worth_it_and_nothing_is_sent(
    journal: SqliteJournal,
) -> None:
    provider = ScriptedProvider(
        reply(json.dumps(LEAD, ensure_ascii=False)), reply(json.dumps({"garder": False}))
    )
    now = [SATURDAY]
    curiosity = curious(journal, None, provider, FakeWeb(ARTICLE), now)
    hunt = await curiosity.tick()
    assert hunt is not None and hunt.outcome == "not worth it"
    assert await curiosity.alerts(SATURDAY.replace(hour=11)) == []
    # the lead is remembered, not to follow it again
    assert "rien trouvé qui vaille" in curiosity.past()


async def test_a_made_up_page_number_finds_nothing(
    journal: SqliteJournal,
) -> None:
    provider = ScriptedProvider(
        reply(json.dumps(LEAD, ensure_ascii=False)),
        reply(json.dumps({"garder": True, "page": 7})),
    )
    now = [SATURDAY]
    curiosity = curious(journal, None, provider, FakeWeb(ARTICLE), now)
    hunt = await curiosity.tick()
    assert hunt is not None and hunt.outcome == "not worth it"


async def test_the_hunt_waits_for_its_day_and_catches_up_a_missed_week(
    journal: SqliteJournal,
) -> None:
    now = [datetime(2026, 10, 22, 12, 0, tzinfo=PARIS)]  # a Thursday, first start
    provider = ScriptedProvider(reply(json.dumps({"piste": ""})))
    curiosity = curious(journal, None, provider, FakeWeb(), now)
    assert await curiosity.tick() is None  # the first hunt waits for Saturday
    now[0] = SATURDAY.replace(hour=5)
    assert await curiosity.tick() is None  # not before its time
    now[0] = SATURDAY
    hunt = await curiosity.tick()
    assert hunt is not None and hunt.outcome == "no lead"
    now[0] = datetime(2026, 11, 2, 9, 0, tzinfo=PARIS)  # Monday: Saturday 31 was missed
    provider.extend(reply(json.dumps({"piste": ""})))
    assert await curiosity.tick() is not None
    assert await curiosity.tick() is None


async def test_a_week_the_model_was_out_of_reach_is_hunted_later(
    journal: SqliteJournal,
) -> None:
    now = [SATURDAY]
    provider = ScriptedProvider(fail("down", retryable=True))
    curiosity = curious(journal, None, provider, FakeWeb(), now)
    hunt = await curiosity.tick()
    assert hunt is not None and hunt.outcome == "model unavailable"
    provider.extend(reply(json.dumps({"piste": ""})))
    again = await curiosity.tick()  # not taken as this week's hunt
    assert again is not None and again.outcome == "no lead"
    assert await curiosity.tick() is None


async def test_subjects_he_names_steer_the_lead_and_what_he_refuses_too(
    journal: SqliteJournal,
) -> None:
    provider = ScriptedProvider(reply(json.dumps({"piste": ""})))
    now = [SATURDAY]
    curiosity = curious(journal, None, provider, FakeWeb(), now)
    interest, drop, _, _ = curiosity_tools(curiosity)
    await interest.handler(interest.parse('{"subject": "les villes sans voiture"}'), CTX)
    said = await interest.handler(
        interest.parse('{"subject": "la crypto", "stance": "moins"}'), CTX
    )
    assert "de côté" in said.content
    await curiosity.tick()
    prompt = str(provider.requests[0].messages[1]["content"])
    assert "à suivre : les villes sans voiture" in prompt and "à éviter : la crypto" in prompt
    (first, _) = curiosity.state.interests
    assert not (await drop.handler(drop.parse(json.dumps({"interest": first})), CTX)).is_error
    assert first not in curiosity.state.interests


class PdfWeb(FakeWeb):
    async def read(self, url: str, correlation_id: str | None = None) -> Reading:
        raise FetchError(f"{url} is not text (application/pdf)")


async def test_a_page_that_cannot_be_read_brings_nothing(journal: SqliteJournal) -> None:
    provider = ScriptedProvider(
        reply(json.dumps(LEAD, ensure_ascii=False)),
        reply(json.dumps({"garder": True, "page": 0})),
    )
    hunt = await curious(journal, None, provider, PdfWeb(ARTICLE), [SATURDAY]).tick()
    assert hunt is not None and hunt.outcome == "page unreadable" and hunt.found is None
