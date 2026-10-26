"""Orion's app run split (D125, D28): the guardian keeps the passkeys and the approvals."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from orion.channels.inbox import OwnerInbox
from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.guardian.app import AppEdge
from orion.guardian.federation import ADMIN_HEADER
from orion.guardian.policy import ActionProposal
from orion.guardian.service import GuardianService
from orion.mobile import channel as mobile_channel
from orion.mobile.channel import MobileChannel
from orion.mobile.module import GuardianSessions
from orion.mobile.server import COOKIE, Gate, core_app
from orion.tools.base import Tool
from tests.fake_guardian import guardian_config, running_guardian
from tests.fake_passkey import FakePasskey
from tests.fakes import ScriptedProvider, reply
from tests.test_imessage import make_orion, until
from tests.test_telegram import nothing

URL = "https://orion.example"


class Mail(BaseModel):
    to: str


def anything(_params: dict[str, Any]) -> bool:
    return True


def edge_of(service: GuardianService) -> httpx.AsyncClient:
    name, app, _host, _port = service.listeners[-1]
    assert name == "app"
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=URL, headers={"origin": URL}
    )


async def test_split_the_guardian_holds_what_makes_a_yes(
    journal: SqliteJournal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mobile_channel, "APPROVAL_POLL_SECONDS", 0.02)
    config = guardian_config(
        tmp_path, modules={"mobile": {"enabled": True, "public_url": URL, "notifications": False}}
    )
    async with running_guardian(config, prepare=lambda s: AppEdge(s).install()) as guardian:
        service = guardian.service
        edge = service.app_edge
        assert edge is not None
        provider = ScriptedProvider(reply("Bien reçu."))
        orion = make_orion(journal, provider)
        await orion.start()
        inbox = OwnerInbox(orion, coalesce_seconds=0.05)
        channel = MobileChannel(
            journal,
            inbox,
            None,
            tmp_path / "m",
            approval_timeout_seconds=5,
            guardian=guardian.client,
        )
        await channel.start()
        worker = asyncio.create_task(inbox.run())
        core = httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=core_app(channel, Gate(URL, GuardianSessions(guardian.client)))
            ),
            base_url=URL,
            headers={"origin": URL},
        )
        try:
            assert not await channel.available()
            # A pairing code only for the guardian's own user, never for the core.
            refused = await guardian.client._http.post(  # pyright: ignore[reportPrivateUsage]
                "/v1/app/pair-code", json={"device": "iphone"}
            )
            assert refused.status_code == 403
            made = await guardian.client.post(
                "/v1/app/pair-code", {"device": "iphone"}, {ADMIN_HEADER: edge.admin_token}
            )
            phone = FakePasskey(URL, "orion.example")
            async with edge_of(service) as app:
                started = (await app.post("/app/pair/options", json={"code": made["code"]})).json()
                paired = await app.post(
                    "/app/pair",
                    json={
                        "ceremony": started["ceremony"],
                        "credential": phone.create(json.dumps(started["options"])),
                    },
                )
                assert paired.status_code == 200
                cookie = app.cookies[COOKIE]
                assert await channel.available()

                # The conversation is the core's, with the session the guardian signed.
                assert (await core.get("/app/api/messages")).status_code == 401
                core.cookies.set(COOKIE, cookie)
                sent = await core.post("/app/api/send", json={"text": "salut"})
                assert sent.status_code == 200
                await until(lambda: any(s.who == "orion" for s in channel.state.said))
                core.cookies.set(COOKIE, cookie[:-2] + "xx")
                assert (await core.get("/app/api/messages")).status_code == 401
                core.cookies.set(COOKIE, cookie)

                tool = Tool(name="send_mail", description="s", input_model=Mail, handler=nothing)
                proposal = ActionProposal(tool, Mail(to="agence@immo.fr"), Trust.OWNER, "o")
                asking = asyncio.create_task(channel.approve(proposal))
                await until(lambda: bool(edge.state.questions))
                [pending] = (await app.get("/app/approvals")).json()["approvals"]
                assert pending["tool"] == "send_mail" and "agence@immo.fr" in pending["question"]
                grant = pending["id"]
                assert await service.use_grant(grant, "send_mail", anything, False) == (
                    "the owner has not said yes"
                )

                # Signed for « no », sent as « yes »: refused by the guardian.
                opened = (
                    await app.post(f"/app/approvals/{grant}/options", json={"answer": "no"})
                ).json()
                forged = await app.post(
                    f"/app/approvals/{grant}",
                    json={
                        "answer": "yes",
                        "ceremony": opened["ceremony"],
                        "credential": phone.sign(json.dumps(opened["options"])),
                    },
                )
                assert forged.status_code == 403 and not asking.done()

                opened = (
                    await app.post(f"/app/approvals/{grant}/options", json={"answer": "yes"})
                ).json()
                yes = await app.post(
                    f"/app/approvals/{grant}",
                    json={
                        "answer": "yes",
                        "ceremony": opened["ceremony"],
                        "credential": phone.sign(json.dumps(opened["options"])),
                    },
                )
                assert yes.status_code == 200
                approval = await asking
                assert approval.granted and approval.grant == grant
                assert await service.use_grant(grant, "send_mail", anything, False) is None
                assert (await app.get("/app/approvals")).json()["approvals"] == []

                # No needs no Face ID, and never counts as a yes.
                asking = asyncio.create_task(channel.approve(proposal))
                await until(lambda: len(edge.state.questions) == 2)
                [pending] = (await app.get("/app/approvals")).json()["approvals"]
                assert (await app.post(f"/app/approvals/{pending['id']}/refuse")).status_code == 200
                assert not (await asking).granted
                assert await service.use_grant(pending["id"], "send_mail", anything, False) == (
                    "the owner has not said yes"
                )
        finally:
            worker.cancel()
            await core.aclose()
