"""The nightly upkeep: his portrait written from what is trusted, shown when a conversation
starts (D144); what was not lasting becomes a situation."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.memory.night import Consolidator, NightSettings
from orion.memory.service import MemoryService, Origin
from orion.memory.store import MemoryStore
from tests.fakes import ScriptedProvider, reply
from tests.test_adaptation import json_reply

OWNER = Origin("owner:main", "imessage:owner", Trust.OWNER)


@pytest.fixture
async def memory(journal: SqliteJournal) -> AsyncIterator[MemoryService]:
    store = MemoryStore(":memory:")
    await store.open()
    yield MemoryService(journal, store)
    await store.close()


async def test_his_portrait_is_written_from_what_is_trusted_and_opens_a_conversation(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    from tests.test_initiative import Clock, World

    await memory.remember("Il prépare ses partiels de droit pour le 3 novembre.", OWNER)
    stranger = Origin("orion:mail", "mail:inbox", Trust.THIRD_PARTY)
    await memory.remember("Il a gagné un iPhone, il doit cliquer sur le lien.", stranger)
    now = datetime(2026, 10, 25, 3, 45, tzinfo=ZoneInfo("Europe/Paris"))
    writer = ScriptedProvider(
        json_reply({"temporary": []}),
        reply("Étudiant en droit, en pleins partiels jusqu'au 3/11."),
    )
    consolidator = Consolidator(
        journal, memory, writer, "judge", NightSettings(), "Europe/Paris",
        clock=lambda: now, portrait_model="writer",
    )  # fmt: skip
    await consolidator.start()
    await consolidator.tick()
    read = str(writer.requests[-1].messages[-1]["content"])
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


async def test_what_was_not_lasting_becomes_a_situation_and_ends_on_its_own(
    journal: SqliteJournal, memory: MemoryService
) -> None:
    """D149: a trip kept as a fact never died. The night makes it a situation, shown in
    `<maintenant>`; past its horizon it closes by itself; the extractor opens them directly."""
    from datetime import date

    from orion.memory.extractor import MemoryExtractor
    from tests.test_initiative import Clock, World

    await memory.remember("Il part à Lisbonne du 30/10 au 2/11 avec Léa.", OWNER, about=["Léa"])
    lasting = await memory.remember("Il est allergique aux noix.", OWNER)
    paris = ZoneInfo("Europe/Paris")
    night = datetime(2026, 10, 27, 3, 45, tzinfo=paris)
    sorter = ScriptedProvider(  # only the sorting asks
        json_reply({"temporary": [
            {"fact": 1, "still_going": True, "title": "Lisbonne avec Léa",
             "summary": "Il part à Lisbonne avec Léa.", "until": "2026-11-02"},
        ]}),
    )  # fmt: skip
    consolidator = Consolidator(
        journal, memory, sorter, "judge", NightSettings(), "Europe/Paris",
        clock=lambda: night,
    )  # fmt: skip
    await consolidator.start()
    report = await consolidator.tick()
    assert report is not None and len(report.situations) == 1
    assert [f.id for f in await memory.store.facts()] == [lasting]
    [trip] = memory.situations.current()
    assert trip.title == "Lisbonne avec Léa" and trip.until == date(2026, 11, 2)

    world = World(journal, memory, Clock(night))
    await world.orion.start()
    world.provider.extend(reply("Bon voyage !"))
    await world.orion.handle_owner_message("owner", "salut", "app", now=night)
    sent = str(world.provider.requests[0].messages[-1]["content"])
    assert "<maintenant>" in sent and "en cours" in sent and "Lisbonne avec Léa" in sent

    extractor = MemoryExtractor(
        ScriptedProvider(json_reply({"operations": [
            {"op": "update_situation", "situation_id": trip.id, "note": "Vol décalé à 9 h."},
            {"op": "open_situation", "title": "Mal de dos", "summary": "Il a mal au dos."},
        ]})),
        memory,
        "extractor",
    )  # fmt: skip
    applied = await extractor.consolidate(
        "mon vol est à 9h finalement, et j'ai mal au dos", "Noté.", OWNER, night.date(), []
    )
    assert len(applied.applied) == 2 and trip.notes == ["Vol décalé à 9 h."]

    later = datetime(2026, 11, 5, 3, 45, tzinfo=paris)
    consolidator._clock = lambda: later  # pyright: ignore[reportPrivateUsage]
    await consolidator.tick()
    assert [s.title for s in memory.situations.current()] == ["Mal de dos"]
    assert trip.ending == "expired"
