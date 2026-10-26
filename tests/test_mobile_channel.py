"""The app as a channel (D125): paired by passkey, the conversation, files, signed approvals."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from orion.adaptation.signals import FEEDBACK_REACTION
from orion.channels.inbox import OwnerInbox
from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.guardian.policy import ActionProposal
from orion.mobile.channel import INBOUND, OUTBOUND, MobileChannel
from orion.mobile.devices import Devices, session_key
from orion.mobile.push import Subscription
from orion.mobile.server import COOKIE, mobile_app
from orion.tools.base import Tool
from tests.fake_passkey import FakePasskey
from tests.fakes import ScriptedProvider, reply
from tests.test_imessage import make_orion, until
from tests.test_telegram import Empty, nothing

URL = "https://orion.example"
APPLE = "https://web.push.apple.com/abc"


class FakePusher:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send(self, subscription: Subscription, message: dict[str, object]) -> None:
        self.sent.append(message)


@dataclass
class World:
    channel: MobileChannel
    http: httpx.AsyncClient
    phone: FakePasskey
    provider: ScriptedProvider
    pusher: FakePusher
    devices: Devices


@pytest.fixture
async def world(journal: SqliteJournal, tmp_path: Path) -> AsyncIterator[World]:
    provider = ScriptedProvider()
    orion = make_orion(journal, provider)
    await orion.start()
    inbox = OwnerInbox(orion, coalesce_seconds=0.05)
    devices = Devices(journal, URL, session_key(tmp_path / "mobile"))
    pusher = FakePusher()
    channel = MobileChannel(
        journal, inbox, devices, tmp_path / "mobile", pusher, approval_timeout_seconds=5.0
    )
    await channel.start()
    worker = asyncio.create_task(inbox.run())
    app = mobile_app(channel, None, secure_cookie=True, session_days=30)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=URL, headers={"origin": URL}
    ) as http:
        yield World(channel, http, FakePasskey(URL, "orion.example"), provider, pusher, devices)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker


async def pair(world: World) -> None:
    code = await world.devices.new_code("iphone")
    started = (await world.http.post("/app/pair/options", json={"code": code})).json()
    options = json.dumps(started["options"])
    done = await world.http.post(
        "/app/pair",
        json={"ceremony": started["ceremony"], "credential": world.phone.create(options)},
    )
    assert done.status_code == 200, done.text
    assert COOKIE in world.http.cookies


async def test_nothing_without_a_paired_passkey(world: World) -> None:
    assert (await world.http.get("/app/api/messages")).status_code == 401
    assert (await world.http.post("/app/login/options")).status_code == 409  # nobody paired
    wrong = await world.http.post("/app/pair/options", json={"code": "AAAAAAAA"})
    assert wrong.status_code == 403
    assert not await world.channel.available()  # no outlet until a phone is paired
    page = await world.http.get("/app/")
    assert (
        page.status_code == 200 and "script-src 'self'" in page.headers["content-security-policy"]
    )


async def test_he_writes_from_the_app_and_orion_answers_there(world: World) -> None:
    await pair(world)
    world.provider.extend(reply("Le 7 h 04, quai 3."))
    sent = await world.http.post("/app/api/send", json={"text": "le train pour Lyon ?"})
    assert sent.status_code == 200
    channel = world.channel

    await until(lambda: any(s.who == "orion" for s in channel.state.said))
    listing = (await world.http.get("/app/api/messages")).json()["messages"]
    assert [(m["who"], m["text"]) for m in listing] == [
        ("owner", "le train pour Lyon ?"),
        ("orion", "Le 7 h 04, quai 3."),
    ]
    later = (await world.http.get(f"/app/api/messages?after={listing[0]['seq']}")).json()
    assert len(later["messages"]) == 1
    events = [e for e in await channel._journal.read() if e.kind == INBOUND]  # pyright: ignore[reportPrivateUsage]
    assert events[0].trust is Trust.OWNER and events[0].provenance.actor == "owner:main"


async def test_a_photo_goes_in_and_a_file_comes_out(world: World, tmp_path: Path) -> None:
    await pair(world)
    world.provider.extend(reply("Joli."))
    jpeg = b"\xff\xd8\xff" + b"0" * 50
    sent = await world.http.post(
        "/app/api/send",
        json={
            "text": "",
            "files": [
                {"name": "IMG.jpg", "type": "image/jpeg", "data": base64.b64encode(jpeg).decode()}
            ],
        },
    )
    message = sent.json()["message"]
    assert message["files"][0]["name"].endswith("IMG.jpg")
    got = await world.http.get(f"/app/api/files/{message['ref']}/0")
    assert got.content == jpeg

    pdf = tmp_path / "devis.pdf"
    pdf.write_bytes(b"%PDF-1.4 devis")
    await world.channel.deliver_file(pdf, "Le devis", None)
    out = world.channel.state.said[-1]
    assert out.who == "orion" and out.text == "Le devis"
    back = await world.http.get(f"/app/api/files/{out.ref}/0")
    assert back.content == b"%PDF-1.4 devis"
    assert (await world.http.get(f"/app/api/files/{out.ref}/9")).status_code == 404


async def test_an_answer_counts_only_signed_for_that_question_and_that_answer(
    world: World,
) -> None:
    await pair(world)
    await world.channel.subscribe("iphone", APPLE, "p256dh", "auth")
    tool = Tool(name="send_mail", description="s", input_model=Empty, handler=nothing)
    asking = asyncio.create_task(
        world.channel.approve(ActionProposal(tool, Empty(), Trust.OWNER, "o"))
    )
    await until(lambda: bool(world.channel.asked))
    [pending] = (await world.http.get("/app/api/approvals")).json()["approvals"]
    assert "send_mail" in pending["question"]
    assert world.pusher.sent[-1]["title"] == "Orion demande ton accord"

    # Signed for « no », sent as « yes »: refused.
    opened = (
        await world.http.post(f"/app/api/approvals/{pending['id']}/options", json={"answer": "no"})
    ).json()
    signed = world.phone.sign(json.dumps(opened["options"]))
    forged = await world.http.post(
        f"/app/api/approvals/{pending['id']}",
        json={"answer": "yes", "ceremony": opened["ceremony"], "credential": signed},
    )
    assert forged.status_code == 403 and not asking.done()

    opened = (
        await world.http.post(f"/app/api/approvals/{pending['id']}/options", json={"answer": "yes"})
    ).json()
    answered = await world.http.post(
        f"/app/api/approvals/{pending['id']}",
        json={
            "answer": "yes",
            "ceremony": opened["ceremony"],
            "credential": world.phone.sign(json.dumps(opened["options"])),
        },
    )
    assert answered.status_code == 200
    approval = await asking
    assert approval.granted and approval.reached


async def test_cross_site_and_reactions(world: World, journal: SqliteJournal) -> None:
    await pair(world)
    elsewhere = await world.http.post(
        "/app/api/send", json={"text": "x"}, headers={"origin": "https://evil.example"}
    )
    assert elsewhere.status_code == 403
    await world.channel.deliver("Voilà le résumé.", None)
    ref = world.channel.state.said[-1].ref
    assert (
        await world.http.post("/app/api/react", json={"ref": ref, "emoji": "👍"})
    ).status_code == 200
    reactions = [e for e in await journal.read() if e.kind == FEEDBACK_REACTION]
    assert reactions[0].payload["reaction"] == "like"
    assert reactions[0].payload["remote_id"] == ref


async def test_a_message_never_answered_comes_back_after_a_restart(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    devices = Devices(journal, URL, session_key(tmp_path / "mobile"))
    crashed = MobileChannel(
        journal, OwnerInbox(make_orion(journal, ScriptedProvider())), devices, tmp_path / "m"
    )
    await crashed.start()
    await crashed.receive("iphone", "tu es là ?")

    provider = ScriptedProvider(reply("Oui, de retour."))
    orion = make_orion(journal, provider)
    await orion.start()
    inbox = OwnerInbox(orion, coalesce_seconds=0.05)
    channel = MobileChannel(journal, inbox, devices, tmp_path / "m")
    await channel.start()
    worker = asyncio.create_task(inbox.run())
    await until(lambda: any(s.who == "orion" for s in channel.state.said))
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    sent = [e for e in await journal.read() if e.kind == OUTBOUND]
    assert [e.payload["text"] for e in sent] == ["Oui, de retour."]


async def test_a_notification_when_the_app_is_closed_not_when_it_is_open(world: World) -> None:
    await pair(world)
    await world.channel.subscribe("iphone", APPLE, "p256dh", "auth")
    await world.channel.deliver("Ton colis arrive demain.", None)
    assert world.pusher.sent[-1]["body"] == "Ton colis arrive demain."
    async with world.channel.hub.listen() as live:
        await world.channel.deliver("Et il pleut.", None)
        assert (await live.get())["message"]["text"] == "Et il pleut."
    assert len(world.pusher.sent) == 1  # it was open: no notification
    with pytest.raises(ValueError):
        await world.channel.subscribe("iphone", "https://10.0.0.1/x", "k", "a")
