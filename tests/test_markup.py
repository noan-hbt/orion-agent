"""Structured pieces in Orion's messages (D126): drawn by the app, plain text elsewhere."""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from pathlib import Path

from orion.channels.inbox import Inbound, OwnerInbox, nothing_to_show
from orion.channels.markup import parse, plain, public
from orion.core.journal import SqliteJournal
from orion.mobile.channel import MobileChannel
from orion.mobile.devices import Devices, session_key
from tests.fakes import ScriptedProvider, reply
from tests.test_imessage import make_orion, until

MESSAGE = """Trois annonces collent. Laquelle tu veux visiter ?
<choices multiple send="Demander une visite">
- 680 € · rue Saint-Martin
- 640 € · av. des Fenots
</choices>
<card title="680 € · rue Saint-Martin" subtitle="42 m²" link="https://immo.example/1">
Refait à neuf
Visite samedi 10 h
</card>
<done undo="Annule le rappel de mardi">Rappel posé · mardi 9:00</done>
<choices>
- Plutôt 10 h
- Parfait
</choices>"""


def test_the_pieces_come_in_order() -> None:
    pieces = parse(MESSAGE)
    assert [p.kind for p in pieces] == ["text", "choices", "card", "done", "choices"]
    assert pieces[1].data == {
        "options": ["680 € · rue Saint-Martin", "640 € · av. des Fenots"],
        "multiple": True,
        "send": "Demander une visite",
    }
    assert pieces[2].data["lines"] == ["Refait à neuf", "Visite samedi 10 h"]
    assert pieces[3].data == {
        "text": "Rappel posé · mardi 9:00",
        "undo": "Annule le rappel de mardi",
    }
    assert pieces[4].data["multiple"] is False and pieces[4].data["send"] == ""


def test_other_channels_read_the_same_thing_as_text() -> None:
    text = plain(MESSAGE)
    assert "<" not in text
    assert "- 640 € · av. des Fenots" in text
    assert "680 € · rue Saint-Martin · 42 m²\nRefait à neuf" in text
    assert "https://immo.example/1" in text
    assert "✓ Rappel posé · mardi 9:00" in text
    assert plain("Pas de balise ici.") == "Pas de balise ici."


def test_other_channels_get_no_markdown_marks() -> None:
    """Seen for real on Telegram: `**bold**` and `*italic*` showed their stars."""
    text = plain("## Le point\n* C'est **important** et *urgent*, [ici](https://example.com).")
    assert text == "Le point\n• C'est important et urgent, ici (https://example.com)."
    assert plain("5 * 3, mon_fichier_test.py") == "5 * 3, mon_fichier_test.py"


def test_what_is_malformed_stays_as_written() -> None:
    for text in (
        "<choices>\n- jamais fermée",
        "<card>sans titre</card>",
        "<choices>\n</choices>",
        "a < b et c > d",
    ):
        assert public(text) == []
        assert plain(text) == text
    unsafe = parse('<card title="x" link="javascript:alert(1)">y</card>')
    assert unsafe[0].data["link"] == ""


async def test_the_app_draws_them_telegram_reads_text(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    provider = ScriptedProvider(reply(MESSAGE), reply(MESSAGE))
    orion = make_orion(journal, provider)
    await orion.start()
    inbox = OwnerInbox(orion, coalesce_seconds=0.05)

    class Telegramish:
        name = "telegram"

        def __init__(self) -> None:
            self.said: list[str] = []

        async def available(self) -> bool:
            return True

        async def deliver(self, text: str, correlation_id: str | None, quiet: bool = False) -> None:
            self.said.append(text)

        def working(self, item: Inbound) -> AbstractAsyncContextManager[None]:
            return nothing_to_show()

    telegram = Telegramish()
    devices = Devices(journal, "https://orion.example", session_key(tmp_path / "m"))
    channel = MobileChannel(journal, inbox, devices, tmp_path / "m")
    inbox.register(telegram)
    await channel.start()
    worker = asyncio.create_task(inbox.run())
    try:
        await channel.receive("iphone", "des annonces ?")
        await until(lambda: any(s.who == "orion" for s in channel.state.said))
        shown = channel.state.said[-1].public()
        assert [b["type"] for b in shown["blocks"]] == [
            "text",
            "choices",
            "card",
            "done",
            "choices",
        ]
        assert shown["text"] == MESSAGE  # journaled as sent

        inbox.submit(Inbound("telegram", ("t1",), "et sur telegram ?"))
        await until(lambda: bool(telegram.said))
        assert "<" not in telegram.said[0] and "- Parfait" in telegram.said[0]
    finally:
        worker.cancel()
