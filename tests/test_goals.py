"""Goals: what the owner is after, reached only on evidence, read on Sunday (D267)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.goals.module import Goals, GoalState, goal_tool
from orion.tools.base import Tool, ToolContext, ToolResult

PARIS = ZoneInfo("Europe/Paris")
MONDAY = datetime(2026, 11, 2, 9, 0, tzinfo=PARIS)
OWNER = ToolContext("owner", "c", context_trust=Trust.OWNER)
BACKGROUND = ToolContext("orion", "night", context_trust=Trust.SYSTEM)


async def call(tool: Tool, ctx: ToolContext = OWNER, **args: object) -> ToolResult:
    return await tool.handler(tool.parse(json.dumps(args, default=str)), ctx)


async def test_two_goals_with_a_dependency_reached_only_on_evidence(
    journal: SqliteJournal,
) -> None:
    now = [MONDAY]
    goals = Goals(journal, "Europe/Paris", lambda: now[0])
    tool = goal_tool(goals)
    await call(
        tool,
        action="set",
        title="Trouver un appart à Lyon",
        done_when="un bail signé",
        next_action="monter le dossier",
        due="2026-12-01",
    )
    (flat,) = goals.book.goals
    await call(
        tool,
        action="set",
        title="Déménager",
        done_when="les cartons sont à Lyon",
        next_action="réserver un camion",
        depends_on=[flat],
    )
    move = next(g for g in goals.book.goals if g != flat)
    duplicate = await call(
        tool, action="set", title="trouver un appart à lyon", done_when="x x", next_action="y y"
    )
    assert duplicate.is_error and flat in duplicate.content

    step = (await call(tool, action="milestone", goal=flat, what="dossier complet")).content
    milestone = step.split("(")[1].rstrip(").")
    await call(tool, action="link", goal=flat, kind="commitment", ref="c_42", what="garant")
    again = await call(tool, action="link", goal=flat, kind="commitment", ref="c_42")
    assert "Déjà" in again.content

    # Contradictions: a goal waiting for another, or with an open milestone, is not reached;
    # Orion alone cannot claim his word; two goals cannot wait for each other.
    assert (await call(tool, action="reached", goal=move, basis="owner", evidence="fait")).is_error
    assert (
        await call(tool, action="reached", goal=flat, basis="owner", evidence="il dit oui")
    ).is_error
    alone = await call(
        tool,
        BACKGROUND,
        action="reached",
        goal=flat,
        milestone=milestone,
        basis="owner",
        evidence="il a fini",
    )
    assert alone.is_error
    loop = await call(tool, action="change", goal=flat, depends_on=[move])
    assert loop.is_error

    await call(tool, action="note", goal=flat, kind="obstacle", text="pas de garant")
    await call(tool, action="state", goal=move, state="blocked", why="attend l'appart")
    now[0] = MONDAY + timedelta(days=6)
    week = "\n".join(await goals.lines(now[0]))
    assert "pas de garant" in week and "monter le dossier" in week
    assert "attend" in week and "garant" in week  # the dependency and the linked commitment

    await call(
        tool, action="reached", goal=flat, milestone=milestone, basis="owner", evidence="envoyé"
    )
    done = await call(tool, action="reached", goal=flat, basis="observed", evidence="bail reçu")
    assert not done.is_error

    # After a restart, everything is read back from the journal.
    later = Goals(journal, "Europe/Paris", lambda: now[0])
    await later.sync()
    assert later.book.goals[flat].state is GoalState.ACHIEVED
    assert later.book.goals[move].state is GoalState.BLOCKED
    assert later.book.goals[flat].links == [("commitment", "c_42", "garant")]


async def test_what_orion_suggests_waits_for_the_owner(journal: SqliteJournal) -> None:
    goals = Goals(journal, "Europe/Paris", lambda: MONDAY)
    tool = goal_tool(goals)
    await call(
        tool,
        action="set",
        title="Courir un semi",
        done_when="un semi fini",
        next_action="choisir une course",
        suggested=True,
    )
    (goal,) = goals.book.goals.values()
    assert goal.state is GoalState.PROPOSED
    assert (await call(tool, BACKGROUND, action="state", goal=goal.id, state="active")).is_error
    await call(tool, action="state", goal=goal.id, state="active")
    assert goal.state is GoalState.ACTIVE
