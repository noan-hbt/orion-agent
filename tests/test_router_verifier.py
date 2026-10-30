from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import BaseModel

from orion.cognition.verifier import Verifier
from orion.config import AgentConfig
from orion.context.compiler import ContextCompiler
from orion.core.events import Provenance, Trust
from orion.core.journal import SqliteJournal
from orion.guardian.policy import Guardian
from orion.initiative.engine import (
    DELIVERED,
    InitiativeEngine,
    InitiativeSettings,
    _read_while_writing,  # pyright: ignore[reportPrivateUsage]
    readable,
)
from orion.initiative.timers import TimerProjection, Timers
from orion.providers.base import ModelRequest, ModelResponse, ProviderError, Usage
from orion.providers.router import Router, parse_spec
from orion.runtime import Orion
from orion.tools.base import Effect, Tool, ToolContext, ToolRegistry, ToolResult
from tests.fakes import Script, ScriptedProvider, call_tools, fail, reply

PARIS = ZoneInfo("Europe/Paris")


def json_reply(payload: object) -> Script:
    def make(_request: ModelRequest) -> ModelResponse:
        text = json.dumps(payload)
        return ModelResponse(
            message={"role": "assistant", "content": text},
            text=text,
            tool_calls=[],
            finish_reason="stop",
            usage=Usage(),
            model="fake",
        )

    return make


def request(model: str) -> ModelRequest:
    return ModelRequest(model=model, messages=[{"role": "user", "content": "hi"}])


def test_parse_spec() -> None:
    assert parse_spec("openrouter:deepseek/x", "d") == ("openrouter", "deepseek/x")
    assert parse_spec("deepseek/x", "d") == ("d", "deepseek/x")
    assert parse_spec("minimax/m3:free", "d") == ("d", "minimax/m3:free")


async def test_router_falls_back_in_order_and_names_the_model() -> None:
    primary = ScriptedProvider(fail("down", retryable=True))
    backup = ScriptedProvider(reply("from backup"))
    router = Router({"a": primary, "b": backup}, "a", {"main": ["b:other"]})
    response = await router.complete(request("main"))
    assert response.text == "from backup"
    assert [r.model for r in primary.requests] == ["main"]
    assert [r.model for r in backup.requests] == ["other"]


async def test_router_raises_the_last_error_and_rejects_unknown_providers() -> None:
    router = Router(
        {"a": ScriptedProvider(fail("one")), "b": ScriptedProvider(fail("two"))},
        "a",
        {"m": ["b:x"]},
    )
    with pytest.raises(ProviderError, match="two"):
        await router.complete(request("m"))
    with pytest.raises(ValueError, match="unknown provider"):
        Router({"a": ScriptedProvider()}, "a", {"m": ["nope:x"]})


def test_verifier_must_be_another_model() -> None:
    with pytest.raises(ValueError, match="verifier_model"):
        AgentConfig(model="x/y", verifier_model="x/y")


async def test_verifier_fails_closed() -> None:
    assert (await Verifier(ScriptedProvider(fail()), "v").check("d", "f")).ok is False
    garbage = ScriptedProvider(reply("not json"))
    assert (await Verifier(garbage, "v").check("d", "f")).ok is False
    good = ScriptedProvider(json_reply({"ok": True, "issues": []}))
    assert (await Verifier(good, "v").check("d", "f")).ok is True
    sent = good.requests[0]
    assert sent.json_output and sent.model == "v" and "DRAFT:\nd" in sent.messages[1]["content"]


class Inbox:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def available(self) -> bool:
        return True

    async def notify(self, text: str, correlation_id: str) -> None:
        self.messages.append(text)


class Empty(BaseModel):
    pass


@pytest.fixture
async def setup(journal: SqliteJournal) -> AsyncIterator[tuple[ScriptedProvider, Timers, Orion]]:
    provider = ScriptedProvider()
    timers = Timers(journal, TimerProjection())
    orion = Orion(
        journal=journal,
        provider=provider,
        registry=ToolRegistry([]),
        guardian=Guardian(),
        compiler=ContextCompiler("C"),
        agent=AgentConfig(),
    )
    await timers.schedule(
        datetime(2026, 10, 20, 9, 0, tzinfo=PARIS),
        "Appeler Paul",
        "owner",
        Provenance(source="t", actor="owner:main"),
        Trust.OWNER,
    )
    yield provider, timers, orion


@pytest.mark.parametrize(
    ("verdict", "expected", "how"),
    [
        ({"ok": True, "issues": []}, "Hé, Paul. C'est l'heure.", "verified"),
        ({"ok": False, "issues": ["says Wednesday"]}, "Rappel : Appeler Paul", "fallback: says"),
    ],
)
async def test_proactive_drafts_are_checked_by_another_model(
    journal: SqliteJournal,
    setup: tuple[ScriptedProvider, Timers, Orion],
    verdict: dict[str, object],
    expected: str,
    how: str,
) -> None:
    provider, timers, orion = setup
    provider.extend(reply("Hé, Paul. C'est l'heure."))
    checker = ScriptedProvider(json_reply(verdict))
    inbox = Inbox()
    engine = InitiativeEngine(
        orion,
        journal,
        timers,
        inbox,
        InitiativeSettings(digests=()),
        clock=lambda: datetime(2026, 10, 20, 9, 1, tzinfo=PARIS),
        verifier=Verifier(checker, "verifier"),
    )
    await engine.tick()
    [sent] = inbox.messages
    assert sent.endswith(expected)
    facts = checker.requests[0].messages[1]["content"]
    assert "mardi 2026-10-20 à 09:01" in facts and "Appeler Paul" in facts
    [record] = [e for e in await journal.read() if e.kind == DELIVERED]
    assert str(record.payload["how"]).startswith(how)


async def test_the_verifier_also_reads_the_recent_conversation(
    journal: SqliteJournal, setup: tuple[ScriptedProvider, Timers, Orion]
) -> None:
    provider, timers, orion = setup
    provider.extend(reply("Trois appels ratés ce soir."), reply("Hé, Paul. Toujours pas joint."))
    await orion.handle_owner_message(
        "owner", "appelle-moi", "imessage", now=datetime(2026, 10, 20, 8, 0, tzinfo=PARIS)
    )
    checker = ScriptedProvider(json_reply({"ok": True, "issues": []}))
    engine = InitiativeEngine(
        orion,
        journal,
        timers,
        Inbox(),
        InitiativeSettings(digests=()),
        clock=lambda: datetime(2026, 10, 20, 9, 1, tzinfo=PARIS),
        verifier=Verifier(checker, "verifier"),
    )
    await engine.tick()
    prompt = checker.requests[0].messages[1]["content"]
    background = prompt.split("RECENT CONVERSATION:\n")[1].split("\n\nDRAFT:")[0]
    assert background == "Lui : appelle-moi\nOrion : Trois appels ratés ce soir."


async def test_the_verifier_sees_what_orion_looked_up_while_writing(
    journal: SqliteJournal, setup: tuple[ScriptedProvider, Timers, Orion]
) -> None:
    """20/10, 19:00: a trip prep built on real searches (weather, route) was rejected as
    invented, because the verifier only saw the notice."""
    provider, timers, _ = setup

    async def weather(_args: Any, _ctx: ToolContext) -> ToolResult:
        return ToolResult("Nonancourt mercredi : averses, 13 °C, coucher 18h54")

    look = Tool(
        name="weather",
        description="w",
        input_model=Empty,
        handler=weather,
        effect=Effect.NONE,
        concurrency_safe=True,
    )
    orion = Orion(
        journal=journal,
        provider=provider,
        registry=ToolRegistry([look]),
        guardian=Guardian(),
        compiler=ContextCompiler("C"),
        agent=AgentConfig(),
    )
    provider.extend(call_tools(("weather", {})), reply("Averses demain, prends une veste."))
    checker = ScriptedProvider(json_reply({"ok": True, "issues": []}))
    engine = InitiativeEngine(
        orion,
        journal,
        timers,
        Inbox(),
        InitiativeSettings(digests=()),
        clock=lambda: datetime(2026, 10, 20, 9, 1, tzinfo=PARIS),
        verifier=Verifier(checker, "verifier"),
    )
    await engine.tick()
    facts = checker.requests[0].messages[1]["content"].split("RECENT CONVERSATION:")[0]
    assert "What Orion read while writing" in facts and "averses, 13 °C" in facts


def test_a_page_read_in_a_long_turn_still_reaches_the_verifier() -> None:
    """30/10, 13:00: a long turn (memory, tax pages, then the timetable) was cut to its ends;
    the rewrite repeated the context, and « salle NAMEK » was rejected twice as invented."""
    context = "mémoire " * 2000
    turn: list[Any] = [
        {"role": "user", "content": context},
        {"role": "tool", "content": "impots.gouv " * 3000},
        {"role": "tool", "content": "Emploi du temps : anglais 13h30, salle NAMEK, 1er étage"},
        {"role": "tool", "content": "page " * 3000},
        {"role": "user", "content": context},
    ]
    assert "salle NAMEK" in _read_while_writing(turn)


def test_a_point_reads_like_a_message() -> None:
    """The plain version, when Orion's own wording was not kept: no ids, no arrows, nothing
    Orion owes itself, each thing once."""
    lines = [
        "- (c_01A) moi (Orion) → toi : Préparer le trajet, pour aujourd'hui",
        "- (c_01B) toi → toi : Appeler le garagiste, pour demain à 09:00",
        "- (c_01C) toi → toi : Appeler le garagiste, pour demain à 09:00",
        "- (c_01D) toi → Paul : Rendre la perceuse, pour aujourd'hui",
    ]
    assert readable(lines) == (
        "- Appeler le garagiste, pour demain à 09:00\n- pour Paul : Rendre la perceuse, pour "
        "aujourd'hui"
    )


async def test_a_rejected_draft_is_rewritten_by_orion_not_replaced_by_a_canned_text(
    journal: SqliteJournal, setup: tuple[ScriptedProvider, Timers, Orion]
) -> None:
    """21/10: a draft was rejected and the notice meant for Orion went out as the message."""
    provider, timers, orion = setup
    provider.extend(reply("Mercredi, appelle Paul."), reply("Hé, c'est l'heure d'appeler Paul."))
    checker = ScriptedProvider(
        json_reply({"ok": False, "issues": ["says Wednesday, it is Tuesday"]}),
        json_reply({"ok": True, "issues": []}),
    )
    inbox = Inbox()
    engine = InitiativeEngine(
        orion,
        journal,
        timers,
        inbox,
        InitiativeSettings(digests=()),
        clock=lambda: datetime(2026, 10, 20, 9, 1, tzinfo=PARIS),
        verifier=Verifier(checker, "verifier"),
    )
    await engine.tick()
    assert inbox.messages == ["Hé, c'est l'heure d'appeler Paul."]
    asked = str(provider.requests[1].messages[-1]["content"])
    assert "n'est pas parti" in asked and "says Wednesday" in asked
    [record] = [e for e in await journal.read() if e.kind == DELIVERED]
    assert record.payload["how"] == "verified on a second draft"


async def test_what_people_write_to_him_is_passed_on_outside_the_budget(
    journal: SqliteJournal, setup: tuple[ScriptedProvider, Timers, Orion]
) -> None:
    """21/10: Victor's messages, relayed at 1 a.m., spent the day: no digest at 8:30 (D104)."""
    from orion.initiative.engine import Alert

    provider, timers, orion = setup
    provider.extend(reply("Hé, Paul. C'est l'heure."), reply("Angèle t'a écrit : elle arrive."))

    class Contacts:
        async def alerts(self, now: datetime) -> list[Alert]:
            said = "Angèle t'a écrit : « j'arrive »"
            return [Alert("secretary:1", said, "Angèle t'a écrit", said, spoken_to=True)]

    inbox = Inbox()
    engine = InitiativeEngine(
        orion,
        journal,
        timers,
        inbox,
        InitiativeSettings(digests=(), daily_budget=0),
        clock=lambda: datetime(2026, 10, 20, 9, 1, tzinfo=PARIS),
        alerts=[Contacts()],
    )
    assert "secretary:1" in await engine.tick()
    assert engine.ledger.spent.get(datetime(2026, 10, 20).date(), 0) == 0
