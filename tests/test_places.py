"""Places: a reminder goes off when the owner's phone says they arrived somewhere."""

from __future__ import annotations

import json

import httpx

from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.places.module import PATH, TOKEN, Places, place_tools, same_place
from orion.tools.base import Tool, ToolContext

CTX = ToolContext("owner", "c", context_trust=Trust.OWNER)


async def call(tools: dict[str, Tool], name: str, **args: object) -> str:
    tool = tools[name]
    result = await tool.handler(tool.parse(json.dumps(args)), CTX)
    assert not result.is_error, result.content
    return result.content


def test_places_match_loosely() -> None:
    assert same_place("boulangerie", "La Boulangerie")
    assert same_place("la boulangerie", "Boulangerie Paul")
    assert not same_place("boulangerie", "bureau")
    assert not same_place("la", "le")


async def test_the_phone_says_where_and_the_reminder_goes_off(journal: SqliteJournal) -> None:
    places = Places(journal)
    tools = {t.name: t for t in place_tools(places)}
    said = await call(tools, "remind_at_place", place="boulangerie", what="acheter du pain")
    assert "jamais signalé ce lieu" in said  # tells how to set the phone up
    await call(tools, "remind_at_place", place="bureau", what="prendre le badge", when="leave")
    token = await places.new_token()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=places.app()), base_url="http://orion"
    ) as http:
        bad = await http.post(PATH, json={"place": "boulangerie"}, headers={"Authorization": "x"})
        assert bad.status_code == 401
        auth = {"Authorization": f"Bearer {token}"}
        arrived = await http.post(PATH, json={"place": "Boulangerie Paul"}, headers=auth)
        assert arrived.json() == {"ok": True, "reminders": 1}
        again = await http.post(PATH, json={"place": "Boulangerie Paul"}, headers=auth)
        assert again.json()["reminders"] == 0  # once
        at_work = await http.post(PATH, json={"place": "bureau", "event": "arrive"}, headers=auth)
        assert at_work.json()["reminders"] == 0  # this one is for leaving
        left = await http.post(PATH, json={"place": "bureau", "event": "leave"}, headers=auth)
        assert left.json()["reminders"] == 1

    first, second = await places.reports()
    assert "Il arrive à « boulangerie »" in first.notice and "acheter du pain" in first.notice
    assert "Il quitte « bureau »" in second.notice and first.key != second.key
    assert first.trust == Trust.OWNER_INSTANCE
    listing = await call(tools, "places")
    assert "Boulangerie Paul" in listing and "bureau" in listing
    events = await journal.read()
    assert token not in str([e.payload for e in events])  # only its digest
    assert any(e.kind == TOKEN for e in events)


async def test_a_new_token_replaces_the_old_and_reminders_can_be_cancelled(
    journal: SqliteJournal,
) -> None:
    places = Places(journal)
    tools = {t.name: t for t in place_tools(places)}
    old = await places.new_token()
    new = await places.new_token()
    await call(tools, "remind_at_place", place="pharmacie", what="l'ordonnance")
    (reminder,) = places.state.reminders
    await call(tools, "place_reminder_cancel", reminder=reminder)
    try:
        await places.signal(old, "pharmacie", "arrive")
        raise AssertionError("the old token still works")
    except PermissionError:
        pass
    assert await places.signal(new, "pharmacie", "arrive") == []
