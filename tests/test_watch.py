"""The watch (D150): a few times a day, Orion steps back and looks at the owner's life as a
whole; what he concludes reaches the owner as an alert, now or at the next digest."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from orion.config import STRONG_MODEL, AgentConfig
from orion.context.compiler import ContextCompiler
from orion.core.events import NewEvent, Trust
from orion.core.journal import SqliteJournal
from orion.guardian.policy import WATCH_THREAD, Guardian
from orion.memory.service import MemoryService, Origin
from orion.memory.store import MemoryStore
from orion.runtime import OWNER_THREAD, Orion
from orion.tools.base import ToolRegistry
from orion.watch.module import POINT_IMPORTANCE, Watch, WatchSettings, conclusion
from tests.conftest import make_event
from tests.fakes import ScriptedProvider, reply

PARIS = ZoneInfo("Europe/Paris")
OWNER = Origin("owner:main", "imessage:owner", Trust.OWNER)


def test_a_conclusion_says_whether_it_goes_now() -> None:
    assert conclusion("SILENCE") is None
    assert conclusion("MAINTENANT : Ton dentiste est à 14 h.") == ("Ton dentiste est à 14 h.", True)
    assert conclusion("au point: Le passeport expire.") == ("Le passeport expire.", False)
    assert conclusion("Le passeport expire.") == ("Le passeport expire.", False)


async def test_orion_steps_back_sees_the_whole_and_tells_him(journal: SqliteJournal) -> None:
    store = MemoryStore(":memory:")
    await store.open()
    memory = MemoryService(journal, store)
    await memory.open_situation(
        "Lisbonne avec Léa", "Il part à Lisbonne avec Léa.", OWNER, until="2026-11-02"
    )
    provider = ScriptedProvider(reply("Bon courage pour ce soir."))
    orion = Orion(
        journal=journal,
        provider=provider,
        registry=ToolRegistry([]),
        guardian=Guardian(),
        compiler=ContextCompiler("C"),
        agent=AgentConfig(),
        memory=memory,
    )
    await orion.start()
    morning = datetime(2026, 10, 28, 9, 0, tzinfo=PARIS)
    await orion.handle_owner_message(
        OWNER_THREAD, "mon passeport expire en novembre", "app", now=morning
    )

    noon = datetime(2026, 10, 28, 12, 40, tzinfo=PARIS)
    watch = Watch(journal, memory, WatchSettings(), STRONG_MODEL, "Europe/Paris", lambda: noon)
    watch.orion = orion
    await watch.sync()
    provider.extend(reply("AU POINT : Ton passeport expire en novembre, et tu pars vendredi."))
    assert await watch.tick() == "Ton passeport expire en novembre, et tu pars vendredi."
    asked = provider.requests[-1]
    assert asked.model == STRONG_MODEL and asked.session_id == f"orion:{WATCH_THREAD}"
    seen = str(asked.messages[-1]["content"])
    assert "Lisbonne avec Léa" in seen and "passeport expire en novembre" in seen
    assert "MAINTENANT" in seen and "12:30" in seen
    assert await watch.tick() is None  # once per slot

    (alert,) = await watch.alerts(noon)
    assert alert.importance == POINT_IMPORTANCE and alert.why == "ta veille"
    assert not await watch.alerts(noon + timedelta(hours=9))  # stale by then

    evening = datetime(2026, 10, 28, 18, 45, tzinfo=PARIS)
    watch.clock = lambda: evening
    provider.extend(reply("SILENCE"))
    assert await watch.tick() is None
    told = str(provider.requests[-1].messages[-1]["content"])
    assert "il ne t'a rien dit depuis" in told  # only what is new since the last watch
    assert "Ton passeport expire en novembre, et tu pars vendredi." in told  # not said twice
    await store.close()


def _occurrence(key: str, start: datetime, title: str, standing: str = "own") -> dict[str, object]:
    return {
        "key": key,
        "start": start.isoformat(),
        "end": (start + timedelta(hours=1)).isoformat(),
        "all_day": False,
        "standing": standing,
        "title": title,
    }


async def _sync(journal: SqliteJournal, at: datetime, *changes: NewEvent) -> None:
    await journal.append(
        [
            *changes,
            make_event("calendar.synced", "calendar:perso", account="perso", at=at.isoformat()),
        ]
    )


def _seen(occurrence: dict[str, object]) -> NewEvent:
    return make_event(
        "calendar.event.seen", "calendar:perso", account="perso", occurrence=occurrence
    )


async def test_what_moves_in_his_life_starts_a_watch_in_between(journal: SqliteJournal) -> None:
    """D155: an agenda that moves within 48 h, or a mail Orion judged worth telling, starts a
    watch between the fixed ones; not on a calendar's first sync, not too often."""
    store = MemoryStore(":memory:")
    await store.open()
    memory = MemoryService(journal, store)
    provider = ScriptedProvider()
    orion = Orion(
        journal=journal,
        provider=provider,
        registry=ToolRegistry([]),
        guardian=Guardian(),
        compiler=ContextCompiler("C"),
        agent=AgentConfig(),
        memory=memory,
    )
    await orion.start()
    clock = [datetime(2026, 10, 28, 8, 0, tzinfo=PARIS)]
    watch = Watch(journal, memory, WatchSettings(), STRONG_MODEL, "Europe/Paris", lambda: clock[0])
    watch.orion = orion
    provider.extend(reply("SILENCE"))
    assert await watch.tick() is None  # the 07:30 watch, caught up

    dentist = datetime(2026, 10, 29, 10, 0, tzinfo=PARIS)
    far = datetime(2026, 11, 20, 10, 0, tzinfo=PARIS)
    await _sync(journal, clock[0], _seen(_occurrence("a@1", dentist, "Dentiste")))
    clock[0] = datetime(2026, 10, 28, 10, 30, tzinfo=PARIS)
    assert await watch.tick() is None  # a first sync is not news
    assert len(provider.requests) == 1

    await _sync(journal, clock[0], _seen(_occurrence("b@1", far, "Mariage de Paul")))
    assert await watch.tick() is None  # in three weeks: the fixed watches will see it

    moved = dentist + timedelta(hours=4)
    await _sync(
        journal,
        clock[0],
        make_event("calendar.event.gone", "calendar:perso", account="perso", key="a@1"),
        _seen(_occurrence("a@2", moved, "Dentiste")),
    )
    provider.extend(reply("MAINTENANT : Ton dentiste passe à 14 h demain, ça mord sur ton TD."))
    assert await watch.tick() == "Ton dentiste passe à 14 h demain, ça mord sur ton TD."
    seen = str(provider.requests[-1].messages[-1]["content"])
    assert "quelque chose a bougé" in seen and "« Dentiste » a bougé" in seen
    assert "Mariage de Paul" not in seen
    assert await watch.tick() is None  # what it saw is spent

    await journal.append(
        [
            make_event(
                "initiative.delivered",
                "orion:initiative",
                key="mail:m1",
                why="un mail de la préfecture vient d'arriver",
                day="2026-10-28",
            )
        ]
    )
    clock[0] = datetime(2026, 10, 28, 11, 0, tzinfo=PARIS)
    assert await watch.tick() is None  # too soon after the last one, and 12:30 is near
    clock[0] = datetime(2026, 10, 28, 12, 40, tzinfo=PARIS)
    provider.extend(reply("SILENCE"))
    assert await watch.tick() is None  # the fixed watch takes it
    told = str(provider.requests[-1].messages[-1]["content"])
    assert "préfecture" in told and "n'était pas prévue" not in told

    await _sync(journal, clock[0], _seen(_occurrence("c@1", moved + timedelta(hours=3), "Kiné")))
    clock[0] = datetime(2026, 10, 28, 15, 0, tzinfo=PARIS)
    provider.extend(reply("SILENCE"))
    await watch.tick()
    assert "« Kiné »" in str(provider.requests[-1].messages[-1]["content"])
    await store.close()


async def test_stirred_watches_have_a_ceiling(journal: SqliteJournal) -> None:
    store = MemoryStore(":memory:")
    await store.open()
    memory = MemoryService(journal, store)
    clock = [datetime(2026, 10, 28, 8, 0, tzinfo=PARIS)]
    settings = WatchSettings(at=[], stirred_per_day=1, stirred_gap_minutes=15)
    watch = Watch(journal, memory, settings, STRONG_MODEL, "Europe/Paris", lambda: clock[0])
    provider = ScriptedProvider(reply("SILENCE"), reply("SILENCE"))
    watch.orion = Orion(
        journal=journal,
        provider=provider,
        registry=ToolRegistry([]),
        guardian=Guardian(),
        compiler=ContextCompiler("C"),
        agent=AgentConfig(),
        memory=memory,
    )
    await watch.orion.start()
    for minutes, key in ((0, "x"), (60, "y")):
        await journal.append(
            [make_event("initiative.delivered", "orion:initiative", key=f"mail:{key}", day="d")]
        )
        clock[0] = datetime(2026, 10, 28, 9, 0, tzinfo=PARIS) + timedelta(minutes=minutes)
        await watch.tick()
    assert len(provider.requests) == 1  # one a day, here
    await store.close()
