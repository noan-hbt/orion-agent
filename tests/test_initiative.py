from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from orion.config import AgentConfig
from orion.context.compiler import ContextCompiler
from orion.core.events import Provenance, Trust
from orion.core.journal import SqliteJournal
from orion.guardian.policy import Guardian
from orion.initiative.engine import (
    DELIVERED,
    SUPPRESSED,
    InitiativeEngine,
    InitiativeSettings,
    digest_lines,
)
from orion.initiative.timers import TimerProjection, Timers
from orion.initiative.tools import reminder_tools
from orion.memory.model import CommitmentStatus
from orion.memory.service import MemoryService, Origin
from orion.memory.store import Commitment, MemoryStore
from orion.runtime import Orion
from orion.tools.base import ToolRegistry
from orion.tools.builtin import builtin_tools
from tests.fakes import ScriptedProvider, call_tools, fail, reply
from tests.test_runtime_cache import assert_prefix_stable

PARIS = ZoneInfo("Europe/Paris")
OWNER = Origin(actor="orion:core", source="terminal:owner", trust=Trust.OWNER)
OWNER_PROVENANCE = Provenance(source="terminal:owner", actor="owner:main")
SETTINGS = InitiativeSettings(
    digests=(time(8, 30), time(19, 0)), quiet_start=time(22, 30), quiet_end=time(8, 0)
)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=PARIS)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class Inbox:
    def __init__(self, available: bool = True) -> None:
        self.messages: list[str] = []
        self.is_available = available

    async def available(self) -> bool:
        return self.is_available

    async def notify(self, text: str, correlation_id: str) -> None:
        self.messages.append(text)


class World:
    def __init__(self, journal: SqliteJournal, memory: MemoryService, clock: Clock) -> None:
        self.journal = journal
        self.memory = memory
        self.clock = clock
        self.provider = ScriptedProvider()
        self.timers = Timers(journal, TimerProjection())
        self.inbox = Inbox()
        self.orion = Orion(
            journal=journal,
            provider=self.provider,
            registry=ToolRegistry([*builtin_tools(), *reminder_tools(self.timers, clock)]),
            guardian=Guardian(),
            compiler=ContextCompiler("CONSTITUTION"),
            agent=AgentConfig(),
            memory=memory,
        )

    def engine(self, settings: InitiativeSettings = SETTINGS) -> InitiativeEngine:
        return InitiativeEngine(
            self.orion,
            self.journal,
            self.timers,
            self.inbox,
            settings,
            memory=self.memory.store,
            clock=self.clock,
        )


@pytest.fixture
async def world(journal: SqliteJournal) -> AsyncIterator[World]:
    store = MemoryStore(":memory:")
    await store.open()
    world = World(journal, MemoryService(journal, store), Clock(at(19, 18)))
    await world.orion.start()
    yield world
    await store.close()


async def kinds(journal: SqliteJournal, kind: str) -> list[dict[str, object]]:
    return [e.payload for e in await journal.read(0, 10_000) if e.kind == kind]


async def test_reminder_round_trip_fires_once_even_across_restarts(world: World) -> None:
    world.provider.extend(
        call_tools(("remind_me", {"when": "2026-10-20T09:00", "what": "Appeler le garagiste"})),
        reply("Je te le rappelle demain à 9h."),
    )
    await world.orion.handle_owner_message(
        "owner", "rappelle-moi d'appeler le garagiste demain 9h", "terminal", now=world.clock.now
    )
    [timer] = world.timers.projection.pending()
    assert timer.due_at == at(20, 9) and timer.trust is Trust.OWNER

    assert await world.engine().tick() == []  # not yet
    world.clock.now = at(20, 9, 0)
    world.provider.extend(reply("Hé, le garagiste. C'est maintenant que tu voulais l'appeler."))
    assert await world.engine().tick() == [f"timer:{timer.id}"]
    assert world.inbox.messages == ["Hé, le garagiste. C'est maintenant que tu voulais l'appeler."]
    notice = world.provider.requests[-1].messages[-1]["content"]
    assert (
        '<initiative pourquoi="tu m\'as demandé de te le rappeler mardi 2026-10-20 à 09:00">'
        in notice
    )
    assert "· initiative]" in notice
    assert world.timers.projection.pending() == []

    world.clock.now = at(20, 9, 1)
    assert await world.engine().tick() == []  # a fresh engine (restart) does not repeat it
    assert_prefix_stable(world.provider)


async def test_remind_me_refuses_the_past(world: World) -> None:
    world.provider.extend(
        call_tools(("remind_me", {"when": "2026-10-19T08:00", "what": "trop tard"})),
        reply("Raté, c'est passé."),
    )
    await world.orion.handle_owner_message(
        "owner", "rappelle-moi ce matin", "terminal", now=world.clock.now
    )
    assert world.timers.projection.pending() == []
    tool_message = world.provider.requests[-1].messages[-1]
    assert "in the past" in str(tool_message["content"])


async def test_remind_me_by_call_is_refused_while_the_phone_is_off(world: World) -> None:
    world.provider.extend(
        call_tools(("remind_me", {"when": "2026-10-19T19:00", "what": "réveil", "by_call": True})),
        reply("Les appels sont coupés, je t'écris à la place ?"),
    )
    await world.orion.handle_owner_message(
        "owner", "appelle-moi à 19h", "terminal", now=world.clock.now
    )
    assert world.timers.projection.pending() == []
    assert "Calls are off" in str(world.provider.requests[-1].messages[-1]["content"])


async def test_explicit_reminders_ignore_quiet_hours_and_survive_model_failure(
    world: World,
) -> None:
    await world.timers.schedule(
        at(19, 23, 30), "Sortir la poubelle", "owner", OWNER_PROVENANCE, Trust.OWNER
    )
    world.clock.now = at(19, 23, 31)
    world.provider.extend(fail("provider down"))
    await world.engine().tick()
    assert world.inbox.messages == ["Rappel : Sortir la poubelle"]


async def test_reminder_is_kept_when_no_channel_is_available(world: World) -> None:
    await world.timers.schedule(at(19, 18, 30), "Truc", "owner", OWNER_PROVENANCE, Trust.OWNER)
    world.clock.now = at(19, 18, 31)
    world.inbox.is_available = False
    assert await world.engine().tick() == []
    assert len(world.timers.projection.pending()) == 1
    world.inbox.is_available = True
    world.provider.extend(reply("Truc !"))
    assert len(await world.engine().tick()) == 1


async def test_digest_speaks_only_when_there_is_something_new(world: World) -> None:
    await world.memory.add_commitment(
        "Envoyer le devis à Paul", OWNER, debtor="owner", creditor="Paul", due="2026-10-20"
    )
    world.clock.now = at(20, 8, 31)
    world.provider.extend(reply("Petit point du matin : le devis pour Paul, c'est aujourd'hui."))
    engine = world.engine()
    assert await engine.tick() == ["digest:2026-10-20:08:30"]
    notice = world.provider.requests[-1].messages[-1]["content"]
    assert "Envoyer le devis à Paul, pour aujourd'hui" in notice

    world.clock.now = at(20, 19, 5)  # evening: already mentioned today, nothing new
    assert await engine.tick() == []
    [suppressed] = await kinds(world.journal, SUPPRESSED)
    assert suppressed["reason"] == "nothing to say"
    assert len(world.provider.requests) == 1  # no model call for an empty digest


async def test_quiet_hours_budget_and_silence(world: World) -> None:
    await world.memory.add_commitment(
        "Payer le loyer", OWNER, debtor="owner", creditor="Proprio", due="2026-10-20"
    )
    quiet = InitiativeSettings(
        digests=(time(23, 0),), quiet_start=time(22, 30), quiet_end=time(8, 0)
    )
    world.clock.now = at(20, 23, 5)
    assert await world.engine(quiet).tick() == []
    assert await kinds(world.journal, SUPPRESSED) == []  # deferred, not dropped

    broke = InitiativeSettings(digests=(time(9, 0),), daily_budget=0)
    world.clock.now = at(21, 9, 1)
    assert await world.engine(broke).tick() == []
    assert (await kinds(world.journal, SUPPRESSED))[-1]["reason"] == "daily attention budget spent"

    world.clock.now = at(22, 9, 1)
    world.provider.extend(reply("SILENCE"))
    assert await world.engine(InitiativeSettings(digests=(time(9, 0),))).tick() == []
    assert (await kinds(world.journal, SUPPRESSED))[-1]["reason"] == "model chose silence"
    assert world.inbox.messages == []
    assert await kinds(world.journal, DELIVERED) == []


async def test_heads_up_before_a_timed_commitment(world: World) -> None:
    await world.memory.add_commitment(
        "Call avec Marc", OWNER, debtor="owner", creditor="Marc", due="2026-10-19T18:40:00+02:00"
    )
    world.provider.extend(reply("Call avec Marc dans 40 min."))
    assert await world.engine().tick() == [
        f"heads-up:{(await world.memory.store.commitments())[0].id}"
    ]


def _c(cid: str, due: str, debtor: str = "owner", creditor: str = "Paul") -> Commitment:
    return Commitment(
        cid, debtor, creditor, f"truc {cid}", due, CommitmentStatus.OPEN, None, Trust.OWNER
    )


def test_digest_lines_rules() -> None:
    today = date(2026, 10, 20)
    items = [
        _c("late", "2026-10-18"),
        _c("today", "2026-10-20T15:00"),
        _c("tomorrow", "2026-10-21"),
        _c("later", "2026-10-30"),
        _c("owed", "2026-10-19", debtor="Léa", creditor="owner"),
    ]
    morning = [cid for cid, _ in digest_lines(items, today, last_slot=False, last_mention={})]
    assert morning == ["late", "today", "owed"]
    evening = digest_lines(
        items, today, last_slot=True, last_mention={"late": today, "today": today, "owed": today}
    )
    assert [cid for cid, _ in evening] == ["tomorrow"]
    chased_recently = {"owed": today - timedelta(days=2)}
    assert "owed" not in [c for c, _ in digest_lines(items, today, False, chased_recently)]
    chased_long_ago = {"owed": today - timedelta(days=3)}
    assert "owed" in [c for c, _ in digest_lines(items, today, False, chased_long_ago)]


def test_quiet_hours_wrap_midnight() -> None:
    assert SETTINGS.is_quiet(time(23, 0)) and SETTINGS.is_quiet(time(7, 59))
    assert not SETTINGS.is_quiet(time(8, 0)) and not SETTINGS.is_quiet(time(22, 29))


def test_quiet_hours_do_not_hold_orion_back_while_the_owner_is_up() -> None:
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo

    from orion.initiative.engine import InitiativeEngine

    night = datetime(2026, 10, 20, 23, 30, tzinfo=ZoneInfo("Europe/Paris"))
    engine = object.__new__(InitiativeEngine)
    engine._orion = SimpleNamespace(owner_wrote_at=night - timedelta(minutes=5))  # pyright: ignore[reportAttributeAccessIssue]
    assert engine._owner_is_up(night)  # pyright: ignore[reportPrivateUsage]
    engine._orion = SimpleNamespace(owner_wrote_at=night - timedelta(hours=2))  # pyright: ignore[reportAttributeAccessIssue]
    assert not engine._owner_is_up(night)  # pyright: ignore[reportPrivateUsage]


def test_what_he_reads_at_night_counts_for_the_evening_not_the_next_day() -> None:
    """Contacts relayed at 1 a.m. spent the next day's budget: no digest at 8:30 (D98)."""
    from zoneinfo import ZoneInfo

    paris = ZoneInfo("Europe/Paris")
    settings = InitiativeSettings()
    assert settings.attention_day(datetime(2026, 10, 21, 1, 30, tzinfo=paris)) == date(2026, 10, 20)
    assert settings.attention_day(datetime(2026, 10, 21, 8, 30, tzinfo=paris)) == date(2026, 10, 21)
    assert settings.attention_day(datetime(2026, 10, 21, 23, 0, tzinfo=paris)) == date(2026, 10, 21)


async def test_a_failed_point_never_sends_oriens_notes_to_itself(world: World) -> None:
    """Seen for real: the evening point's draft failed, and « Pour la suite : Récap du jour
    (ouvre le point du soir par là…) — ce que j'ai fait pour lui » went out as it was."""
    from orion.initiative.engine import DigestLine

    class Notes:
        def __init__(self, plain: str) -> None:
            self.plain = plain

        async def digest(
            self, now: datetime, since: datetime, first_slot: bool, last_slot: bool
        ) -> list[DigestLine]:
            return [DigestLine("Note pour toi : ouvre le point par là.", plain=self.plain)]

    def engine(plain: str) -> InitiativeEngine:
        return InitiativeEngine(
            world.orion,
            world.journal,
            world.timers,
            world.inbox,
            InitiativeSettings(digests=(time(19, 0),)),
            memory=world.memory.store,
            clock=world.clock,
            digests=[Notes(plain)],
        )

    world.clock.now = at(20, 19, 1)
    world.provider.extend(fail(), fail())
    assert await engine("").tick() == []
    assert world.inbox.messages == []  # nothing he could read as it is: nothing sent
    world.clock.now = at(21, 19, 1)
    world.provider.extend(fail(), fail())
    assert await engine("Aujourd'hui, j'ai :\n- écrit à Angèle").tick() != []
    [sent] = world.inbox.messages
    assert sent == "Aujourd'hui, j'ai :\n- écrit à Angèle"
    assert "Note pour toi" not in sent
