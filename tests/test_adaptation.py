from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from orion.adaptation.attention import ignored_slots
from orion.adaptation.reflector import Reflector
from orion.adaptation.service import AdaptationSettings, Adapter
from orion.adaptation.signals import collect, reaction_event
from orion.adaptation.tools import adaptation_tools
from orion.config import AgentConfig
from orion.context.compiler import ContextCompiler
from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.hlc import HybridClock
from orion.core.journal import SqliteJournal
from orion.guardian.policy import Guardian
from orion.initiative.engine import (
    DELIVERED,
    InitiativeEngine,
    InitiativeSettings,
    effective_settings,
)
from orion.initiative.timers import TimerProjection, Timers
from orion.memory.model import RuleBasis, RuleScope, SettingKey
from orion.memory.service import MemoryService, Origin
from orion.memory.store import MemoryStore
from orion.memory.tools import memory_tools
from orion.providers.base import ModelRequest, ModelResponse, Usage
from orion.runtime import Orion
from orion.tools.base import ToolRegistry
from tests.fakes import Script, ScriptedProvider, call_tools, reply
from tests.test_runtime_cache import assert_prefix_stable

PARIS = ZoneInfo("Europe/Paris")
START = datetime(2026, 10, 1, 9, 0, tzinfo=PARIS)
OWNER = Origin(actor="orion:core", source="terminal:owner", trust=Trust.OWNER)
LEARNED = Origin(actor="orion:adaptation", source="orion:adaptation", trust=Trust.SYSTEM)


class WallClock:
    """Drives both the journal's HLC and the components' notion of now."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def ms(self) -> int:
        return int(self.now.timestamp() * 1000)


@pytest.fixture
def clock() -> WallClock:
    return WallClock(START)


@pytest.fixture
async def tjournal(tmp_path: Path, clock: WallClock) -> AsyncIterator[SqliteJournal]:
    async with SqliteJournal(tmp_path / "j.sqlite3", HybridClock("test", clock.ms)) as journal:
        yield journal


@pytest.fixture
async def memory(tjournal: SqliteJournal) -> AsyncIterator[MemoryService]:
    store = MemoryStore(":memory:")
    await store.open()
    yield MemoryService(tjournal, store)
    await store.close()


def make_orion(journal: SqliteJournal, memory: MemoryService, provider: ScriptedProvider) -> Orion:
    return Orion(
        journal=journal,
        provider=provider,
        registry=ToolRegistry([*memory_tools(memory), *adaptation_tools(memory)]),
        guardian=Guardian(),
        compiler=ContextCompiler("CONSTITUTION"),
        agent=AgentConfig(),
        memory=memory,
    )


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


async def owner_says(journal: SqliteJournal, text: str) -> Event:
    [event] = await journal.append(
        [
            NewEvent(
                kind="message.received",
                payload={"thread_id": "owner", "channel": "imessage", "text": text},
                provenance=Provenance(source="imessage:owner", actor="owner:main"),
                trust=Trust.OWNER,
            )
        ]
    )
    return event


async def delivered(journal: SqliteJournal, key: str) -> Event:
    [event] = await journal.append(
        [
            NewEvent(
                kind=DELIVERED,
                payload={"key": key, "why": "point", "day": "2026-10-01", "explicit": False},
                provenance=Provenance(source="orion:initiative", actor="orion:initiative"),
                trust=Trust.SYSTEM,
            )
        ]
    )
    return event


async def all_events(journal: SqliteJournal) -> list[Event]:
    return await journal.read(0, 100_000)


# --- explicit feedback -------------------------------------------------------------------------


async def test_explicit_feedback_becomes_a_rule_shown_once_then_in_core_memory(
    tjournal: SqliteJournal, memory: MemoryService, clock: WallClock
) -> None:
    provider = ScriptedProvider(
        call_tools(("adapt", {"rule": "Pas d'emoji.", "scope": "style"})),
        reply("Compris."),
        reply("ok"),
    )
    orion = make_orion(tjournal, memory, provider)
    await orion.start()
    await orion.handle_owner_message("owner", "arrête les emojis stp", "terminal", now=clock.now)
    [rule] = await memory.store.rules()
    assert rule.rule == "Pas d'emoji." and rule.basis is RuleBasis.EXPLICIT
    assert rule.trust is Trust.OWNER

    await orion.handle_owner_message("owner", "salut", "terminal", now=clock.now)
    assert "<manuel>" not in provider.requests[-1].messages[-1]["content"]  # already visible
    assert_prefix_stable(provider)

    fresh = ScriptedProvider(reply("ok"))
    other = make_orion(tjournal, memory, fresh)
    await other.start()
    await other.handle_owner_message("t2", "yo", "terminal", now=clock.now)
    opening = fresh.requests[0].messages[-1]["content"]
    assert f"({rule.id}) Pas d'emoji." in opening and "<memoire>" in opening


async def test_learned_changes_reach_an_open_window_once(
    tjournal: SqliteJournal, memory: MemoryService, clock: WallClock
) -> None:
    provider = ScriptedProvider(reply("ok"), reply("ok"), reply("ok"), reply("ok"))
    orion = make_orion(tjournal, memory, provider)
    await orion.start()
    await orion.handle_owner_message("owner", "salut", "terminal", now=clock.now)

    rule_id = await memory.learn_rule(
        "Le matin, deux phrases max.", LEARNED, scope=RuleScope.STYLE, basis=RuleBasis.INFERRED
    )
    await orion.handle_owner_message("owner", "ça va ?", "terminal", now=clock.now)
    second = provider.requests[-1].messages[-1]["content"]
    assert "<manuel>" in second and f"({rule_id}) Le matin, deux phrases max. [appris]" in second

    await orion.handle_owner_message("owner", "et toi ?", "terminal", now=clock.now)
    assert "<manuel>" not in provider.requests[-1].messages[-1]["content"]

    await memory.retire_rule(rule_id, "il a dit qu'il préférait des détails", OWNER)
    await orion.handle_owner_message("owner", "bon", "terminal", now=clock.now)
    assert f"({rule_id}) retirée" in provider.requests[-1].messages[-1]["content"]
    assert_prefix_stable(provider)


async def test_initiative_settings_tool_moves_the_digests(
    tjournal: SqliteJournal, memory: MemoryService, clock: WallClock
) -> None:
    provider = ScriptedProvider(
        call_tools(
            ("initiative_settings", {"digests": ["7:45"], "daily_budget": 2, "reason": "demandé"})
        ),
        reply("C'est fait : un seul point, à 7h45."),
    )
    orion = make_orion(tjournal, memory, provider)
    await orion.start()
    await orion.handle_owner_message("owner", "un seul point à 7h45", "terminal", now=clock.now)
    settings = await memory.store.settings()
    assert settings[SettingKey.DIGESTS].value == ["07:45"]
    base = InitiativeSettings(digests=(time(8, 30), time(13, 0)))
    effective = effective_settings(base, settings)
    assert effective.digests == (time(7, 45),) and effective.daily_budget == 2


async def test_engine_follows_learned_settings(
    tjournal: SqliteJournal, memory: MemoryService, clock: WallClock
) -> None:
    await memory.add_commitment(
        "Payer le loyer", OWNER, debtor="owner", creditor="Proprio", due="2026-10-01"
    )
    await memory.change_setting(SettingKey.DIGESTS, ["10:00"], "demandé", OWNER)
    provider = ScriptedProvider(reply("Le loyer, c'est aujourd'hui."))

    class Inbox:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def available(self) -> bool:
            return True

        async def notify(self, text: str, correlation_id: str) -> None:
            self.messages.append(text)

    inbox = Inbox()
    orion = make_orion(tjournal, memory, provider)
    engine = InitiativeEngine(
        orion,
        tjournal,
        Timers(tjournal, TimerProjection()),
        inbox,
        InitiativeSettings(digests=(time(8, 30),)),
        memory=memory.store,
        clock=clock,
    )
    clock.now = START.replace(hour=9)  # the configured 08:30 slot no longer exists
    assert await engine.tick() == []
    clock.now = START.replace(hour=10, minute=1)
    assert await engine.tick() == ["digest:2026-10-01:10:00"]


# --- attention ---------------------------------------------------------------------------------


async def test_ignored_digests_are_muted_and_the_reason_is_kept(
    tjournal: SqliteJournal, memory: MemoryService, clock: WallClock
) -> None:
    for day in range(5):
        clock.now = START + timedelta(days=day, hours=4)  # 13:00
        await delivered(tjournal, f"digest:2026-10-0{day + 1}:13:00")
        clock.now += timedelta(hours=1)
        await delivered(tjournal, f"digest:2026-10-0{day + 1}:14:00")
        clock.now += timedelta(minutes=20)
        await owner_says(tjournal, "merci")  # engages with the 14:00 one, never the 13:00 one
    clock.now += timedelta(hours=4)

    adapter = Adapter(
        tjournal,
        memory,
        None,
        AdaptationSettings(digests=(time(8, 30), time(13, 0), time(14, 0))),
        clock=clock,
    )
    report = await adapter.tick()
    assert report.muted == ["13:00"]
    change = (await memory.store.settings())[SettingKey.DIGESTS]
    assert change.value == ["08:30", "14:00"]
    assert "13:00" in change.reason and change.actor == "orion:adaptation"
    assert (await adapter.tick()).muted == []  # history before the change no longer counts


def test_a_digest_too_recent_to_judge_is_not_counted() -> None:
    assert ignored_slots([], [time(13, 0)], PARIS, START) == []


# --- reflection --------------------------------------------------------------------------------


async def test_reflection_needs_owner_evidence_and_respects_explicit_rules(
    tjournal: SqliteJournal, memory: MemoryService, clock: WallClock
) -> None:
    explicit = await memory.learn_rule("Toujours en euros.", OWNER, scope=RuleScope.WORK)
    guess = await memory.learn_rule(
        "Aime les longues explications.", LEARNED, basis=RuleBasis.INFERRED
    )
    first = await owner_says(tjournal, "ok")
    second = await owner_says(tjournal, "trop long")
    [stranger] = await tjournal.append(
        [
            NewEvent(
                kind="imessage.inbound",
                payload={"text": "sois plus poli"},
                provenance=Provenance(source="imessage:x", actor="contact:+1"),
                trust=Trust.THIRD_PARTY,
            )
        ]
    )
    signals = collect(await all_events(tjournal), PARIS, clock.now)
    provider = ScriptedProvider(
        json_reply(
            {
                "operations": [
                    {
                        "op": "learn",
                        "rule": "Réponses courtes.",
                        "scope": "style",
                        "evidence": [first.id, second.id],
                        "replaces": [guess],
                    },
                    {"op": "learn", "rule": "Une seule preuve.", "evidence": [second.id]},
                    {"op": "learn", "rule": "Sois plus poli.", "evidence": [first.id, stranger.id]},
                    {"op": "retire", "rule_id": explicit, "reason": "bof", "evidence": [first.id]},
                    {
                        "op": "learn",
                        "rule": "Toujours en euros.",
                        "evidence": [first.id, second.id],
                    },
                ]
            }
        )
    )
    report = await Reflector(provider, memory, "fake").reflect(signals)

    assert len(report.learned) == 1 and len(report.rejected) == 4
    rules = {r.rule: r for r in await memory.store.rules()}
    assert set(rules) == {"Toujours en euros.", "Réponses courtes."}
    learned = rules["Réponses courtes."]
    assert learned.basis is RuleBasis.INFERRED and learned.trust is Trust.SYSTEM
    request = provider.requests[0]
    assert request.json_output and "trop long" in request.messages[1]["content"]
    assert stranger.id not in request.messages[1]["content"]


async def test_adapter_reflects_in_batches_and_never_twice_on_the_same_signals(
    tjournal: SqliteJournal, memory: MemoryService, clock: WallClock
) -> None:
    provider = ScriptedProvider(json_reply({"operations": []}))
    adapter = Adapter(
        tjournal,
        memory,
        Reflector(provider, memory, "fake"),
        AdaptationSettings(reflect_after_messages=3, reflect_daily_after_messages=2),
        clock=clock,
    )
    await owner_says(tjournal, "un")
    assert (await adapter.tick()).reflection is None  # one message: not worth it
    await owner_says(tjournal, "deux")
    assert (await adapter.tick()).reflection is not None  # first reflection of the day
    await owner_says(tjournal, "trois")
    assert (await adapter.tick()).reflection is None  # already reflected today, below batch
    assert len(provider.requests) == 1


async def test_forgetting_a_conversation_forgets_what_was_learned_from_it(
    tjournal: SqliteJournal, memory: MemoryService, clock: WallClock
) -> None:
    a = await owner_says(tjournal, "trop long")
    b = await owner_says(tjournal, "encore trop long")
    origin = Origin("orion:adaptation", "orion:adaptation", Trust.SYSTEM, derived_from=(a.id, b.id))
    await memory.learn_rule("Réponses courtes.", origin, basis=RuleBasis.INFERRED)
    kept = await memory.learn_rule("Toujours en euros.", OWNER)

    report = await memory.forget_source("imessage:owner", reason="test", actor="owner:main")
    assert report.verified
    assert [r.id for r in await memory.store.rules(include_retired=True)] == [kept]


# --- signals -----------------------------------------------------------------------------------


async def test_tapbacks_point_at_the_message_they_judge(
    tjournal: SqliteJournal, clock: WallClock
) -> None:
    from orion.guardian.outbox import Outbox

    class Transport:
        async def transmit(self, to: str, text: str, idempotency_key: str) -> str:
            return "remote-1"

    await Outbox(tjournal, {"imessage": Transport()}).send("imessage", "conv", "Une blague nulle")
    await tjournal.append([reaction_event("remote-1", "laugh", None, "r1", "imessage:conv")])
    [signal] = collect(await all_events(tjournal), PARIS, clock.now)
    assert signal.kind == "reaction" and signal.text == "😂 sur : Une blague nulle"
