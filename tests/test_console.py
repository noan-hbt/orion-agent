"""The control page: in only with a one-time link; every correction is journaled."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from orion.console.app import Console, console_app
from orion.console.auth import COOKIE, Gate, session_key
from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.guardian.autonomy import Autonomy
from orion.initiative.timers import TimerProjection, Timers
from orion.memory.model import CommitmentStatus
from orion.memory.provenance import Provenances
from orion.memory.service import MemoryService, Origin
from orion.memory.store import MemoryStore

OWNER = Origin(actor="owner:main", source="imessage:owner", trust=Trust.OWNER)
BASE = "https://orion.test"


World = tuple[httpx.AsyncClient, Gate, MemoryService, Autonomy]


@pytest.fixture
async def world(journal: SqliteJournal, tmp_path: Path) -> AsyncIterator[World]:
    store = MemoryStore(":memory:")
    await store.open()
    memory = MemoryService(journal, store)
    timers = Timers(journal, TimerProjection())
    autonomy = Autonomy(journal, ["calendar_add"])
    await autonomy.start()
    gate = Gate(journal, session_key(tmp_path))
    app = console_app(
        Console(
            gate, journal, memory, autonomy, Provenances(journal, memory, timers), "Europe/Paris"
        )
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as http:
        yield http, gate, memory, autonomy
    await store.close()


async def test_nothing_without_a_link_and_a_link_works_once(world: World) -> None:
    http, gate, memory, _ = world
    await memory.remember("Aime le jazz", OWNER)
    page = await http.get("/console/memory")
    assert "jazz" not in page.text and "lien" in page.text
    assert "expiré" in (await http.get("/console/login?t=forged")).text
    token = await gate.new_link()
    login = await http.get(f"/console/login?t={token}")
    assert login.status_code == 303
    cookie = login.cookies[COOKIE]
    assert "httponly" in login.headers["set-cookie"].lower()
    assert "samesite=strict" in login.headers["set-cookie"].lower()
    assert "expiré" in (await http.get(f"/console/login?t={token}")).text  # once
    http.cookies.set(COOKIE, cookie, domain="orion.test", path="/console")
    assert "jazz" in (await http.get("/console/memory")).text
    assert not gate.valid(cookie[:-1] + ("0" if cookie[-1] != "0" else "1"))  # tampered


async def logged_in(http: httpx.AsyncClient, gate: Gate) -> None:
    login = await http.get(f"/console/login?t={await gate.new_link()}")
    http.cookies.set(COOKIE, login.cookies[COOKIE], domain="orion.test", path="/console")


async def test_corrections_are_journaled_as_the_owners(world: World) -> None:
    http, gate, memory, autonomy = world
    await logged_in(http, gate)
    fact = await memory.remember("Habite à Lyon", OWNER)
    owed = await memory.add_commitment("Payer le loyer", OWNER, debtor="owner", creditor="Agence")
    rule = await memory.learn_rule("Messages courts", OWNER)
    await autonomy.grant("calendar_add", "g", None)

    await http.post(f"/console/facts/{fact}/forget")
    assert await memory.store.facts() == []
    await http.post(f"/console/commitments/{owed}/done")
    [done] = await memory.store.commitments(CommitmentStatus.DONE)
    assert done.evidence == "depuis la console"
    await http.post(f"/console/rules/{rule}/retire")
    assert await memory.store.rules() == []
    await http.post("/console/autonomy/calendar_add/revoke")
    assert not await autonomy.allows("calendar_add")
    activity = await http.get("/console/activity")
    assert "autonomie retirée" in activity.text

    refused = await http.post(
        f"/console/facts/{fact}/forget", headers={"origin": "https://evil.example"}
    )
    assert refused.status_code == 403


async def test_a_source_can_be_traced_from_the_page(world: World) -> None:
    http, gate, memory, _ = world
    await logged_in(http, gate)
    await memory.remember(
        "Doit 500 €",
        Origin(actor="orion:quarantine", source="mail:perso:<evil@x>", trust=Trust.THIRD_PARTY),
    )
    page = await http.get("/console/sources", params={"source": "mail:perso:<evil@x>"})
    assert "1 souvenir(s)" in page.text and "Mettre en quarantaine" in page.text
    await http.post("/console/contain", data={"source": "mail:perso:<evil@x>"})
    assert await memory.store.facts() == []


async def test_what_orion_will_do_and_what_it_cost(journal: SqliteJournal, tmp_path: Path) -> None:
    from datetime import datetime, timedelta

    from orion.config import BudgetConfig
    from orion.modules.base import Planned
    from orion.providers.spending import with_spending
    from tests.fakes import ScriptedProvider

    store = MemoryStore(":memory:")
    await store.open()
    memory = MemoryService(journal, store)
    gate = Gate(journal, session_key(tmp_path))

    async def plans(now: datetime, until: datetime) -> list[Planned]:
        return [
            Planned(
                now + timedelta(days=1),
                "te rappeler : appeler le garage",
                "reminders (cancel, t_1)",
            )
        ]

    spending = with_spending(ScriptedProvider(), journal, BudgetConfig(), "Europe/Paris")
    app = console_app(
        Console(
            gate,
            journal,
            memory,
            None,
            Provenances(journal, memory, Timers(journal, TimerProjection())),
            "Europe/Paris",
            planned=plans,
            spending=spending,
        )
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as http:
        await logged_in(http, gate)
        page = (await http.get("/console/planned")).text
        assert "appeler le garage" in page and "reminders (cancel, t_1)" in page
        home = (await http.get("/console/")).text
        assert "ai coûté" in home and "Ce mois-ci : 0.00 $" in home
    await store.close()
