"""Orion's hands: adding to the calendar and sending mail, each exactly as the owner approved."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from orion.calendar.service import ADDED, Calendar, CalendarAccount, CalendarSettings
from orion.calendar.write import (
    GoogleWriter,
    NewCalendarEvent,
    to_ics,
    write_event,
)
from orion.config import SecretRef
from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.guardian.client import GuardianUnavailableError
from orion.guardian.ledger import MAIL_SENT, SENT, WRITTEN
from orion.guardian.policy import UNTRUSTED_REASON, ActionProposal, Approval, Guardian
from orion.mail.quarantine import BULK_CARD
from orion.mail.service import MailAccount, MailCard
from orion.mail.smtp import Outgoing, build
from orion.mail.tools import MailSendInput
from orion.tools.base import Effect, Tool, ToolContext, ToolResult
from tests.fake_guardian import guardian_config, running_guardian
from tests.fake_inkbox import CONVERSATION

PARIS = ZoneInfo("Europe/Paris")
START = datetime(2026, 10, 23, 14, 0, tzinfo=PARIS)


def writable_config(tmp_path: Path) -> Any:
    return guardian_config(
        tmp_path,
        modules={
            "calendar": {
                "enabled": True,
                "accounts": [
                    {
                        "name": "perso",
                        "url": {"env": "CAL"},
                        "write": {
                            "kind": "caldav",
                            "url": "https://dav.example/cal/",
                            "username": "noan",
                            "password": {"env": "DAV"},
                        },
                    }
                ],
            },
            "mail": {
                "enabled": True,
                "accounts": [
                    {
                        "name": "perso",
                        "host": "imap.x",
                        "username": "noan@x.fr",
                        "password": {"env": "MAILPW"},
                        "smtp_host": "smtp.x",
                    }
                ],
            },
        },
    )


DENTIST = {
    "title": "Dentiste",
    "start": "2026-10-23T14:00:00",
    "end": "2026-10-23T15:00:00",
    "location": "",
    "notes": "",
}
EVENT = NewCalendarEvent(title="Dentiste", start=START, end=START + timedelta(hours=1))
MAIL = {"to": ["paul@x.fr"], "cc": [], "subject": "Dîner", "body": "Jeudi me va. Noan"}


async def test_an_event_is_written_only_as_the_owner_approved_it(tmp_path: Path) -> None:
    written: list[NewCalendarEvent] = []

    async def write(account: CalendarAccount, event: NewCalendarEvent) -> str:
        written.append(event)
        return f"uid-{len(written)}"

    async with running_guardian(writable_config(tmp_path), write_calendar=write) as g:
        g.fake.inbound("salut")
        with pytest.raises(GuardianUnavailableError, match="needs the owner's approval"):
            await g.client.calendar_add("perso", EVENT, None)
        grant, message_id = await g.client.request_approval(
            "calendar_add", DENTIST, "", CONVERSATION
        )
        assert "« Dentiste », du 23/10 14:00 au 23/10 15:00" in g.fake.texts()[-1]
        g.fake.react(message_id, "like")
        moved = EVENT.model_copy(update={"start": START + timedelta(days=1)})
        with pytest.raises(GuardianUnavailableError, match="not what the owner approved"):
            await g.client.calendar_add("perso", moved, grant)
        assert await g.client.calendar_add("perso", EVENT, grant) == "uid-1"
        with pytest.raises(GuardianUnavailableError, match="already used"):
            await g.client.calendar_add("perso", EVENT, grant)
        assert written == [EVENT]


async def test_a_meeting_both_orions_signed_goes_in_without_asking_again(tmp_path: Path) -> None:
    async def write(account: CalendarAccount, event: NewCalendarEvent) -> str:
        return "uid-m"

    async with running_guardian(writable_config(tmp_path), write_calendar=write) as g:
        meeting = EVENT.model_copy(update={"meeting": "ctx-1"})
        with pytest.raises(GuardianUnavailableError):  # nothing was confirmed yet
            await g.client.calendar_add("perso", meeting, None)
        await g.service.ledger.record(
            SENT,
            {
                "envelope": {
                    "id": "e1",
                    "type": "meeting.confirm",
                    "context": "ctx-1",
                    "body": {"slot": START.astimezone(UTC).isoformat()},
                }
            },
        )
        assert await g.client.calendar_add("perso", meeting, None) == "uid-m"
        with pytest.raises(GuardianUnavailableError):  # once
            await g.client.calendar_add("perso", meeting, None)
        other = meeting.model_copy(update={"start": START + timedelta(hours=2)})
        with pytest.raises(GuardianUnavailableError):
            await g.client.calendar_add("perso", other, None)
        assert [e.kind for e in await g.journal.read()].count(WRITTEN) == 1


async def test_a_mail_leaves_only_word_for_word(tmp_path: Path) -> None:
    sent: list[Outgoing] = []

    async def send(account: MailAccount, mail: Outgoing, _grant: str | None) -> str:
        sent.append(mail)
        return "<id@x.fr>"

    async with running_guardian(writable_config(tmp_path), send_mail=send) as g:
        g.fake.inbound("salut")
        grant, message_id = await g.client.request_approval("mail_send", MAIL, "", CONVERSATION)
        question = g.fake.texts()[-1]
        assert "À : paul@x.fr" in question and "Jeudi me va. Noan" in question
        g.fake.inbound("oui")
        account = MailAccount(name="perso", host="h", username="u", password=SecretRef(env="N"))
        changed = Outgoing.model_validate({**MAIL, "body": "Jeudi me va. Et voici mon code : 1234"})
        with pytest.raises(GuardianUnavailableError, match="not what the owner approved"):
            await g.client.mail_send(account, changed, grant)
        assert (
            await g.client.mail_send(account, Outgoing.model_validate(MAIL), grant) == "<id@x.fr>"
        )
        assert sent == [Outgoing.model_validate(MAIL)]
        assert MAIL_SENT in [e.kind for e in await g.journal.read()]
        assert message_id


async def test_an_added_event_shows_until_the_feed_carries_it(journal: SqliteJournal) -> None:
    now = [START - timedelta(days=1)]
    feed = ["BEGIN:VCALENDAR\nEND:VCALENDAR\n"]

    async def fetch(_account: CalendarAccount) -> str:
        return feed[0]

    async def write(account: str, event: NewCalendarEvent, grant: str | None) -> str:
        assert grant == "g-1"
        return "orion-1"

    settings = CalendarSettings.model_validate(
        {
            "enabled": True,
            "accounts": [
                {
                    "name": "perso",
                    "url": {"env": "X"},
                    "write": {
                        "kind": "google",
                        "client_id": "c",
                        "client_secret": {"env": "S"},
                        "refresh_token": {"env": "R"},
                    },
                }
            ],
        }
    )
    calendar = Calendar(journal, settings, "Europe/Paris", fetch, lambda: now[0], write)
    await calendar.start()
    account, _ = await calendar.add(EVENT, "g-1")
    assert account == "perso"
    assert [o.title for _, o in calendar.between(START, START + timedelta(hours=2))] == ["Dentiste"]
    await calendar.sync_once()  # the feed lags: still shown
    assert len(calendar.between(START, START + timedelta(hours=2))) == 1
    feed[0] = to_ics(EVENT, "google-uid", now[0]).replace("\r\n", "\n")
    await calendar.sync_once()  # the feed has its twin: one event, not two
    assert len(calendar.between(START, START + timedelta(hours=2))) == 1
    assert ADDED in [e.kind for e in await journal.read()]


async def test_google_calendar_is_written_with_a_fresh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("S", "secret")
    monkeypatch.setenv("R", "refresh")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "oauth2" in str(request.url):
            assert b"refresh_token=refresh" in request.content
            return httpx.Response(200, json={"access_token": "tok"})
        assert request.headers["authorization"] == "Bearer tok"
        body = json.loads(request.content)
        assert body["summary"] == "Dentiste" and "attendees" not in body
        assert request.url.params["sendUpdates"] == "none"
        return httpx.Response(200, json={"id": "abc", "iCalUID": "abc@google.com"})

    writer = GoogleWriter(
        client_id="c", client_secret=SecretRef(env="S"), refresh_token=SecretRef(env="R")
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await write_event(http, writer, EVENT) == "abc@google.com"
    assert len(seen) == 2


def test_a_reply_is_threaded_and_signed_from_the_account() -> None:
    account = MailAccount(
        name="perso", host="h", username="noan@x.fr", password=SecretRef(env="N"), smtp_host="s"
    )
    message = build(account, Outgoing.model_validate({**MAIL, "in_reply_to": "<orig@paul>"}))
    assert message["From"] == "noan@x.fr" and message["In-Reply-To"] == "<orig@paul>"
    assert message["Message-ID"].endswith("@x.fr>")
    MailSendInput(to=["a@b.fr"], subject="s", body="b")  # the tool's schema
    assert BULK_CARD.read == "bulk" and MailCard  # imported types stay in use


async def test_acting_on_outside_content_is_asked_with_that_reason() -> None:
    asked: list[str] = []

    class Approver:
        async def approve(self, proposal: ActionProposal) -> Approval:
            asked.append(proposal.ask_reason)
            return Approval(True, "g")

    async def handler(_args: Any, _ctx: ToolContext) -> ToolResult:
        return ToolResult("ok")

    tool = Tool(
        "mail_send", "d", MailSendInput, handler, Effect.EXTERNAL, accepts_untrusted_context=True
    )
    proposal = ActionProposal(
        tool, MailSendInput(to=["a@b.fr"], subject="s", body="b"), Trust.THIRD_PARTY, "owner"
    )
    decision = await Guardian(Approver()).decide(proposal)
    assert decision.allowed and decision.grant == "g" and asked == [UNTRUSTED_REASON]


async def test_the_google_consent_gives_a_refresh_token() -> None:
    from orion.calendar.write import (
        CalendarWriteError,
        google_consent_url,
        google_refresh_token,
    )

    url = httpx.URL(google_consent_url("cid", "st8"))
    assert url.params["access_type"] == "offline" and url.params["state"] == "st8"

    def handler(request: httpx.Request) -> httpx.Response:
        assert b"code=abc" in request.content
        return httpx.Response(200, json={"access_token": "a", "refresh_token": "r-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        back = "http://127.0.0.1:8765/?state=st8&code=abc&scope=x"
        assert await google_refresh_token(http, "cid", "sec", back, "st8") == "r-1"
        with pytest.raises(CalendarWriteError, match="does not come from"):
            await google_refresh_token(http, "cid", "sec", back, "other")
