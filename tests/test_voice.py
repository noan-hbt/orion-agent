from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from orion.channels.inbox import Inbound, OwnerInbox
from orion.channels.inkbox import Call, TranscriptEntry, WebhookData, WebhookEvent
from orion.config import AgentConfig
from orion.context.compiler import ContextCompiler
from orion.core.events import Provenance, Trust
from orion.core.journal import SqliteJournal
from orion.guardian.policy import Guardian
from orion.initiative.engine import DELIVERED, InitiativeEngine, InitiativeSettings
from orion.initiative.timers import TimerProjection, Timers
from orion.memory.model import Category, RuleScope
from orion.memory.service import MemoryService, Origin
from orion.memory.store import MemoryStore
from orion.runtime import Orion
from orion.tools.base import ToolRegistry
from orion.voice.brief import build_brief
from orion.voice.channel import CALL_ENDED, CALL_REQUESTED, VoiceChannel, VoiceSettings, render_call
from orion.voice.tools import PhoneLink, voice_tools
from tests.fake_inkbox import OWNER, FakeInkbox
from tests.fakes import ScriptedProvider, call_tools, reply

PARIS = ZoneInfo("Europe/Paris")
NOW = datetime(2026, 10, 19, 18, 0, tzinfo=PARIS)
OWNER_ORIGIN = Origin(actor="orion:core", source="terminal:owner", trust=Trust.OWNER)


class IMessageOutlet:
    name = "imessage"

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def available(self) -> bool:
        return True

    async def deliver(self, text: str, correlation_id: str | None) -> None:
        self.sent.append(text)

    @asynccontextmanager
    async def working(self, item: Inbound) -> AsyncGenerator[None]:
        yield


class World:
    def __init__(self, journal: SqliteJournal, store: MemoryStore) -> None:
        self.journal = journal
        self.memory = MemoryService(journal, store)
        self.fake = FakeInkbox()
        self.provider = ScriptedProvider()
        self.timers = Timers(journal, TimerProjection())
        self.link = PhoneLink()
        self.orion = Orion(
            journal=journal,
            provider=self.provider,
            registry=ToolRegistry(voice_tools(self.link)),
            guardian=Guardian(),
            compiler=ContextCompiler("C"),
            agent=AgentConfig(),
            memory=self.memory,
        )
        self.inbox = OwnerInbox(self.orion, coalesce_seconds=0.05)
        self.imessage = IMessageOutlet()
        self.inbox.register(self.imessage)
        self.voice = VoiceChannel(
            self.fake.client(),
            journal,
            self.inbox,
            VoiceSettings(owner_number=OWNER, handle="orion-test", identity_id="ident-1"),
            store,
            self.timers.projection,
            self.orion.threads,
            clock=lambda: NOW,
        )
        self.link.target = self.voice


@pytest.fixture
async def world(journal: SqliteJournal) -> AsyncIterator[World]:
    store = MemoryStore(":memory:")
    await store.open()
    world = World(journal, store)
    await world.orion.start()
    await world.voice.start()
    worker = asyncio.create_task(world.inbox.run())
    yield world
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    await store.close()


async def until_requests(world: World, count: int) -> None:
    async with asyncio.timeout(2):
        while len(world.provider.requests) < count:  # noqa: ASYNC110 - polling test fakes
            await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)


async def test_brief_is_pushed_only_when_it_changes(world: World) -> None:
    await world.memory.learn_rule("Pas d'emoji.", OWNER_ORIGIN, scope=RuleScope.STYLE)
    await world.memory.add_commitment(
        "Envoyer le devis", OWNER_ORIGIN, debtor="owner", creditor="Paul", due="2026-10-22"
    )
    third_party = Origin("orion:core", "mail:1", Trust.THIRD_PARTY)
    await world.memory.remember(
        "Noan doit 10 000 € à un prince.",
        third_party,
        category=Category.PROFILE,
    )
    assert await world.voice.brief_once() is True
    assert await world.voice.brief_once() is False
    [config] = world.fake.voice_configs
    brief = config["instructions"]
    assert "Pas d'emoji." in brief and "Envoyer le devis" in brief and "Paul" in brief
    assert "prince" not in brief and "(u_" not in brief and "(c_" not in brief
    await world.memory.learn_rule("Réponses courtes.", OWNER_ORIGIN)
    assert await world.voice.brief_once() is True


async def test_a_call_with_the_owner_comes_back_into_the_thread(world: World) -> None:
    world.fake.call(
        turns=[("user", "rappelle-moi demain 9h d'appeler le garage"), ("agent", "C'est noté.")]
    )
    world.provider.extend(reply("C'est calé : demain 9h, le garage."))
    await world.voice.poll_once()
    await until_requests(world, 1)
    content = world.provider.requests[0].messages[-1]["content"]
    assert (
        re.search(r"· appel[] ·]", content)
        and '<appel sens="entrant" durée="1 min 35 s"' in content
    )
    assert "Lui : rappelle-moi demain 9h" in content and "Orion (voix) : C'est noté." in content
    assert world.imessage.sent == ["C'est calé : demain 9h, le garage."]
    [ended] = [e for e in await world.journal.read() if e.kind == CALL_ENDED]
    assert ended.trust is Trust.OWNER

    await world.voice.poll_once()  # already read back: nothing new
    await asyncio.sleep(0.1)
    assert len(world.provider.requests) == 1


async def test_nothing_to_confirm_after_a_call_means_no_message(world: World) -> None:
    world.fake.call(turns=[("user", "rien, je voulais juste parler"), ("agent", "Ok.")])
    world.provider.extend(reply("SILENCE"))
    await world.voice.poll_once()
    await until_requests(world, 1)
    assert world.imessage.sent == []


async def test_webhook_and_polling_read_a_call_once(world: World) -> None:
    raw = world.fake.call(turns=[("user", "salut")])
    world.provider.extend(reply("SILENCE"))
    event = WebhookEvent(
        id="evt_1",
        event_type="call.ended",
        timestamp="t",
        data=WebhookData(call=Call.model_validate(raw), transcript=None),
    )
    await world.voice.handle_webhook(event)
    await world.voice.poll_once()
    await until_requests(world, 1)
    assert len([e for e in await world.journal.read() if e.kind == CALL_ENDED]) == 1


async def test_calling_someone_is_reported_as_third_party_content(world: World) -> None:
    world.provider.extend(
        call_tools(
            (
                "call_someone",
                {
                    "number": "+33102030405",
                    "name": "La Pizzeria",
                    "goal": "Réserver pour 2 jeudi 20h au nom de Noan",
                },
            )
        ),
        reply("Je les appelle."),
    )
    await world.orion.handle_owner_message(
        "owner", "réserve la pizzeria jeudi", "terminal", now=NOW
    )
    # The guardian's default approver refuses external effects: nothing was dialled.
    assert world.fake.phone_calls == {}

    call_id = await world.voice.call_someone(
        "+33102030405", "La Pizzeria", "Réserver pour 2 jeudi 20h"
    )
    assert (
        "/identities/orion-test/phone-contact-rules",
        {
            "action": "allow",
            "match_type": "exact_number",
            "match_target": "+33102030405",
            "direction": "outbound",
        },
    ) in world.fake.rules
    requested = [e for e in await world.journal.read() if e.kind == CALL_REQUESTED]
    assert requested[0].payload["purpose"] == "third_party"

    call = world.fake.phone_calls[call_id]
    call.update(status="completed", duration_seconds=40)
    world.fake.transcripts[call_id] = [{"party": "user", "text": "C'est noté pour jeudi 20h."}]
    before = len(world.provider.requests)
    await world.voice.poll_once()
    [report] = await world.voice.reports()
    assert report.trust is Trust.THIRD_PARTY and "jeudi 20h" in report.notice
    await asyncio.sleep(0.1)
    assert len(world.provider.requests) == before  # never an owner turn: reported, not obeyed


async def test_reminders_by_call_ring_the_owner(world: World) -> None:
    await world.timers.schedule(
        datetime(2026, 10, 20, 7, 0, tzinfo=PARIS),
        "Debout",
        "owner",
        Provenance(source="t", actor="owner:main"),
        Trust.OWNER,
        by_call=True,
    )
    engine = InitiativeEngine(
        world.orion,
        world.journal,
        world.timers,
        world.inbox,
        InitiativeSettings(digests=()),
        clock=lambda: datetime(2026, 10, 20, 7, 0, tzinfo=PARIS),
        caller=world.voice,
    )
    assert len(await engine.tick()) == 1
    [call] = world.fake.phone_calls.values()
    assert call["remote_phone_number"] == OWNER and "Debout" in call["reason"]
    [delivered] = [e for e in await world.journal.read() if e.kind == DELIVERED]
    assert delivered.payload["how"] == "call"
    assert world.imessage.sent == []


async def test_a_call_to_the_owner_that_failed_is_reported(world: World) -> None:
    call_id = await world.voice.call_owner("Rappel : Debout")
    world.fake.phone_calls[call_id].update(status="failed", hangup_reason="rejected")
    await world.voice.poll_once()
    [report] = await world.voice.reports()
    assert report.key == f"call:{call_id}" and report.trust is Trust.SYSTEM
    assert "failed, rejected" in report.notice and "Debout" in report.notice
    assert "Debout" in report.fallback

    same = await world.voice.call_owner("Encore")
    world.fake.phone_calls[same].update(status="failed", hangup_reason="failed")
    await world.voice.poll_once()
    latest = (await world.voice.reports())[-1]
    assert "(Inkbox : failed)" in latest.fallback  # not "failed, failed"

    answered = await world.voice.call_owner("Point du soir")
    world.fake.phone_calls[answered].update(status="completed", duration_seconds=30)
    world.provider.extend(reply("SILENCE"))
    await world.voice.poll_once()
    await until_requests(world, 1)
    assert [r.key for r in await world.voice.reports()] == [f"call:{call_id}", f"call:{same}"]


def test_render_and_brief_helpers() -> None:
    call = Call(
        id="c",
        remote_phone_number=OWNER,
        direction="outbound",
        status="completed",
        created_at="t",
        duration_seconds=None,
    )
    text = render_call(call, [TranscriptEntry(marker="abridged", omitted_turns=4)], [])
    assert "sortant" in text and "durée inconnue" in text and "4 échanges coupés" in text
    brief = build_brief(
        NOW,
        "Europe/Paris",
        [],
        [],
        [],
        [],
        [
            {"role": "user", "content": "[lundi · imessage]\n<memoire>\nsecret\n</memoire>\nyo"},
            {"role": "assistant", "content": "Salut."},
        ],
    )
    assert "Lui : yo" in brief and "secret" not in brief and "Orion : Salut." in brief


async def test_a_live_call_is_not_read_back_and_orion_opens_it(world: World) -> None:
    from dataclasses import replace

    from orion.core.events import NewEvent, Provenance
    from orion.voice.live import LIVE_STARTED

    world.voice._settings = replace(  # pyright: ignore[reportPrivateUsage]
        world.voice._settings,  # pyright: ignore[reportPrivateUsage]
        live_url="wss://orion.test/voice/live",
    )
    call_id = await world.voice.call_owner("Ton rendez-vous chez le dentiste est à 15 h.")
    placed = world.fake.phone_calls[call_id]
    assert placed["mode"] == "client_websocket" and placed["reason"] is None
    assert await world.voice.opening(call_id) == (
        "Allô, c'est Orion. Ton rendez-vous chez le dentiste est à 15 h."
    )
    await world.journal.append(
        [
            NewEvent(
                kind=LIVE_STARTED,
                payload={"call_id": call_id},
                provenance=Provenance(source=f"call:{call_id}", actor="orion:voice"),
                trust=Trust.SYSTEM,
            )
        ]
    )
    world.fake.phone_calls[call_id] |= {"status": "completed", "duration_seconds": 60}
    await world.voice.poll_once()
    await asyncio.sleep(0.1)
    assert world.provider.requests == []  # its words are already in the thread
    assert CALL_ENDED in [e.kind for e in await world.journal.read()]  # still on record
