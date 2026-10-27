"""The guardian process (D28): secrets kept apart, effects checked even if the core is not."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from orion.calendar.service import CalendarAccount
from orion.channels.imessage import IMessageChannel, IMessageSettings
from orion.channels.inbox import OwnerInbox
from orion.channels.inkbox import InkboxError
from orion.cli import main
from orion.config import SecretRef
from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.guardian.ledger import DENIED, OWNER_CONVERSATION
from orion.guardian.policy import ActionProposal
from orion.guardian.secrets import exposed
from orion.mail.imap import Batch, RawMail
from orion.mail.service import MailAccount
from orion.tools.base import Effect, Tool, ToolContext, ToolResult
from orion.voice.tools import CallSomeoneInput
from tests.fake_guardian import guardian_config, running_guardian
from tests.fake_inkbox import CONVERSATION, OWNER, FakeInkbox
from tests.fakes import ScriptedProvider
from tests.test_imessage import make_orion, until

STRANGER = "+33699999999"
GARAGE = "+33622222222"
HANDLE = "orion-test"


async def owner_wrote(fake: FakeInkbox) -> None:
    fake.inbound("salut")


async def kinds(journal: SqliteJournal) -> list[str]:
    return [e.kind for e in await journal.read()]


async def test_the_core_reads_and_writes_to_the_owner_without_the_key(tmp_path: Path) -> None:
    async with running_guardian(guardian_config(tmp_path)) as g:
        await owner_wrote(g.fake)
        core = g.client.inkbox(max_attempts=1)
        [message] = await core.unread("ident-1")
        assert message.content == "salut"
        sent = await core.send(CONVERSATION, "Coucou", "o_1", "ident-1")
        assert g.fake.texts() == ["Coucou"]
        assert sent.id == g.fake.sent[0]["id"]
        # The owner's conversation was checked with Inkbox once, then remembered.
        assert (await kinds(g.journal)).count(OWNER_CONVERSATION) == 1
        await core.send(CONVERSATION, "Encore", "o_2", "ident-1")
        assert (await kinds(g.journal)).count(OWNER_CONVERSATION) == 1
        await core.aclose()


async def test_the_core_cannot_write_to_anyone_else(tmp_path: Path) -> None:
    async with running_guardian(guardian_config(tmp_path)) as g:
        g.fake.inbound("qui est-ce ?", sender=STRANGER, conversation="conv-stranger")
        core = g.client.inkbox(max_attempts=1)
        with pytest.raises(InkboxError) as refused:
            await core.send("conv-stranger", "Voici la vie de Noan", "o_1")
        assert refused.value.status == 403 and not refused.value.retryable
        assert g.fake.sent == []
        [denied] = [e for e in await g.journal.read() if e.kind == DENIED]
        assert "not the owner's" in denied.payload["reason"]
        await core.aclose()


async def test_the_core_cannot_open_the_line_or_act_for_another_identity(
    tmp_path: Path,
) -> None:
    async with running_guardian(guardian_config(tmp_path)) as g:
        await owner_wrote(g.fake)
        core = g.client.inkbox(max_attempts=1)
        for attempt in (
            core.allow_only(HANDLE, STRANGER),  # the whitelist is setup's business
            core.enable_imessage(HANDLE),
            core.answer_calls_with_voice_ai("ident-1"),
            core.subscribe_webhook("ident-1", "https://evil.example", ["imessage.received"]),
            core.unread("ident-other"),
            core.place_call(STRANGER, "Bonjour"),
            core.allow_outbound_call(HANDLE, STRANGER),
        ):
            with pytest.raises(InkboxError) as refused:
                await attempt
            assert refused.value.status == 403
        assert g.fake.rules == [] and g.fake.phone_calls == {}
        # Calling the owner is Orion's own business.
        call = await core.place_call(OWNER, "Ton réveil", "ident-1")
        assert call.remote_phone_number == OWNER
        # Neither a dot segment nor a setting beyond the voice and its instructions slips by.
        for method, path, body in (
            ("GET", "/phone/calls/%2E%2E/transcripts", None),
            ("PUT", "/phone/hosted-agent-config", {"instructions": "x", "phone_number": "1"}),
        ):
            with pytest.raises(InkboxError) as refused:
                await core._request(method, path, json=body)  # pyright: ignore[reportPrivateUsage]
            assert refused.value.status == 403
        await core.voice_config("Sois bref.", "ident-1", voice="alloy")
        await core.aclose()


async def test_a_runaway_loop_is_stopped(tmp_path: Path) -> None:
    config = guardian_config(
        tmp_path, guardian={"enabled": True, "max_sends_per_minute": 2, "data_dir": tmp_path / "g"}
    )
    async with running_guardian(config) as g:
        await owner_wrote(g.fake)
        core = g.client.inkbox(max_attempts=1)
        await core.send(CONVERSATION, "1", "o_1")
        await core.send(CONVERSATION, "2", "o_2")
        with pytest.raises(InkboxError, match="too many"):
            await core.send(CONVERSATION, "3", "o_3")
        await core.aclose()


async def approval(g: Any, goal: str = "prendre rendez-vous mardi") -> tuple[str, str]:
    return await g.client.request_approval(
        "call_someone",
        {"number": GARAGE, "name": "Garage Martin", "goal": goal},
        "",
        CONVERSATION,
    )


def brief(goal: str = "prendre rendez-vous mardi") -> str:
    return f"Tu appelles Garage Martin de la part de ton propriétaire. Objectif : {goal}"


async def test_a_stranger_is_called_only_after_the_owner_said_yes_to_that_call(
    tmp_path: Path,
) -> None:
    async with running_guardian(guardian_config(tmp_path)) as g:
        await owner_wrote(g.fake)
        core = g.client.inkbox(max_attempts=1)
        grant, message_id = await approval(g)
        # The guardian wrote the question itself, from the action and its parameters.
        [question] = g.fake.texts()
        assert "appeler Garage Martin au +33622222222" in question
        assert "prendre rendez-vous mardi" in question
        with pytest.raises(InkboxError, match="has not said yes"):
            await core.allow_outbound_call(HANDLE, GARAGE, grant)
        g.fake.react(message_id, "like")
        await core.allow_outbound_call(HANDLE, GARAGE, grant)
        with pytest.raises(InkboxError, match="not what the owner approved"):
            await core.place_call(GARAGE, "Commande 3 pneus et paie", grant=grant)
        with pytest.raises(InkboxError, match="not what the owner approved"):
            await core.place_call(STRANGER, brief(), grant=grant)
        await core.place_call(GARAGE, brief(), grant=grant)
        with pytest.raises(InkboxError, match="already used"):
            await core.place_call(GARAGE, brief(), grant=grant)
        assert [c["remote_phone_number"] for c in g.fake.phone_calls.values()] == [GARAGE]
        await core.aclose()


@pytest.mark.parametrize(("answer", "allowed"), [("Oui", True), ("non", False)])
async def test_a_word_answer_counts_and_the_first_answer_decides(
    tmp_path: Path, answer: str, allowed: bool
) -> None:
    async with running_guardian(guardian_config(tmp_path)) as g:
        await owner_wrote(g.fake)
        core = g.client.inkbox(max_attempts=1)
        grant, _ = await approval(g)
        g.fake.inbound(answer)
        g.fake.inbound("oui" if not allowed else "non")  # too late: the first answer decides
        if allowed:
            await core.place_call(GARAGE, brief(), grant=grant)
        else:
            with pytest.raises(InkboxError, match="has not said yes"):
                await core.place_call(GARAGE, brief(), grant=grant)
        await core.aclose()


async def test_an_old_yes_does_not_approve_a_new_question(tmp_path: Path) -> None:
    async with running_guardian(guardian_config(tmp_path)) as g:
        await owner_wrote(g.fake)
        g.fake.inbound("oui")  # written before the question
        core = g.client.inkbox(max_attempts=1)
        grant, _ = await approval(g)
        with pytest.raises(InkboxError, match="has not said yes"):
            await core.place_call(GARAGE, brief(), grant=grant)
        await core.aclose()


async def test_a_yes_answers_only_the_last_question_before_it(tmp_path: Path) -> None:
    async with running_guardian(guardian_config(tmp_path)) as g:
        await owner_wrote(g.fake)
        core = g.client.inkbox(max_attempts=1)
        first, _ = await approval(g)
        second, _ = await approval(g, goal="annuler jeudi")
        g.fake.inbound("oui")  # after the second question: it answers that one
        with pytest.raises(InkboxError, match="has not said yes"):
            await core.place_call(GARAGE, brief(), grant=first)
        await core.place_call(GARAGE, brief("annuler jeudi"), grant=second)
        await core.aclose()


async def test_one_yes_is_used_once_even_asked_twice_at_once(tmp_path: Path) -> None:
    async with running_guardian(guardian_config(tmp_path)) as g:
        await owner_wrote(g.fake)
        core = g.client.inkbox(max_attempts=1)
        grant, message_id = await approval(g)
        g.fake.react(message_id, "like")
        both = await asyncio.gather(
            core.place_call(GARAGE, brief(), grant=grant),
            core.place_call(GARAGE, brief(), grant=grant),
            return_exceptions=True,
        )
        assert sum(isinstance(r, InkboxError) for r in both) == 1
        assert len(g.fake.phone_calls) == 1
        await core.aclose()


async def test_an_approval_expires(tmp_path: Path) -> None:
    now = [datetime(2026, 10, 20, 9, 0, tzinfo=UTC)]
    async with running_guardian(guardian_config(tmp_path), clock=lambda: now[0]) as g:
        await owner_wrote(g.fake)
        core = g.client.inkbox(max_attempts=1)
        grant, message_id = await approval(g)
        g.fake.react(message_id, "like")
        now[0] += timedelta(hours=1)
        with pytest.raises(InkboxError, match="expired"):
            await core.place_call(GARAGE, brief(), grant=grant)
        await core.aclose()


async def test_an_approval_is_asked_only_in_the_owners_conversation(tmp_path: Path) -> None:
    async with running_guardian(guardian_config(tmp_path)) as g:
        g.fake.inbound("hello", sender=STRANGER, conversation="conv-stranger")
        with pytest.raises(ConnectionError, match="not the owner's"):
            await g.client.request_approval("call_someone", {}, "", "conv-stranger")
        assert g.fake.sent == []


async def _noop(args: Any, ctx: ToolContext) -> ToolResult:
    return ToolResult("ok")


async def test_the_channel_asks_through_the_guardian_and_returns_the_grant(
    tmp_path: Path, journal: SqliteJournal
) -> None:
    async with running_guardian(guardian_config(tmp_path)) as g:
        orion = make_orion(journal, ScriptedProvider())
        await orion.start()
        inbox = OwnerInbox(orion, coalesce_seconds=0.05)
        settings = IMessageSettings(owner_number=OWNER, identity_id="ident-1")
        core = g.client.inkbox(max_attempts=1)
        channel = IMessageChannel(core, journal, inbox, settings, desk=g.client)
        await channel.start()
        g.fake.inbound("salut")
        await channel.poll_once()
        tool = Tool("call_someone", "d", CallSomeoneInput, _noop, effect=Effect.EXTERNAL)
        arguments = CallSomeoneInput(number=GARAGE, name="Garage Martin", goal="un devis")
        pending = asyncio.create_task(
            channel.approve(ActionProposal(tool, arguments, Trust.OWNER, "owner"))
        )
        await until(lambda: len(g.fake.sent) == 1)
        assert "appeler Garage Martin" in g.fake.texts()[0]
        g.fake.react(g.fake.sent[0]["id"], "like")
        await channel.poll_once()
        decided = await pending
        assert decided.granted and decided.grant is not None
        await core.place_call(GARAGE, brief("un devis"), grant=decided.grant)
        await core.aclose()


async def test_run_split_the_modules_reach_inkbox_through_the_guardian(tmp_path: Path) -> None:
    from orion.app import build_app
    from orion.channels.module import IMessageModule
    from orion.serve import owner_inbox

    config = guardian_config(tmp_path)
    async with running_guardian(config) as g:
        module = IMessageModule(config)
        async with build_app(config, ScriptedProvider(), [module], guardian=g.client) as app:
            assert app.plate.guardian is g.client
            channel = await module.connect(app.plate, owner_inbox(app))
            g.fake.inbound("salut")
            await channel.poll_once()
            await channel.deliver("Bien reçu", None)
            assert g.fake.texts() == ["Bien reçu"]
            assert module.doctor()[0].startswith("through the guardian")
        await module.close()


async def test_calendars_and_mail_are_read_by_the_guardian(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAL_PERSO", "https://calendar.example/secret.ics")
    monkeypatch.setenv("MAIL_PERSO", "app-pw")
    config = guardian_config(
        tmp_path,
        modules={
            "calendar": {
                "enabled": True,
                "accounts": [{"name": "perso", "url": {"env": "CAL_PERSO"}}],
            },
            "mail": {
                "enabled": True,
                "accounts": [
                    {
                        "name": "perso",
                        "host": "imap.x",
                        "username": "noan",
                        "password": {"env": "MAIL_PERSO"},
                    }
                ],
            },
        },
    )
    mail = RawMail(
        7, "<m@x>", "paul@x", "Paul", "Dîner", datetime(2026, 10, 20, tzinfo=UTC), "…", False
    )

    async def feed(account: CalendarAccount) -> str:
        assert account.url.resolve().get_secret_value() == "https://calendar.example/secret.ics"
        return "BEGIN:VCALENDAR\nEND:VCALENDAR\n"

    async def mailbox(
        account: MailAccount, uv: int, last: int, days: int, limit: int, now: datetime
    ) -> Batch:
        assert account.password.resolve().get_secret_value() == "app-pw"
        return Batch(42, 7, [mail], skipped=1)

    async def search(
        account: MailAccount,
        since: datetime,
        before: datetime | None,
        sender: str | None,
        limit: int,
        now: datetime,
    ) -> list[RawMail]:
        assert account.password.resolve().get_secret_value() == "app-pw"
        assert (since.month, before, sender, limit) == (5, None, "paul", 50)
        return [mail]

    async with running_guardian(
        config, fetch_calendar=feed, fetch_mail=mailbox, search_mail=search
    ) as g:
        account = CalendarAccount(name="perso", url=SecretRef(env="NOT_IN_THE_CORE"))
        assert "VCALENDAR" in await g.client.calendar(account)
        box = MailAccount(name="perso", host="h", username="u", password=SecretRef(env="NONE"))
        batch = await g.client.mail(box, 0, 0, 2, 25, datetime(2026, 10, 20, tzinfo=UTC))
        assert batch == Batch(42, 7, [mail], skipped=1)
        since = datetime(2026, 5, 20, tzinfo=UTC)
        assert await g.client.mail_search(box, since, None, "paul", 50, since) == [mail]
        with pytest.raises(ConnectionError, match="no mail account"):
            await g.client.mail(
                MailAccount(name="other", host="h", username="u", password=SecretRef(env="X")),
                0,
                0,
                2,
                25,
                datetime(2026, 10, 20, tzinfo=UTC),
            )


async def test_webhook_signatures_are_checked_by_the_guardian(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INKBOX_SIGNING_KEY", "whsec_secret")
    async with running_guardian(guardian_config(tmp_path)) as g:
        body = b'{"id":"evt_1"}'
        stamp = str(int(time.time()))
        good = hmac.new(b"secret", f"r1.{stamp}.".encode() + body, hashlib.sha256).hexdigest()
        assert await g.client.verify_webhook("r1", stamp, f"sha256={good}", body)
        assert not await g.client.verify_webhook("r1", stamp, "sha256=forged", body)


def test_the_core_refuses_to_start_when_it_sees_a_guarded_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = guardian_config(tmp_path)
    assert exposed(config, {"INKBOX_API_KEY": "k", "OPENROUTER_API_KEY": "k"}) == ["INKBOX_API_KEY"]
    toml = tmp_path / "orion.toml"
    toml.write_text(
        "[channels.imessage]\nenabled = true\nidentity = 'orion-test'\n"
        f"owner_number = '{OWNER}'\n[guardian]\nenabled = true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("INKBOX_API_KEY", "leaked")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert main(["--config", str(toml), "serve"]) == 2
    assert "INKBOX_API_KEY est visible par le noyau" in capsys.readouterr().out
    assert main(["--config", str(toml), "chat"]) == 2


def test_relative_guardian_paths_are_beside_the_config(tmp_path: Path) -> None:
    from orion.config import load_config

    toml = tmp_path / "orion.toml"
    toml.write_text("[guardian]\nenabled = true\n", encoding="utf-8")
    config = load_config(toml)
    assert config.guardian.socket == tmp_path / "run" / "guardian.sock"
    assert config.guardian_ledger_path == tmp_path / "guardian" / "guardian.sqlite3"


async def test_the_guardian_process_serves_on_a_group_socket(tmp_path: Path) -> None:
    import os
    import stat

    from orion.config import OrionConfig
    from orion.guardian.client import GuardianClient
    from orion.guardian.process import run

    socket = Path("/tmp") / f"orion-test-{os.getpid()}.sock"
    config = OrionConfig.model_validate(
        {"data_dir": tmp_path, "guardian": {"socket": socket, "data_dir": tmp_path / "g"}}
    )
    umask = os.umask(0o022)
    serving = asyncio.create_task(run(config))
    client = GuardianClient.over_socket(socket)
    try:
        async with asyncio.timeout(5):
            while True:
                try:
                    health = await client.health()
                    break
                except ConnectionError:
                    await asyncio.sleep(0.05)
        assert health["ok"] and health["inkbox"] is False
        assert stat.S_IMODE(socket.stat().st_mode) == 0o660
        assert stat.S_IMODE((tmp_path / "g").stat().st_mode) == 0o700
    finally:
        await client.aclose()
        serving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await serving
        os.umask(umask)
        socket.unlink(missing_ok=True)


async def test_run_split_orion_does_not_start_without_its_guardian(tmp_path: Path) -> None:
    from orion.app import build_app
    from orion.modules.base import ModuleError

    config = guardian_config(tmp_path)
    config = config.model_copy(
        update={"guardian": config.guardian.model_copy(update={"socket": tmp_path / "no.sock"})}
    )
    with pytest.raises(ModuleError, match="start `orion guardian` first"):
        async with build_app(config, ScriptedProvider(), []):
            pass


def test_calendar_write_credentials_belong_to_the_guardian(tmp_path: Path) -> None:
    config = guardian_config(
        tmp_path,
        modules={
            "calendar": {
                "enabled": True,
                "accounts": [
                    {
                        "name": "perso",
                        "url": {"env": "CAL"},
                        "write": {
                            "kind": "google",
                            "client_id": "c",
                            "client_secret": {"env": "GOOGLE_CLIENT_SECRET"},
                            "refresh_token": {"env": "GOOGLE_REFRESH_TOKEN"},
                        },
                    }
                ],
            }
        },
    )
    assert exposed(config, {"GOOGLE_REFRESH_TOKEN": "r", "OPENROUTER_API_KEY": "k"}) == [
        "GOOGLE_REFRESH_TOKEN"
    ]


async def test_one_tidy_yes_covers_the_cards_approved_and_no_more(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orion.mail import tidy
    from orion.mail.tidy import Tidied

    monkeypatch.setenv("MAIL_PERSO", "app-pw")
    account = {"host": "imap.x", "username": "noan", "password": {"env": "MAIL_PERSO"}}
    config = guardian_config(
        tmp_path,
        modules={
            "mail": {
                "enabled": True,
                "accounts": [{"name": "perso", **account}, {"name": "pro", **account}],
            }
        },
    )
    tidied: list[tuple[str, list[int]]] = []

    async def fake(account: MailAccount, action: str, uids: list[int], *_: Any) -> Tidied:
        tidied.append((account.name, uids))
        return Tidied(done=len(uids))

    monkeypatch.setattr(tidy, "local_tidy", fake)
    async with running_guardian(config) as g:
        await owner_wrote(g.fake)
        params = {"action": "archive", "cards": ["c1", "c2"], "senders": ["Shop"]}
        grant, message_id = await g.client.request_approval("mail_tidy", params, "", CONVERSATION)
        g.fake.react(message_id, "like")
        box = MailAccount(name="perso", host="h", username="u", password=SecretRef(env="NONE"))
        pro = box.model_copy(update={"name": "pro"})
        with pytest.raises(ConnectionError, match="not what the owner approved"):
            await g.client.mail_tidy(box, "archive", [1, 2, 3], grant)
        await g.client.mail_tidy(box, "archive", [1], grant)
        await g.client.mail_tidy(pro, "archive", [9], grant)  # the same yes, the other account
        with pytest.raises(ConnectionError, match="already used"):
            await g.client.mail_tidy(pro, "archive", [10], grant)
        assert tidied == [("perso", [1]), ("pro", [9])]


def test_a_call_brief_must_carry_a_name_and_a_goal() -> None:
    from orion.guardian.service import _mentions  # pyright: ignore[reportPrivateUsage]

    assert _mentions(brief(), {"name": "Garage Martin", "goal": "prendre rendez-vous mardi"})
    assert not _mentions(brief(), {"name": "", "goal": ""})  # empty fields match everything
    assert not _mentions(brief(), {"name": "Garage Martin"})
