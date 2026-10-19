from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from orion.cognition.verifier import Verifier
from orion.config import AgentConfig
from orion.context.compiler import ContextCompiler
from orion.core.events import Provenance, Trust
from orion.core.journal import SqliteJournal
from orion.guardian.policy import Guardian
from orion.initiative.engine import DELIVERED, InitiativeEngine, InitiativeSettings
from orion.initiative.timers import TimerProjection, Timers
from orion.providers.base import ModelRequest, ModelResponse, ProviderError, Usage
from orion.providers.router import Router, parse_spec
from orion.runtime import Orion
from orion.tools.base import ToolRegistry
from tests.fakes import Script, ScriptedProvider, fail, reply

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
    assert inbox.messages == [expected]
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
