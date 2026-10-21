"""Missions: a goal worked on over days, in its own thread, reported only when there is news."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from orion.config import AgentConfig
from orion.context.compiler import ContextCompiler
from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.guardian.policy import Guardian
from orion.missions.module import MissionWork, mission_tools
from orion.missions.service import Missions
from orion.runtime import Orion
from orion.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from tests.fakes import ScriptedProvider, call_tools, reply

PARIS = ZoneInfo("Europe/Paris")
MONDAY = datetime(2026, 10, 19, 9, 0, tzinfo=PARIS)


class World:
    def __init__(self, journal: SqliteJournal, provider: ScriptedProvider) -> None:
        self.now = MONDAY
        self.missions = Missions(journal)
        tools = mission_tools(self.missions, lambda: self.now, max_active=3)
        self.tools = {t.name: t for t in tools}
        self.orion = Orion(
            journal=journal,
            provider=provider,
            registry=ToolRegistry(tools),
            guardian=Guardian(),
            compiler=ContextCompiler("C"),
            agent=AgentConfig(),
        )
        self.work = MissionWork(self.missions, self.orion, lambda: self.now, tick=60)

    async def use(self, name: str, ctx: ToolContext, **args: object) -> ToolResult:
        tool: Tool = self.tools[name]
        return await tool.handler(tool.parse(json.dumps(args)), ctx)


def seen(*items: tuple[str, str]) -> tuple[str, dict[str, object]]:
    return ("mission_seen", {"items": [{"key": k, "label": label} for k, label in items]})


async def test_a_mission_works_alone_and_reports_only_news(journal: SqliteJournal) -> None:
    provider = ScriptedProvider()
    world = World(journal, provider)
    await world.orion.start()
    owner = ToolContext("owner", "c0")
    started = await world.use(
        "mission_start", owner, goal="Un appart à Dreux", criteria="T2, < 700 €", every_hours=24
    )
    [mission] = await world.missions.active()
    assert mission.id in started.content

    # First run: two listings, both new; a report.
    provider.extend(
        call_tools(seen(("https://a.fr/1", "T2 650 €"), ("https://a.fr/2", "T3 720 €"))),
        reply("Deux annonces : un T2 à 650 € (https://a.fr/1) et un T3 à 720 €."),
    )
    assert await world.work.once() == [mission.id]
    brief = provider.requests[0].messages[-1]["content"]
    assert "Un appart à Dreux" in str(brief) and "T2, < 700 €" in str(brief)
    assert provider.requests[0].session_id == f"orion:mission:{mission.id}"  # its own thread
    assert await world.work.once() == []  # not due again before a day

    alerts = await world.work.alerts(world.now)
    assert [a.key for a in alerts] == [f"mission:{mission.id}:1"]
    assert "650 €" in alerts[0].notice

    # The owner turns one down, with a reason.
    await world.use(
        "mission_reject", owner, mission=mission.id, key="https://a.fr/2", why="trop cher"
    )

    # A day later: the same listings come back, nothing new: silence, no new alert.
    world.now += timedelta(days=1)
    provider.extend(
        call_tools(seen(("https://a.fr/1", "T2 650 €"), ("https://a.fr/2", "T3 720 €"))),
        reply("SILENCE"),
    )
    assert await world.work.once() == [mission.id]
    second = provider.requests[-2].messages
    assert "https://a.fr/2 : trop cher" in str(second)  # the run knows what was refused
    told = [m for m in provider.requests[-1].messages if m["role"] == "tool"][-1]
    assert "Rien de neuf" in str(told["content"])
    assert [a.key for a in await world.work.alerts(world.now)] == [f"mission:{mission.id}:1"]

    # The run remembers its own thread: the first run is still in view.
    assert "Deux annonces" in str(second)

    await world.use("mission_close", owner, mission=mission.id, outcome="trouvé")
    assert await world.missions.active() == []


async def test_what_a_mission_read_on_the_web_keeps_its_trust(journal: SqliteJournal) -> None:
    provider = ScriptedProvider()
    world = World(journal, provider)
    owner = ToolContext("owner", "c0")
    await world.use("mission_start", owner, goal="Guetter un prix", every_hours=1)
    [mission] = await world.missions.active()
    web = ToolContext(mission.thread, "c1", context_trust=Trust.WEB)
    await world.use("mission_note", web, note="Le site X affiche les prix TTC")
    [mission] = await world.missions.active()
    assert mission.trust is Trust.WEB  # its next brief is page content, not an order
    provider.extend(reply("Le prix a baissé : 89 €."))
    await world.work.once()
    [alert] = await world.work.alerts(world.now)
    assert alert.trust <= Trust.WEB


async def test_a_mission_called_off_while_it_runs_stops_there(journal: SqliteJournal) -> None:
    provider = ScriptedProvider()
    world = World(journal, provider)
    owner = ToolContext("owner", "c0")
    assert not world.work._woken.is_set()  # pyright: ignore[reportPrivateUsage]
    await world.use("mission_start", owner, goal="Des billets pour Amsterdam", every_hours=24)
    assert world.work._woken.is_set()  # pyright: ignore[reportPrivateUsage]  # its first run: now
    [mission] = await world.missions.active()
    # He says "leave it" while the run is under way (here, as its first step lands).
    provider.extend(
        call_tools(("mission_close", {"mission": mission.id, "outcome": "laisse tomber"})),
        call_tools(("mission_note", {"note": "Kombo marche bien"})),
        reply("Premier passage : 96 € l'A/R."),
    )
    assert await world.work.once() == [mission.id]
    assert len(provider.requests) == 2  # it stopped there: no third step, no report
    assert (await world.missions.get(mission.id)).notes == []  # pyright: ignore[reportOptionalMemberAccess]
    assert await world.work.alerts(world.now) == []


async def test_a_mission_with_more_to_see_goes_on_right_away(journal: SqliteJournal) -> None:
    provider = ScriptedProvider()
    world = World(journal, provider)
    owner = ToolContext("owner", "c0")
    await world.use("mission_start", owner, goal="Des colliers dorés", every_hours=6)
    [mission] = await world.missions.active()
    # A pass with more to look at than it had room for asks for another one.
    provider.extend(call_tools(("mission_now", {})), reply("Trois colliers, la suite arrive."))
    assert await world.work.once() == [mission.id]
    assert "juste après" in str(provider.requests[-1].messages[-1]["content"])
    provider.extend(reply("Deux de plus."))
    assert await world.work.once() == [mission.id]  # the next pass, without waiting 6 h
    assert await world.work.once() == []  # then back to its pace

    # He wants it now: from his own conversation, a pass comes at once.
    world.work._woken.clear()  # pyright: ignore[reportPrivateUsage]
    told = await world.use("mission_now", owner, mission=mission.id)
    assert "tout de suite" in told.content
    assert world.work._woken.is_set()  # pyright: ignore[reportPrivateUsage]
    provider.extend(reply("SILENCE"))
    assert await world.work.once() == [mission.id]

    # Never without end: a few a day.
    for _ in range(6):
        await world.use("mission_now", owner, mission=mission.id)
    refused = await world.use("mission_now", owner, mission=mission.id)
    assert refused.is_error and "Plus de passage immédiat" in refused.content
