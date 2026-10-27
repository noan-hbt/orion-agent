"""The watch (D150): a few times a day, Orion steps back and looks at the owner's life as a
whole; what he concludes reaches the owner as an alert, now or at the next digest."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from orion.config import STRONG_MODEL, AgentConfig
from orion.context.compiler import ContextCompiler
from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.guardian.policy import WATCH_THREAD, Guardian
from orion.memory.service import MemoryService, Origin
from orion.memory.store import MemoryStore
from orion.runtime import OWNER_THREAD, Orion
from orion.tools.base import ToolRegistry
from orion.watch.module import POINT_IMPORTANCE, Watch, WatchSettings, conclusion
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
