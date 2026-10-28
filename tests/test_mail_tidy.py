"""Writing to the mailbox: attachments, archiving, unsubscribing, drafts (D78)."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from orion.core.journal import SqliteJournal
from orion.guardian.service import _mail_tidy, _same_mail  # pyright: ignore[reportPrivateUsage]
from orion.mail import tidy
from orion.mail.imap import parse_message
from orion.mail.service import MailAccount
from orion.mail.smtp import Outgoing, Postman, build
from orion.mail.tidy import Tidied, Unsubscribe, local_tidy, parse_unsubscribe
from orion.mail.tools import DRAFTED, TIDIED, mail_tools
from orion.tools.base import ToolContext
from tests.fakes import ScriptedProvider
from tests.test_mail import NEWSLETTER, NOW, PAUL_CARD, PLAIN, Server, json_reply, mailbox


@pytest.fixture(autouse=True)
def app_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_PERSO_PASSWORD", "app-pw")


CTX = ToolContext(thread_id="owner", correlation_id="c", grant="g1")


def test_unsubscribe_headers_are_read_one_click_first() -> None:
    one = parse_unsubscribe(
        "<mailto:u@shop.example?subject=stop>, <https://shop.example/u/1>",
        "List-Unsubscribe=One-Click",
    )
    assert one == Unsubscribe("https://shop.example/u/1", "mailto:u@shop.example?subject=stop")
    page = parse_unsubscribe("<https://shop.example/prefs>", "")
    assert page == Unsubscribe(page="https://shop.example/prefs")
    assert parse_unsubscribe("", "") == Unsubscribe()


async def test_unsubscribing_posts_once_mails_or_returns_the_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = MailAccount.model_validate(
        {"name": "perso", "host": "imap.x", "username": "n@x", "password": {"env": "X"}}
    )
    found = {
        1: Unsubscribe(one_click="https://a.example/u"),
        2: Unsubscribe(one_click="https://a.example/u"),  # the same list: once
        3: Unsubscribe(mailto="mailto:stop@b.example?subject=unsubscribe%20me"),
        4: Unsubscribe(page="https://c.example/prefs"),
    }

    def headers(_account: MailAccount, _uids: list[int]) -> dict[int, Unsubscribe]:
        return found

    monkeypatch.setattr(tidy, "unsubscribe_headers", headers)
    mailed: list[Outgoing] = []

    async def send(_account: MailAccount, mail: Outgoing, _grant: str | None = None) -> str:
        mailed.append(mail)
        return "<id>"

    monkeypatch.setattr(tidy, "smtp_send", send)
    posted: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        posted.append(f"{request.url} {request.content.decode()}")
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        outcome = await local_tidy(account, "unsubscribe", [1, 2, 3, 4], http=http)
    assert outcome.done == 2 and outcome.pages == ["https://c.example/prefs"]
    assert posted == ["https://a.example/u List-Unsubscribe=One-Click"]
    assert mailed[0].to == ["stop@b.example"] and mailed[0].subject == "unsubscribe me"


def test_an_unsubscribe_header_cannot_make_him_write_to_anyone() -> None:
    from orion.mail.tidy import _mailto  # pyright: ignore[reportPrivateUsage]

    for steered in (
        "mailto:victim@x.com,other@y.com",
        "mailto:stop@b.example?cc=victim@x.com",
        "mailto:Victim <victim@x.com>",
    ):
        with pytest.raises(ValueError, match="single unsubscribe address"):
            _mailto(steered)
    long = _mailto("mailto:stop@b.example?body=" + "spam%20" * 100)
    assert long.body == "unsubscribe"  # a short line, or the plain word
    assert _mailto("mailto:stop@b.example?body=unsubscribe%20news").body == "unsubscribe news"


async def test_tools_tidy_in_batches_draft_and_attach(journal: SqliteJournal) -> None:
    server = Server(parse_message(10, PLAIN, NOW), parse_message(12, NEWSLETTER, NOW))
    box = mailbox(journal, server, ScriptedProvider(json_reply(PAUL_CARD)))
    await box.start()
    await box.sync_once()
    account = box.settings.accounts[0]
    account.smtp_host = "smtp.x"
    account.drafts_folder = "Drafts"
    asked: list[tuple[str, list[int], str | None]] = []
    moved: list[str | None] = []

    async def fake_tidy(
        _account: MailAccount, action: str, uids: list[int], grant: str | None, folder: str | None
    ) -> Tidied:
        asked.append((action, uids, grant))
        moved.append(folder)
        return Tidied(done=len(uids))

    marked: list[tuple[str, list[int]]] = []

    async def fake_mark(_account: MailAccount, action: str, uids: list[int]) -> int:
        marked.append((action, uids))
        return len(uids)

    drafts: list[Outgoing] = []

    async def fake_draft(_account: MailAccount, mail: Outgoing) -> None:
        drafts.append(mail)

    sent: list[Outgoing] = []

    async def send(_account: MailAccount, mail: Outgoing, _grant: str | None) -> str:
        sent.append(mail)
        return "<sent@x>"

    def lookup(file_id: str) -> tuple[str, str, bytes] | None:
        return ("Attestation.pdf", "application/pdf", b"%PDF-1") if file_id == "fi_1" else None

    tools = {
        t.name: t
        for t in mail_tools(
            box, Postman(journal, box, send), lookup, fake_tidy, fake_draft, fake_mark
        )
    }
    assert set(tools) == {"mail", "mail_send", "mail_tidy", "mail_draft", "mail_mark"}

    bulk = await tools["mail"].handler(tools["mail"].parse('{"bulk": true}'), CTX)
    [news] = [c for c in box.state.cards.values() if c.bulk]
    assert f"Shop <news@shop.example> : 1 mail(s) ({news.id})" in bulk.content

    args = {"action": "unsubscribe", "cards": [news.id], "senders": ["Shop"]}
    done = await tools["mail_tidy"].handler(tools["mail_tidy"].parse(json.dumps(args)), CTX)
    assert done.content.startswith("1 désabonnement") and asked == [("unsubscribe", [12], "g1")]
    assert _mail_tidy(args) == "Je peux me désabonner de ces 1 mail(s) ? Expéditeurs : Shop"

    [paul] = [c for c in box.state.cards.values() if not c.bulk]
    # Deleting is a move to the trash, approved like the rest; marking needs no approval.
    trash = {"action": "trash", "cards": [paul.id], "senders": ["Paul"]}
    await tools["mail_tidy"].handler(tools["mail_tidy"].parse(json.dumps(trash)), CTX)
    assert asked[-1][0] == "trash" and "corbeille" in _mail_tidy(trash)
    move = {"action": "move", "cards": [paul.id], "senders": ["Paul"], "folder": "Perso"}
    await tools["mail_tidy"].handler(tools["mail_tidy"].parse(json.dumps(move)), CTX)
    assert moved[-1] == "Perso" and "Perso" in _mail_tidy(move)
    mark = tools["mail_mark"].parse(json.dumps({"action": "unread", "cards": [paul.id]}))
    await tools["mail_mark"].handler(mark, CTX)
    assert marked == [("unread", [paul.uid])]
    draft = {"to": ["paul@example.com"], "subject": "Re: Dîner", "body": "Carrément !",
             "reply_to": paul.id}  # fmt: skip
    await tools["mail_draft"].handler(tools["mail_draft"].parse(json.dumps(draft)), CTX)
    assert drafts[0].in_reply_to == paul.message_id

    mail = {"to": ["agence@x.fr"], "subject": "Attestation", "body": "Ci-jointe.",
            "attachments": ["fi_1"]}  # fmt: skip
    await tools["mail_send"].handler(tools["mail_send"].parse(json.dumps(mail)), CTX)
    [attached] = sent[0].attachments
    assert attached.name == "Attestation.pdf" and base64.b64decode(attached.data) == b"%PDF-1"
    built = build(account, sent[0])
    assert [p.get_filename() for p in built.iter_attachments()] == ["Attestation.pdf"]
    # The guardian checks the approved files are the ones attached.
    approved: dict[str, Any] = {**mail, "cc": []}
    assert _same_mail(approved, sent[0])
    assert not _same_mail({**approved, "attachments": []}, sent[0])
    missing = {**mail, "attachments": ["fi_2"]}
    refused = await tools["mail_send"].handler(tools["mail_send"].parse(json.dumps(missing)), CTX)
    assert refused.is_error

    kinds = [e.kind for e in await journal.read()]
    assert TIDIED in kinds and DRAFTED in kinds
