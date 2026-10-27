"""Orion and his app, more joined up (D140): pieces it draws, the app following what changes,
notifications that lead somewhere, and what Orion follows for him (agenda, mails, nights)."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx
import pytest

from orion.calendar.ics import Occurrence, Standing
from orion.channels.markup import plain, public
from orion.core.events import NewEvent, Provenance, Trust
from orion.core.journal import SqliteJournal
from orion.mail.card import Card, Category, Importance
from orion.mail.service import MailCard
from orion.mobile.channel import changed_part
from orion.mobile.control import ControlError
from orion.mobile.notebook import Notebook
from orion.mobile.server import mobile_app
from orion.thinking.module import NIGHT, ThinkingState
from tests.test_mobile_channel import URL, World, pair, world  # noqa: F401  (fixture)

PARIS = ZoneInfo("Europe/Paris")
NOW = datetime(2026, 10, 27, 8, 0, tzinfo=PARIS)


# -- the pieces ------------------------------------------------------------------------------


def test_an_event_a_mail_a_screen_and_a_progress_are_drawn() -> None:
    text = (
        "La visite est calée.\n"
        '<event title="Visite du T2" when="2026-11-01T10:00" end="2026-11-01T10:30" '
        'where="12 rue X"/>\n'
        '<mail from="Agence Foncia" subject="Votre dossier">Il manque   un papier.</mail>\n'
        '<open screen="papers"/>\n'
        '<progress label="Dossier" value="1.7"/>'
    )
    pieces = public(text)
    assert [p["type"] for p in pieces] == ["text", "event", "mail", "open", "progress"]
    event, mail, opened, progress = pieces[1:]
    assert event["when"] == "2026-11-01T10:00:00" and not event["all_day"]
    assert mail["snippet"] == "Il manque un papier."
    assert opened == {"type": "open", "screen": "papers", "label": "Voir tes papiers"}
    assert progress["value"] == 1.0  # held between 0 and 1
    flat = plain(text)
    assert "Visite du T2 · 01/11 10:00 · 12 rue X" in flat
    assert "✉ Agence Foncia · « Votre dossier »" in flat
    assert "Dossier : 100 %" in flat
    assert "papers" not in flat  # a screen of the app means nothing on another channel


def test_what_the_app_cannot_draw_stays_text() -> None:
    bad = '<open screen="ailleurs"/> <event title="Sans date" when="bientôt"/>'
    assert public(bad) == []  # both malformed: only text
    assert plain(bad) == bad


def test_the_limits_the_doc_gives_are_the_parser_s() -> None:
    six = "<choices>\n" + "\n".join(f"- {n}" for n in "abcdef") + "\n</choices>"
    assert public(six)[0]["options"] == list("abcde")  # five quick answers at most
    ticks = "<choices multiple>\n" + "\n".join(f"- {n}" for n in "abcdefghij") + "\n</choices>"
    assert len(public(ticks)[0]["options"]) == 8
    cards = "".join(f'<card title="{n}">x</card>' for n in range(4))
    drawn = public(cards)
    assert [p["type"] for p in drawn] == ["card", "card", "card", "text"]
    assert drawn[-1]["text"] == '<card title="3">x</card>'


# -- the app follows what changes -----------------------------------------------------------


def test_each_change_names_a_part_of_the_app_and_nothing_more() -> None:
    assert changed_part("task.finished") == "tasks"
    assert changed_part("mission.ran") == "missions"
    assert changed_part("calendar.event.seen") == "agenda"
    assert changed_part("calendar.synced") is None  # a sync that changed nothing
    assert changed_part("mail.card") == "mail"
    assert changed_part("spend.recorded") == "cost"
    assert changed_part("mobile.outbound") is None  # messages have their own event


async def test_the_open_app_hears_what_changed_once_per_burst(world: World) -> None:  # noqa: F811
    channel = world.channel
    follower = asyncio.create_task(channel.changes())
    await asyncio.sleep(0.05)
    async with channel.hub.listen() as queue:
        journal = cast(SqliteJournal, channel._journal)  # pyright: ignore[reportPrivateUsage]
        await journal.append(
            [
                NewEvent(
                    kind=kind,
                    payload={},
                    provenance=Provenance(source="test", actor="orion:test"),
                    trust=Trust.SYSTEM,
                )
                for kind in ("task.started", "task.reported", "mission.ran", "conversation.x")
            ]
        )
        event = await asyncio.wait_for(queue.get(), 2.0)
        assert event == {"type": "changed", "what": ["missions", "tasks"]}
        await asyncio.sleep(0.5)
        assert queue.empty()  # one repaint for the burst
    follower.cancel()


async def test_nobody_listening_nothing_is_kept(world: World) -> None:  # noqa: F811
    channel = world.channel
    follower = asyncio.create_task(channel.changes())
    await asyncio.sleep(0.05)
    journal = cast(SqliteJournal, channel._journal)  # pyright: ignore[reportPrivateUsage]
    await journal.append(
        [
            NewEvent(
                kind="task.started",
                payload={},
                provenance=Provenance(source="t", actor="t"),
                trust=Trust.SYSTEM,
            )
        ]
    )
    await asyncio.sleep(0.5)
    assert not channel._changed  # pyright: ignore[reportPrivateUsage]
    follower.cancel()


# -- notifications that lead somewhere ------------------------------------------------------


async def test_a_notification_says_what_it_is_about(world: World) -> None:  # noqa: F811
    await pair(world)
    await world.channel.subscribe("iphone", "https://web.push.apple.com/abc", "p256dh", "auth")
    await world.channel.deliver("Ton colis arrive demain.", None)
    sent = world.pusher.sent[-1]
    assert sent["kind"] == "message" and str(sent["tag"])


def test_the_service_worker_opens_the_right_place_and_refuses_without_face_id() -> None:
    sw = (Path(__file__).parents[1] / "src/orion/mobile/static/sw.js").read_text(encoding="utf-8")
    assert '"/app/#" + (KINDS.has(kind) ? kind + ":" : "")' in sw
    assert "/refuse" in sw and "/options" not in sw  # yes always goes through Face ID


# -- what Orion follows for him -------------------------------------------------------------


def _occurrence(key: str, start: datetime, title: str, location: str = "") -> Occurrence:
    return Occurrence(
        key=key,
        start=start,
        end=start + timedelta(hours=1),
        all_day=False,
        standing=Standing.OWN,
        title=title,
        location=location,
    )


class FakeCalendar:
    def __init__(self, found: list[tuple[str, Occurrence]]) -> None:
        self.found = found
        self.asked: tuple[datetime, datetime] | None = None

    def between(self, start: datetime, end: datetime) -> list[tuple[str, Occurrence]]:
        self.asked = (start, end)
        return self.found


class FakeMailbox:
    def __init__(self, cards: list[MailCard]) -> None:
        self.cards = cards
        self.worth_it: bool | None = None

    def recent(self, since: datetime, worth_it: bool = False) -> list[MailCard]:
        self.worth_it = worth_it
        return self.cards


def _mail() -> MailCard:
    return MailCard(
        id="mc_1",
        account="perso",
        from_addr="contact@foncia.fr",
        from_name="Agence Foncia",
        subject="Votre dossier",
        received_at=NOW - timedelta(hours=2),
        card=Card(
            category=Category.OTHER,
            importance=Importance.HIGH,
            summary="Il manque l'avis d'imposition.",
            needs_reply=True,
            due=date(2026, 11, 3),
            amount=None,
            currency=None,
            suspicious=False,
            read="headers",
        ),
        text="Le texte entier du mail, qui ne doit jamais partir vers l'app.",
    )


class FakeThinking:
    def __init__(self, state: ThinkingState) -> None:
        self.state = state
        self.settings = SimpleNamespace(budget_usd=0.5)

    async def sync(self) -> None:
        return None


def _notebook(journal: SqliteJournal, **kwargs: Any) -> Notebook:
    return Notebook(journal, lambda: NOW, **kwargs)


async def test_the_agenda_is_the_next_two_weeks(journal: SqliteJournal) -> None:
    calendar = FakeCalendar(
        [("perso", _occurrence("k1", NOW + timedelta(hours=2), "Dentiste", "Cabinet"))]
    )
    notebook = _notebook(journal, calendar=lambda: calendar)
    events = notebook.agenda()
    assert events[0]["title"] == "Dentiste" and events[0]["location"] == "Cabinet"
    assert calendar.asked == (NOW, NOW + timedelta(days=14))
    with pytest.raises(ControlError):
        _notebook(journal).agenda()  # the calendar off


async def test_mails_show_what_orion_made_of_them_never_the_mail(journal: SqliteJournal) -> None:
    mailbox = FakeMailbox([_mail()])
    shown = _notebook(journal, mailbox=lambda: mailbox).mails()
    assert mailbox.worth_it is True  # newsletters and spam stay out
    assert shown[0]["summary"] == "Il manque l'avis d'imposition."
    assert shown[0]["important"] and shown[0]["needs_reply"] and shown[0]["due"] == "2026-11-03"
    assert "ne doit jamais" not in json.dumps(shown, ensure_ascii=False)


async def test_the_nights_keep_what_each_read_and_cost(journal: SqliteJournal) -> None:
    state = ThinkingState()
    for seq, day in enumerate(("2026-10-26", "2026-10-27"), start=1):
        await state.apply(
            SimpleNamespace(  # type: ignore[arg-type]
                seq=seq,
                redacted=False,
                kind=NIGHT,
                payload={"day": day, "upto": seq, "read": 10, "noted": True, "cost_usd": 0.3},
            )
        )
    nights = await _notebook(journal, thinking=lambda: FakeThinking(state)).nights()
    assert [n["day"] for n in nights["nights"]] == ["2026-10-27", "2026-10-26"]  # newest first
    assert nights["budget_usd"] == 0.5 and nights["nights"][0]["cost_usd"] == 0.3


async def test_the_screens_are_behind_the_session(journal: SqliteJournal, world: World) -> None:  # noqa: F811
    calendar = FakeCalendar([])
    notebook = _notebook(journal, calendar=lambda: calendar)
    app = mobile_app(world.channel, None, True, 30, notebook=notebook)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=URL, headers={"origin": URL}
    ) as http:
        assert (await http.get("/app/api/agenda")).status_code == 401
        await pair(
            World(world.channel, http, world.phone, world.provider, world.pusher, world.devices)
        )
        assert (await http.get("/app/api/agenda")).json() == {"events": []}
        assert (await http.get("/app/api/mails")).status_code == 404  # mail off: an empty state
