from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import pytest

from orion.config import AgentConfig, OrionConfig
from orion.context.compiler import ContextCompiler
from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.core.projections import ProjectionRunner
from orion.guardian.policy import ActionProposal, Approval, Guardian
from orion.mail import imap
from orion.mail.imap import Batch, RawMail, parse_message
from orion.mail.module import MailModule
from orion.mail.quarantine import (
    BULK_CARD,
    FAILED_CARD,
    SUSPICIOUS,
    Category,
    Importance,
    Quarantine,
)
from orion.mail.service import CARD, MailAccount, Mailbox, MailSettings, MailState
from orion.mail.tools import mail_tools
from orion.memory.service import MemoryService
from orion.memory.store import MemoryStore
from orion.memory.tools import memory_tools
from orion.modules.registry import load
from orion.runtime import Orion
from orion.tools.base import ToolContext, ToolRegistry
from tests.fakes import ScriptedProvider, call_tools, fail, reply

PARIS = ZoneInfo("Europe/Paris")
NOW = datetime(2026, 10, 20, 8, 0, tzinfo=PARIS)
OWNER = "noan@example.com"

PLAIN = b"""From: Paul Martin <paul@example.com>
To: noan@example.com
Subject: Diner samedi ?
Date: Mon, 19 Oct 2026 18:30:00 +0200
Message-ID: <abc@example.com>
Content-Type: text/plain; charset=utf-8

Salut, on dine samedi soir ? Dis-moi avant jeudi.
"""
HTML = b"""From: EDF <facture@edf.example>
Subject: Votre facture
Date: Mon, 19 Oct 2026 07:00:00 +0000
Message-ID: <f1@edf.example>
Content-Type: text/html; charset=utf-8

<html><style>p{color:red}</style><body><p>Montant : <b>54,20 EUR</b></p>
<p>A payer avant le 30/10.</p></body></html>
"""
NEWSLETTER = b"""From: Shop <news@shop.example>
Subject: -50% ce week-end
Date: Mon, 19 Oct 2026 09:00:00 +0000
List-Unsubscribe: <mailto:unsub@shop.example>
Content-Type: text/plain

Promo !
"""


def test_parse_message_reads_plain_html_and_bulk_mail() -> None:
    paul = parse_message(7, PLAIN, NOW)
    assert (paul.from_name, paul.from_addr, paul.subject) == (
        "Paul Martin",
        "paul@example.com",
        "Diner samedi ?",
    )
    assert paul.body == "Salut, on dine samedi soir ? Dis-moi avant jeudi."
    assert paul.received_at == datetime(2026, 10, 19, 16, 30, tzinfo=UTC)
    assert not paul.bulk and paul.message_id == "<abc@example.com>"
    edf = parse_message(8, HTML, NOW)
    assert edf.body == "Montant : 54,20 EUR A payer avant le 30/10."  # no style, no tags
    assert parse_message(9, NEWSLETTER, NOW).bulk


class FakeIMAP:
    """Stands in for imaplib.IMAP4_SSL; records what Orion asked."""

    mailbox: ClassVar[dict[int, bytes]] = {}
    uidvalidity = 42
    log: ClassVar[list[tuple[str, ...]]] = []

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.log.append(("connect", host, str(port)))

    def __enter__(self) -> FakeIMAP:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def login(self, user: str, password: str) -> None:
        self.log.append(("login", user, password))

    def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.log.append(("select", folder, str(readonly)))
        return "OK", [b"3"]

    def response(self, code: str) -> tuple[str, list[bytes]]:
        return code, [str(self.uidvalidity).encode()]

    def uid(self, command: str, *args: str) -> tuple[str, list[Any]]:
        self.log.append((command, *args))
        if command == "SEARCH":
            if args[0] == "SINCE":
                uids = sorted(self.mailbox)
            else:
                start = int(args[1].split(":")[0])
                uids = [u for u in sorted(self.mailbox) if u >= start] or [max(self.mailbox)]
            return "OK", [" ".join(map(str, uids)).encode()]
        uid = int(args[0])
        return "OK", [(f"{uid} (BODY[] {{1}}".encode(), self.mailbox[uid]), b")"]


def test_fetch_is_strictly_read_only_and_resumes_after_the_last_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeIMAP.mailbox = {10: PLAIN, 11: HTML, 12: NEWSLETTER}
    FakeIMAP.log = []
    monkeypatch.setattr(imap.imaplib, "IMAP4_SSL", FakeIMAP)
    first = imap.fetch_since("imap.x", 993, "noan", "app-pw", "INBOX", 0, 0, 2, 2, NOW)
    assert ("select", "INBOX", "True") in FakeIMAP.log  # EXAMINE: nothing is marked read
    assert ("SEARCH", "SINCE", "18-Oct-2026") in FakeIMAP.log
    assert all(entry[2] == "(BODY.PEEK[])" for entry in FakeIMAP.log if entry[0] == "FETCH")
    assert [m.uid for m in first.mails] == [11, 12] and first.skipped == 1
    assert (first.uidvalidity, first.last_uid) == (42, 12)

    FakeIMAP.log = []
    again = imap.fetch_since("imap.x", 993, "noan", "app-pw", "INBOX", 42, 12, 2, 25, NOW)
    assert ("SEARCH", "UID", "13:*") in FakeIMAP.log
    assert again.mails == [] and again.last_uid == 12  # "*" returned 12: not new

    FakeIMAP.uidvalidity = 43  # the server renumbered the folder: start over from the date
    FakeIMAP.log = []
    renumbered = imap.fetch_since("imap.x", 993, "noan", "app-pw", "INBOX", 42, 12, 2, 25, NOW)
    assert renumbered.uidvalidity == 43 and len(renumbered.mails) == 3
    FakeIMAP.uidvalidity = 42


def json_reply(answer: dict[str, Any]) -> Any:
    return reply(json.dumps(answer))


async def test_quarantine_makes_a_bounded_card_and_flags_injections() -> None:
    provider = ScriptedProvider(
        json_reply(
            {
                "category": "bill",
                "importance": "high",
                "needs_reply": False,
                "due": "2026-10-30",
                "amount": 54.2,
                "currency": "EUR",
                "summary": "EDF demande 54,20 € avant le 30/10. " * 20,
                "suspicious": False,
            }
        ),
        json_reply({"category": "work", "summary": "x", "suspicious": True}),
        reply("not json"),
        fail(),
    )
    quarantine = Quarantine(provider, "q-model")
    edf = await quarantine.card(parse_message(8, HTML, NOW))
    assert (edf.category, edf.importance, edf.due, edf.amount) == (
        Category.BILL,
        Importance.HIGH,
        date(2026, 10, 30),
        54.2,
    )
    assert len(edf.summary) <= 200
    request = provider.requests[0]
    assert request.tools == [] and request.model == "q-model"
    assert "Montant : 54,20 EUR" in request.messages[1]["content"]

    evil = await quarantine.card(parse_message(7, PLAIN, NOW))
    assert evil.suspicious and evil.summary == SUSPICIOUS and evil.importance is Importance.LOW
    assert await quarantine.card(parse_message(7, PLAIN, NOW)) == FAILED_CARD
    assert await quarantine.card(parse_message(7, PLAIN, NOW)) == FAILED_CARD
    assert await quarantine.card(parse_message(9, NEWSLETTER, NOW)) == BULK_CARD
    assert len(provider.requests) == 4  # bulk mail never reaches a model


def settings(**extra: Any) -> MailSettings:
    return MailSettings.model_validate(
        {
            "enabled": True,
            "owner_emails": [OWNER],
            "accounts": [
                {
                    "name": "perso",
                    "host": "imap.x",
                    "username": "noan",
                    "password": {"env": "MAIL_PERSO_PASSWORD"},
                }
            ],
            **extra,
        }
    )


@pytest.fixture(autouse=True)
def app_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_PERSO_PASSWORD", "app-pw")


class Server:
    def __init__(self, *mails: RawMail) -> None:
        self.mails = list(mails)
        self.down = False
        self.cursors: list[tuple[int, int]] = []

    async def fetch(
        self,
        account: MailAccount,
        uidvalidity: int,
        last_uid: int,
        lookback_days: int,
        limit: int,
        now: datetime,
    ) -> Batch:
        assert account.password.resolve().get_secret_value() == "app-pw"
        self.cursors.append((uidvalidity, last_uid))
        if self.down:
            raise OSError("connection refused")
        new = [m for m in self.mails if m.uid > last_uid]
        return Batch(42, max([last_uid, *(m.uid for m in new)]), new)


PAUL_CARD = {
    "category": "personal",
    "importance": "high",
    "needs_reply": True,
    "summary": "Paul propose un dîner samedi soir, réponse avant jeudi.",
}


def mailbox(journal: SqliteJournal, server: Server, provider: ScriptedProvider) -> Mailbox:
    return Mailbox(
        journal,
        settings(),
        Quarantine(provider, "q"),
        "Europe/Paris",
        server.fetch,
        clock=lambda: NOW,
    )


async def test_sync_journals_cards_not_mail_and_resumes(journal: SqliteJournal) -> None:
    own = RawMail(13, "<n@x>", OWNER, "Noan", "note", NOW, "secret", False)
    server = Server(parse_message(10, PLAIN, NOW), parse_message(12, NEWSLETTER, NOW), own)
    box = mailbox(journal, server, ScriptedProvider(json_reply(PAUL_CARD)))
    await box.start()
    assert await box.sync_once() == {"perso": 2}  # the owner's own mail is skipped
    cards = [e for e in await journal.read() if e.kind == CARD]
    assert all(e.trust is Trust.THIRD_PARTY for e in cards)
    assert cards[0].provenance.source == "mail:perso:<abc@example.com>"
    assert "Dis-moi avant jeudi" not in str([e.payload for e in cards])  # no body kept

    assert await box.sync_once() == {"perso": 0}
    assert server.cursors[-1] == (42, 13)
    server.down = True
    assert await box.sync_once() == {}
    rebuilt = MailState()
    await ProjectionRunner(journal, [rebuilt]).catch_up()
    assert rebuilt.cards == box.state.cards and rebuilt.cursors == {"perso": (42, 13)}


async def test_tool_alerts_and_digest_carry_third_party_trust(journal: SqliteJournal) -> None:
    server = Server(parse_message(10, PLAIN, NOW), parse_message(12, NEWSLETTER, NOW))
    box = mailbox(journal, server, ScriptedProvider(json_reply(PAUL_CARD)))
    await box.start()
    await box.sync_once()
    [tool] = mail_tools(box)
    result = await tool.handler(tool.parse("{}"), ToolContext(thread_id="o", correlation_id="c"))
    assert result.trust is Trust.THIRD_PARTY
    [line] = result.content.splitlines()[1:]
    assert "Paul Martin <paul@example.com> — « Diner samedi ? » : Paul propose" in line
    assert "[personal, important, réponse attendue]" in line
    everything = await tool.handler(
        tool.parse('{"everything": true}'), ToolContext(thread_id="o", correlation_id="c")
    )
    assert "-50%" in everything.content

    assert await box.alerts(NOW) == []  # 13 h old: too late to alert
    [alert] = await box.alerts(datetime(2026, 10, 19, 22, 0, tzinfo=PARIS))
    assert (
        alert.trust is Trust.THIRD_PARTY
        and alert.why == "Paul Martin attend sans doute une réponse"
    )
    digest = await box.digest(NOW, NOW - timedelta(days=1), first_slot=True, last_slot=False)
    assert [d.trust for d in digest] == [Trust.THIRD_PARTY, Trust.THIRD_PARTY]
    assert digest[1].text == "- et 1 autre(s) mail(s) sans intérêt particulier"


class Approver:
    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.asked: list[str] = []

    async def approve(self, proposal: ActionProposal) -> Approval:
        self.asked.append(proposal.ask_reason)
        return Approval(self.answer)


@pytest.mark.parametrize("answer", [False, True])
async def test_after_reading_mail_remembering_needs_the_owner(
    journal: SqliteJournal, answer: bool
) -> None:
    store = MemoryStore(":memory:")
    await store.open()
    memory = MemoryService(journal, store)
    server = Server(parse_message(10, PLAIN, NOW))
    box = mailbox(journal, server, ScriptedProvider(json_reply(PAUL_CARD)))
    await box.start()
    approver = Approver(answer)
    provider = ScriptedProvider(
        call_tools(("mail", {})),
        reply("Paul te propose un dîner samedi."),
        call_tools(("remember", {"statement": "Noan dîne avec Paul samedi."})),
        reply("ok"),
    )
    orion = Orion(
        journal=journal,
        provider=provider,
        registry=ToolRegistry([*mail_tools(box), *memory_tools(memory)]),
        guardian=Guardian(approver),
        compiler=ContextCompiler("C"),
        agent=AgentConfig(),
        memory=memory,
    )
    await orion.start()
    await orion.handle_owner_message("owner", "des mails ?", "imessage", now=NOW)
    await orion.handle_owner_message("owner", "retiens que je dîne avec lui", "imessage", now=NOW)
    assert len(approver.asked) == 1 and "pourrait m'avoir influencé" in approver.asked[0]
    facts = await store.facts()
    if answer:
        [fact] = facts
        assert fact.trust is Trust.THIRD_PARTY  # remembered, and marked as such
    else:
        assert facts == []
    await store.close()


def test_the_module_loads_and_reports_in_doctor(tmp_path: Path) -> None:
    cfg = OrionConfig.model_validate(
        {
            "data_dir": tmp_path,
            "modules": {
                "mail": {
                    "enabled": True,
                    "accounts": [
                        {
                            "name": "perso",
                            "host": "imap.gmail.com",
                            "username": "noan",
                            "password": {"env": "MAIL_PERSO_PASSWORD"},
                        }
                    ],
                }
            },
        }
    )
    [module] = [m for m in load(cfg) if isinstance(m, MailModule)]
    assert module.doctor() == ["perso (noan on imap.gmail.com) ok"]


async def test_a_promise_in_a_mail_joins_the_ledger_and_closes_on_the_reply(
    journal: SqliteJournal,
) -> None:
    from orion.mail.smtp import Outgoing, Postman
    from orion.memory.model import CommitmentStatus
    from orion.memory.service import MemoryService
    from orion.memory.store import MemoryStore

    store = MemoryStore(":memory:")
    await store.open()
    memory = MemoryService(journal, store)
    card = {
        **PAUL_CARD,
        "commitment": {"by": "owner", "what": "Répondre à Paul pour le dîner", "due": "2026-10-22"},
    }
    server = Server(parse_message(10, PLAIN, NOW))
    box = Mailbox(
        journal,
        settings(),
        Quarantine(ScriptedProvider(json_reply(card)), "q"),
        "Europe/Paris",
        server.fetch,
        clock=lambda: NOW,
        memory=memory,
    )
    await box.start()
    await box.sync_once()
    [owed] = await store.commitments()
    assert owed.debtor == "owner" and owed.due == "2026-10-22"
    assert "d'après un mail de Paul" in owed.what and owed.trust is Trust.THIRD_PARTY

    sent: list[Outgoing] = []

    async def send(account: MailAccount, mail: Outgoing, grant: str | None) -> str:
        sent.append(mail)
        return "<reply@x>"

    box.settings.accounts[0].smtp_host = "smtp.x"
    postman = Postman(journal, box, send, memory)
    [paul] = box.state.cards.values()
    reply = Outgoing(to=["paul@example.com"], subject="Re: Dîner", body="Avec plaisir.")
    await postman.send(reply, paul, "g")
    assert sent[0].in_reply_to == paul.message_id  # threaded
    assert await store.commitments() == []
    [done] = await store.commitments(CommitmentStatus.DONE)
    assert done.evidence == "mail envoyé <reply@x>"
    await store.close()


async def test_ads_and_newsletters_promise_nothing() -> None:
    from orion.mail.quarantine import Card

    provider = ScriptedProvider(
        json_reply(
            {
                "category": "newsletter",
                "commitment": {"by": "owner", "what": "Profiter de -50 %"},
            }
        )
    )
    card: Card = await Quarantine(provider, "q").card(parse_message(10, PLAIN, NOW))
    assert card.promise is None
