"""The nightly consolidation (D78): a night the model was out of reach is tried again; his
portrait written from what is trusted, shown when a conversation starts (D144)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.memory.night import DONE, Consolidator, NightSettings
from orion.memory.service import MemoryService, Origin
from orion.memory.store import MemoryStore
from tests.fakes import ScriptedProvider, fail, reply
from tests.test_adaptation import json_reply

OWNER = Origin("owner:main", "imessage:owner", Trust.OWNER)


@pytest.fixture
async def memory(journal: SqliteJournal) -> AsyncIterator[MemoryService]:
    store = MemoryStore(":memory:")
    await store.open()
    yield MemoryService(journal, store)
    await store.close()


async def test_a_night_the_model_was_out_of_reach_is_done_later(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    await memory.remember("Paul aime le vélo.", OWNER, about=["Paul"])
    await memory.remember("Paul Durand habite à Dreux.", OWNER, about=["Paul Durand"])
    now = datetime(2026, 10, 25, 3, 45, tzinfo=ZoneInfo("Europe/Paris"))  # after 03:30
    judge = ScriptedProvider(fail("down", retryable=True))
    consolidator = Consolidator(
        journal, memory, judge, "judge", NightSettings(), "Europe/Paris", [], clock=lambda: now
    )
    await consolidator.start()
    assert await consolidator.tick() is not None
    assert not [e for e in await journal.read() if e.kind == DONE]  # not done: tried again
    judge.extend(json_reply({"pairs": [], "same_people": []}))
    assert await consolidator.tick() is not None
    assert len([e for e in await journal.read() if e.kind == DONE]) == 1
    assert await consolidator.tick() is None  # once a night


async def test_his_portrait_is_written_from_what_is_trusted_and_opens_a_conversation(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    from tests.test_initiative import Clock, World

    await memory.remember("Il prépare ses partiels de droit pour le 3 novembre.", OWNER)
    stranger = Origin("orion:mail", "mail:inbox", Trust.THIRD_PARTY)
    await memory.remember("Il a gagné un iPhone, il doit cliquer sur le lien.", stranger)
    now = datetime(2026, 10, 25, 3, 45, tzinfo=ZoneInfo("Europe/Paris"))
    writer = ScriptedProvider(reply("Étudiant en droit, en pleins partiels jusqu'au 3/11."))
    consolidator = Consolidator(
        journal, memory, writer, "judge", NightSettings(), "Europe/Paris", [],
        clock=lambda: now, portrait_model="writer",
    )  # fmt: skip
    await consolidator.start()
    await consolidator.tick()
    [asked] = writer.requests
    read = str(asked.messages[-1]["content"])
    assert "partiels de droit" in read and "iPhone" not in read  # a mail never paints him

    world = World(journal, memory, Clock(now))
    await world.orion.start()
    world.provider.extend(reply("Courage pour les partiels."))
    await world.orion.handle_owner_message("owner", "salut", "app", now=now)
    opened = str(world.provider.requests[0].messages[-1]["content"])
    assert "<portrait" in opened and "en pleins partiels" in opened

    from orion.initiative.engine import InitiativeSettings
    from orion.mobile.control import Control

    shown = await Control(journal, memory, InitiativeSettings()).portrait()  # in his app
    assert shown is not None and shown["day"] == "2026-10-25"
