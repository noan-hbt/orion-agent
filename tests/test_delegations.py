"""Acting in the world: autonomies bounded by the owner's limits, and logged-in sessions the
guardian lends for one approved task."""

from __future__ import annotations

from pathlib import Path

import pytest

from orion.config import SecretRef
from orion.core.journal import SqliteJournal
from orion.guardian.autonomy import Autonomy
from orion.guardian.client import GuardianUnavailableError
from orion.guardian.ledger import SESSION_LENT
from orion.guardian.limits import Limits, within
from orion.guardian.sessions import SessionError, add_session, cookie_domains
from orion.mail.service import MailAccount
from orion.mail.smtp import Outgoing
from tests.fake_guardian import running_guardian
from tests.fake_inkbox import CONVERSATION
from tests.test_computer import Site, element, only_the_test_site
from tests.test_writes import writable_config

AGENCY = Limits(recipients=["@agence-dreux.fr", "paul@x.fr"])
STATE = {"cookies": [{"name": "sid", "value": "s3cret", "domain": ".doctolib.fr"}], "origins": []}


def test_limits_are_checked_on_every_recipient_and_site() -> None:
    assert within("mail_send", AGENCY, {"to": ["marie@agence-dreux.fr"], "cc": ["paul@x.fr"]})
    assert not within("mail_send", AGENCY, {"to": ["marie@agence-dreux.fr"], "cc": ["a@b.fr"]})
    assert not within("mail_send", AGENCY, {"to": []})
    assert not within("mail_send", None, {"to": ["paul@x.fr"]})  # never mail alone unbounded
    assert within("calendar_add", None, {})
    sites = Limits(domains=["doctolib.fr"])
    assert within("computer_task_send", sites, {"sites": ["www.doctolib.fr"]})
    assert not within("computer_task_send", sites, {"sites": ["doctolib.fr.evil.com"]})
    assert not within("computer_task_send", sites, {"sites": []})


async def test_a_bounded_autonomy_in_the_core(journal: SqliteJournal) -> None:
    autonomy = Autonomy(journal, ["mail_send"])
    with pytest.raises(ValueError, match="recipients"):
        await autonomy.grant("mail_send", "g", None)
    await autonomy.grant("mail_send", "g", None, AGENCY)
    assert await autonomy.allows("mail_send", {"to": ["paul@x.fr"]})
    assert not await autonomy.allows("mail_send", {"to": ["inconnu@y.fr"]})


async def test_the_guardian_mails_alone_only_within_the_limits(tmp_path: Path) -> None:
    sent: list[Outgoing] = []

    async def send(_account: MailAccount, mail: Outgoing, _grant: str | None) -> str:
        sent.append(mail)
        return "<id@x.fr>"

    config = writable_config(tmp_path)
    config.guardian.autonomable = ["mail_send"]
    account = MailAccount(name="perso", host="h", username="u", password=SecretRef(env="N"))
    async with running_guardian(config, send_mail=send) as g:
        g.fake.inbound("salut")
        with pytest.raises(GuardianUnavailableError, match="recipients"):
            await g.client.post("/v1/autonomy/grant", {"action": "mail_send"})
        asked = {"action": "mail_send", "recipients": AGENCY.recipients, "domains": []}
        grant, message_id = await g.client.request_approval(
            "autonomy_grant", asked, "", CONVERSATION
        )
        assert "seulement à @agence-dreux.fr, paul@x.fr" in g.fake.texts()[-1]
        g.fake.react(message_id, "like")
        # The core cannot widen what the owner approved.
        wider = {"recipients": ["@"], "domains": []}
        with pytest.raises(GuardianUnavailableError, match="not what the owner approved"):
            await g.client.post(
                "/v1/autonomy/grant",
                {"action": "mail_send", "limits": wider},
                headers={"X-Orion-Grant": grant},
            )
        await g.client.post(
            "/v1/autonomy/grant",
            {"action": "mail_send", "limits": AGENCY.model_dump()},
            headers={"X-Orion-Grant": grant},
        )
        to_agency = Outgoing(to=["marie@agence-dreux.fr"], subject="Visite", body="Mardi ?")
        assert await g.client.mail_send(account, to_agency, None) == "<id@x.fr>"
        stranger = Outgoing(to=["inconnu@y.fr"], subject="Visite", body="Mardi ?")
        with pytest.raises(GuardianUnavailableError, match="needs the owner's approval"):
            await g.client.mail_send(account, stranger, None)
        assert sent == [to_agency]


async def test_a_session_is_lent_once_for_the_approved_task(tmp_path: Path) -> None:
    config = writable_config(tmp_path)
    config.guardian.autonomable = ["computer_task_send"]
    brief = "Prendre le premier rendez-vous libre chez le Dr Martin, mardi matin."
    task = {"title": "RDV", "brief": brief, "sites": ["doctolib.fr"], "session": "doctolib"}
    async with running_guardian(config) as g:
        g.fake.inbound("salut")
        with pytest.raises(SessionError, match="lowercase"):
            await add_session(config, g.service.ledger, "Doctolib!", STATE, [])
        assert await add_session(config, g.service.ledger, "doctolib", STATE, []) == ["doctolib.fr"]
        assert (config.guardian.data_dir / "sessions" / "doctolib.json").stat().st_mode & 0o077 == 0

        lend = {"brief": brief, "session": "doctolib", "sites": ["doctolib.fr"]}
        with pytest.raises(GuardianUnavailableError, match="needs approval"):
            await g.client.lend_session("doctolib", lend, None)
        grant, _ = await g.client.request_approval("computer_task_send", task, "", CONVERSATION)
        question = g.fake.texts()[-1]
        assert brief in question and "session connectée « doctolib »" in question
        g.fake.inbound("oui")
        other = {**lend, "brief": "Annuler tous mes rendez-vous."}
        with pytest.raises(GuardianUnavailableError, match="not what the owner approved"):
            await g.client.lend_session("doctolib", other, grant)
        state = await g.client.lend_session("doctolib", lend, grant)
        assert state["cookies"][0]["value"] == "s3cret"
        with pytest.raises(GuardianUnavailableError, match="already used"):
            await g.client.lend_session("doctolib", lend, grant)
        assert [e.kind for e in await g.journal.read()].count(SESSION_LENT) == 1


def test_a_session_knows_its_sites() -> None:
    assert cookie_domains(STATE) == ["doctolib.fr"]


async def test_a_sending_task_sends_only_on_its_sites() -> None:
    from orion.computer.browser import Session

    for allowed, posted in ((("annonces.test",), True), (("ailleurs.test",), False)):
        site = Site()
        session = Session(False, only_the_test_site, {"annonces.test": site.handle})
        session.send_hosts = allowed
        async with session as browser:
            await browser.goto("https://annonces.test/")
            await browser.type(await element(browser, "Votre message"), "Bonjour", enter=False)
            await browser.click(await element(browser, "Envoyer"))
        assert any(r.startswith("POST") for r in site.requests) is posted


async def test_the_lent_session_reaches_the_worker_but_not_the_journal(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    import json
    from typing import Any

    from orion.computer.module import computer_tools
    from orion.delegation.claude_code import AgentOutcome
    from orion.delegation.jobs import JobService
    from orion.guardian.client import GuardianClient
    from orion.tools.base import ToolContext

    got: list[dict[str, Any] | None] = []

    class Worker:
        name = "browser-submit"
        allow_bash = False

        async def run(
            self, task: str, workdir: Path, options: dict[str, Any] | None = None
        ) -> AgentOutcome:
            got.append(options)
            return AgentOutcome(True, "Rendez-vous pris mardi 9h.")

    class Lender(GuardianClient):
        async def lend_session(
            self, name: str, params: dict[str, Any], grant: str | None
        ) -> dict[str, Any]:
            assert grant == "g1" and params["session"] == name
            return STATE

    jobs = JobService(journal, Worker(), tmp_path)
    reading = JobService(journal, Worker(), tmp_path)
    tools = {
        t.name: t for t in computer_tools(reading, jobs, Lender.over_socket(tmp_path / "none.sock"))
    }
    send = tools["computer_task_send"]
    args = send.parse(
        json.dumps(
            {
                "title": "RDV",
                "brief": "Prendre rendez-vous mardi",
                "sites": ["doctolib.fr"],
                "session": "doctolib",
            }
        )
    )
    result = await send.handler(args, ToolContext("owner", "c", grant="g1"))
    assert not result.is_error
    await jobs.wait()
    assert got == [{"sites": ["doctolib.fr"], "state": STATE}]
    assert "s3cret" not in str([e.payload for e in await journal.read()])
