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
    send = {"action": "send", "page": "https://www.doctolib.fr/rdv"}
    assert within("browser", sites, send)
    assert not within("browser", sites, {**send, "page": "https://doctolib.fr.evil.com/"})
    assert not within("browser", sites, {"action": "send", "page": ""})


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


async def test_a_session_is_lent_when_orion_asks(tmp_path: Path) -> None:
    """D115: the guardian lends a session to Orion's browser when it asks; each loan is
    recorded."""
    config = writable_config(tmp_path)
    login = {"action": "login", "session": "doctolib", "login": "", "url": ""}
    async with running_guardian(config) as g:
        with pytest.raises(SessionError, match="lowercase"):
            await add_session(config, g.service.ledger, "Doctolib!", STATE, [])
        assert await add_session(config, g.service.ledger, "doctolib", STATE, []) == ["doctolib.fr"]
        assert (config.guardian.data_dir / "sessions" / "doctolib.json").stat().st_mode & 0o077 == 0
        with pytest.raises(GuardianUnavailableError, match="no session"):
            await g.client.lend_session("banque", {**login, "session": "banque"}, None)
        lent = await g.client.lend_session("doctolib", login, None)
        assert lent["state"]["cookies"][0]["value"] == "s3cret"
        assert lent["domains"] == ["doctolib.fr"]
        await g.client.lend_session("doctolib", login, None)
        assert [e.kind for e in await g.journal.read()].count(SESSION_LENT) == 2


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
