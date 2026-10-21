"""The people in the owner's life: meetings prepared, birthdays, staying in touch."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from orion.calendar.ics import Occurrence, Standing
from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.memory.model import Category
from orion.memory.service import MemoryService, Origin
from orion.memory.store import MemoryStore
from orion.relations.module import Relations, RelationSettings, prepared_at, relation_tools
from orion.tools.base import Tool, ToolContext

PARIS = ZoneInfo("Europe/Paris")
TUESDAY = datetime(2026, 10, 20, 15, 0, tzinfo=PARIS)
OWNER = Origin(actor="owner:main", source="imessage:owner", trust=Trust.OWNER)
CTX = ToolContext("owner", "c")


class Agenda:
    def __init__(self, *events: tuple[str, datetime]) -> None:
        self.events = [
            Occurrence(f"k{i}", start, start + timedelta(hours=1), False, Standing.ACCEPTED, title)
            for i, (title, start) in enumerate(events)
        ]

    def between(self, start: datetime, end: datetime) -> list[tuple[str, Occurrence]]:
        return [("perso", o) for o in self.events if o.end > start and o.start < end]


@pytest.fixture
async def memory(journal: SqliteJournal) -> AsyncIterator[MemoryService]:
    store = MemoryStore(":memory:")
    await store.open()
    service = MemoryService(journal, store)
    await service.remember(
        "Paul Martin cherche du travail depuis son licenciement.",
        OWNER,
        about=["Paul Martin"],
        category=Category.PERSON,
    )
    await service.add_commitment(
        "lui passer le contact de Julien", OWNER, debtor="owner", creditor="Paul Martin"
    )
    yield service
    await store.close()


def relations_for(
    journal: SqliteJournal, memory: MemoryService, now: list[datetime], agenda: Any = None
) -> tuple[Relations, dict[str, Tool]]:
    relations = Relations(
        journal, memory, RelationSettings(), "Europe/Paris", ["Noan"], agenda, lambda: now[0]
    )
    return relations, {t.name: t for t in relation_tools(relations)}


async def call(tools: dict[str, Tool], name: str, **args: object) -> str:
    tool = tools[name]
    result = await tool.handler(tool.parse(json.dumps(args)), CTX)
    assert not result.is_error, result.content
    return result.content


def test_a_morning_meeting_is_prepared_the_evening_before() -> None:
    settings = RelationSettings()
    morning = datetime(2026, 10, 22, 10, 0, tzinfo=PARIS)
    assert prepared_at(morning, settings) == datetime(2026, 10, 21, 19, 0, tzinfo=PARIS)
    lunch = datetime(2026, 10, 22, 12, 30, tzinfo=PARIS)
    assert prepared_at(lunch, settings) == datetime(2026, 10, 22, 9, 0, tzinfo=PARIS)


async def test_seeing_someone_in_the_calendar_brings_back_what_matters(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    breakfast = datetime(2026, 10, 21, 9, 30, tzinfo=PARIS)
    now = [TUESDAY]
    agenda = Agenda(
        ("Petit-déj avec Paul", breakfast), ("Dentiste", breakfast + timedelta(hours=3))
    )
    relations, _ = relations_for(journal, memory, now, agenda)
    assert await relations.alerts(TUESDAY) == []  # too early
    (alert,) = await relations.alerts(TUESDAY.replace(hour=19, minute=5))
    assert "Il voit Paul Martin mercredi à 09:30 (Petit-déj avec Paul)" in alert.notice
    assert "cherche du travail" in alert.notice and "contact de Julien" in alert.notice
    assert "SILENCE" in alert.notice and alert.expires_at == breakfast
    assert await relations.alerts(breakfast + timedelta(minutes=1)) == []


async def test_a_meeting_the_owner_mentions_is_prepared_too(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    now = [TUESDAY]
    relations, tools = relations_for(journal, memory, now)
    said = await call(tools, "meeting_soon", person="Paul", at="2026-10-22 12:30", about="déjeuner")
    assert "jeudi à 09:00" in said
    (alert,) = await relations.alerts(datetime(2026, 10, 22, 9, 15, tzinfo=PARIS))
    assert "Il voit Paul jeudi à 12:30 (déjeuner)" in alert.notice
    assert "licenciement" in alert.notice  # « Paul » found as Paul Martin


async def test_birthdays_come_in_time_and_on_the_day(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    now = [TUESDAY]
    relations, tools = relations_for(journal, memory, now)
    await call(tools, "person_birthday", person="Maman", month=10, day=25, year=1970)
    ahead = await relations.alerts(datetime(2026, 10, 22, 10, 30, tzinfo=PARIS))
    assert len(ahead) == 1 and "Dans 3 jours (dimanche)" in ahead[0].notice
    assert "(56 ans)" in ahead[0].notice
    (day,) = await relations.alerts(datetime(2026, 10, 25, 9, 30, tzinfo=PARIS))
    assert "C'est aujourd'hui l'anniversaire de Maman" in day.notice
    assert day.key != ahead[0].key and day.importance > ahead[0].importance


async def test_the_owners_own_birthday_is_not_a_relation(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    relations, tools = relations_for(journal, memory, [TUESDAY])
    for name in ("Noan", "noan herbeth gomes"):
        tool = tools["person_birthday"]
        result = await tool.handler(
            tool.parse(json.dumps({"person": name, "month": 11, "day": 14, "year": 2006})), CTX
        )
        assert result.is_error and "remember" in result.content
    assert await relations.alerts(datetime(2026, 11, 14, 9, 30, tzinfo=PARIS)) == []
    assert not relations.is_owner("Noanne")


async def test_too_long_without_seeing_someone_is_said_once_a_week(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    now = [TUESDAY]
    relations, tools = relations_for(journal, memory, now)
    await call(tools, "keep_in_touch", person="Julien", every_days=14)
    await call(tools, "saw_someone", person="Julien", how="un verre")
    evening = TUESDAY.replace(hour=18, minute=30)
    assert await relations.alerts(evening + timedelta(days=7)) == []
    (nudge,) = await relations.alerts(evening + timedelta(days=15))
    assert "tous les 14 jours" in nudge.notice and "depuis le 20/10" in nudge.notice
    same_week = await relations.alerts(evening + timedelta(days=16))
    assert [a.key for a in same_week] == [nudge.key]  # the engine's ledger says it once
    now[0] = evening + timedelta(days=16)
    await call(tools, "saw_someone", person="julien")
    assert await relations.alerts(evening + timedelta(days=16)) == []
    await call(tools, "keep_in_touch", person="Julien", every_days=0)
    assert relations.state.rhythms == {}
