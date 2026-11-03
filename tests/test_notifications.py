from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, time
from pathlib import Path

import pytest

from orion.channels.inbox import OwnerInbox
from orion.config import AgentConfig, OrionConfig
from orion.context.compiler import ContextCompiler
from orion.core.events import NewEvent, Provenance, Trust
from orion.core.journal import SqliteJournal
from orion.guardian.policy import Guardian
from orion.initiative.engine import SUPPRESSED, Alert, InitiativeEngine, InitiativeSettings
from orion.memory.service import MemoryService
from orion.memory.store import MemoryStore
from orion.notifications.module import NotificationsModule
from orion.notifications.policy import NotificationError, Notifications
from orion.runtime import Orion
from orion.tools.base import ToolRegistry
from tests.fakes import ScriptedProvider, reply
from tests.test_inbox import Outlet
from tests.test_initiative import Clock, World, at, kinds

BY_HIM = Provenance(source="app", actor="owner:main")


@pytest.fixture
async def inbox(journal: SqliteJournal) -> OwnerInbox:
    orion = Orion(
        journal=journal,
        provider=ScriptedProvider(),
        registry=ToolRegistry([]),
        guardian=Guardian(),
        compiler=ContextCompiler("C"),
        agent=AgentConfig(),
    )
    return OwnerInbox(orion)


async def test_each_kind_rings_and_goes_as_he_chose(
    journal: SqliteJournal, inbox: OwnerInbox
) -> None:
    app, telegram = Outlet("mobile"), Outlet("telegram")
    inbox.register(app)
    inbox.register(telegram)
    notifications = Notifications(journal)
    inbox.policy = notifications
    names = inbox.outlet_names()

    await notifications.choose("point", "muette", None, BY_HIM, Trust.OWNER, names)
    await notifications.choose("initiative", "off", None, BY_HIM, Trust.OWNER, names)
    await notifications.choose("rappel", None, "partout", BY_HIM, Trust.OWNER, names)
    with pytest.raises(NotificationError):
        await notifications.choose("rappel", "off", None, BY_HIM, Trust.OWNER, names)

    assert await inbox.notify_as("Ton point", "c1", "point")
    assert not await inbox.notify_as("Une idée", "c2", "initiative")
    assert await inbox.notify_as("Le pain !", "c3", "rappel")
    assert app.sent == ["Ton point", "Le pain !"] and app.quiet == [True, False]
    assert telegram.sent == ["Le pain !"]


async def _world(journal: SqliteJournal) -> AsyncIterator[World]:
    store = MemoryStore(":memory:")
    await store.open()
    world = World(journal, MemoryService(journal, store), Clock(at(20, 23, 5)))
    await world.orion.start()
    yield world
    await store.close()


class Choosing:
    """An inbox with his choices: what the engine asks before writing."""

    def __init__(self, levels: dict[str, str]) -> None:
        self.levels = levels
        self.sent: list[tuple[str, str]] = []

    async def available(self) -> bool:
        return True

    async def notify(self, text: str, correlation_id: str) -> None:
        self.sent.append(("?", text))

    def level_of(self, kind: str) -> str:
        return self.levels.get(kind, "normale")

    async def notify_as(self, text: str, correlation_id: str, kind: str, level: str = "") -> bool:
        self.sent.append((kind, text))
        return True


class Alerts:
    def __init__(self, *alerts: Alert) -> None:
        self.list = list(alerts)

    async def alerts(self, now: datetime) -> list[Alert]:
        return self.list


async def test_a_kind_he_turned_off_is_not_even_written_and_urgent_passes_the_night(
    journal: SqliteJournal,
) -> None:
    async for world in _world(journal):
        choosing = Choosing({"initiative": "off", "depense": "urgente"})
        alerts = Alerts(
            Alert("watch:1", "Une idée pour demain", "ma veille", "", importance=0.9),
            Alert("spending:day:1", "Plafond atteint", "le plafond", "", importance=0.9),
        )
        engine = InitiativeEngine(
            world.orion,
            world.journal,
            world.timers,
            choosing,
            InitiativeSettings(digests=(), quiet_start=time(22, 30)),
            memory=world.memory.store,
            clock=world.clock,
            alerts=[alerts],
        )
        world.provider.extend(reply("Le plafond du jour est atteint."))
        delivered = await engine.tick()  # 23:05, quiet hours
        assert delivered == ["spending:day:1"]
        assert choosing.sent == [("depense", "Le plafond du jour est atteint.")]
        [dropped] = await kinds(world.journal, SUPPRESSED)
        assert dropped["key"] == "watch:1"


async def test_a_new_kind_is_one_line_once_he_turns_it_on(
    journal: SqliteJournal, inbox: OwnerInbox, tmp_path: Path
) -> None:
    app = Outlet("mobile")
    inbox.register(app)
    module = NotificationsModule(OrionConfig.model_validate({"data_dir": tmp_path}))
    module.notifications = Notifications(journal)
    inbox.policy = module.notifications
    done = NewEvent(
        kind="task.finished",
        payload={"task_id": "t_1", "how": "done", "summary": "Trois vols trouvés pour Lisbonne"},
        provenance=Provenance(source="task:t_1", actor="orion:tasks"),
        trust=Trust.SYSTEM,
    )
    [event] = await journal.append([done])
    await module._direct(inbox, event)  # pyright: ignore[reportPrivateUsage]
    assert app.sent == []  # off until he wants it

    await module.notifications.choose("tache", "urgente", None, BY_HIM, Trust.OWNER, ["mobile"])
    await module._direct(inbox, event)  # pyright: ignore[reportPrivateUsage]
    assert len(app.sent) == 1 and "Lisbonne" in app.sent[0]
