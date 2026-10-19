from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest
from starlette.applications import Starlette

from orion.channels.inkbox import InkboxError, WebhookEvent, parse_webhook, verify_signature
from orion.channels.webhook import webhook_app
from orion.core.journal import SqliteJournal
from orion.guardian.outbox import DELIVERED, FAILED, QUEUED, Outbox, SendError
from tests.fake_inkbox import CONVERSATION, FakeInkbox

SECRET = "whsec_topsecret"
NOW = 1_800_000_000


def sign(body: bytes, request_id: str = "req-1", timestamp: int = NOW) -> dict[str, str]:
    digest = hmac.new(
        b"topsecret", f"{request_id}.{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return {
        "x-inkbox-request-id": request_id,
        "x-inkbox-timestamp": str(timestamp),
        "x-inkbox-signature": f"sha256={digest}",
    }


def webhook_body(text: str = "salut") -> bytes:
    return json.dumps(
        {
            "id": "evt_1",
            "event_type": "imessage.received",
            "timestamp": "2026-10-19T18:00:00Z",
            "data": {
                "message": {
                    "id": "msg-1",
                    "conversation_id": CONVERSATION,
                    "direction": "inbound",
                    "remote_number": "+33611111111",
                    "content": text,
                    "created_at": "2026-10-19T18:00:00Z",
                    "unknown_future_field": 1,
                },
                "reaction": None,
                "contacts": [],
                "agent_identities": [],
            },
        }
    ).encode()


def test_signature_accepts_valid_and_rejects_tampered_or_stale() -> None:
    body = webhook_body()
    h = sign(body)
    args = (h["x-inkbox-request-id"], h["x-inkbox-timestamp"], h["x-inkbox-signature"])
    assert verify_signature(SECRET, *args, body, now=NOW)
    assert not verify_signature(SECRET, *args, body + b" ", now=NOW)
    assert not verify_signature("whsec_other", *args, body, now=NOW)
    assert not verify_signature(SECRET, *args, body, now=NOW + 301)
    assert not verify_signature(SECRET, args[0], "not-a-number", args[2], body, now=NOW)


def test_parse_webhook_ignores_unknown_fields_and_rejects_garbage() -> None:
    event = parse_webhook(webhook_body("coucou"))
    assert event.data.message is not None and event.data.message.content == "coucou"
    with pytest.raises(InkboxError):
        parse_webhook(b'{"nope": 1}')


def asgi_client(app: Starlette) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://orion")


async def test_webhook_endpoint_fails_closed() -> None:
    received: list[WebhookEvent] = []

    async def handle(event: WebhookEvent) -> None:
        received.append(event)

    body = webhook_body()
    async with asgi_client(webhook_app("/hook", None, handle, clock=lambda: NOW)) as unsigned:
        response = await unsigned.post("/hook", content=body, headers=sign(body))
        assert response.status_code == 503

    async with asgi_client(webhook_app("/hook", SECRET, handle, clock=lambda: NOW)) as client:
        response = await client.post("/hook", content=body, headers=sign(body + b"x"))
        assert response.status_code == 401
        garbage = b"{}"
        response = await client.post("/hook", content=garbage, headers=sign(garbage))
        assert response.status_code == 400
        assert received == []
        response = await client.post("/hook", content=body, headers=sign(body))
        assert response.status_code == 200
    assert [e.id for e in received] == ["evt_1"]


async def test_client_sends_with_idempotency_key_and_identity() -> None:
    fake = FakeInkbox()
    client = fake.client()
    first = await client.send(CONVERSATION, "yo", "key-1", identity_id="ident-1")
    again = await client.send(CONVERSATION, "yo", "key-1", identity_id="ident-1")
    assert first.id == again.id and fake.texts() == ["yo"]
    fake.identity = None
    assert await client.identity("missing") is None
    await client.aclose()


async def test_client_retries_transient_errors() -> None:
    fake = FakeInkbox(fail_sends=[503])
    client = fake.client(max_attempts=2)
    sent = await client.send(CONVERSATION, "yo", "key-1")
    assert sent.content == "yo"
    fake.fail_sends = [400]
    with pytest.raises(InkboxError) as error:
        await client.send(CONVERSATION, "non", "key-2")
    assert error.value.status == 400 and not error.value.retryable
    await client.aclose()


class FlakyTransport:
    def __init__(self, failures: list[SendError]) -> None:
        self.failures = failures
        self.keys: list[str] = []

    async def transmit(self, to: str, text: str, idempotency_key: str) -> str:
        self.keys.append(idempotency_key)
        if self.failures:
            raise self.failures.pop(0)
        return f"remote-{len(self.keys)}"


async def test_outbox_journals_and_resumes_with_the_same_key(journal: SqliteJournal) -> None:
    transport = FlakyTransport([SendError("network down", retryable=True)])
    outbox = Outbox(journal, {"imessage": transport})
    with pytest.raises(SendError):
        await outbox.send("imessage", CONVERSATION, "hello")
    assert len(outbox.projection.pending) == 1

    # A fresh process (same journal) finishes the send with the original key.
    restarted = Outbox(journal, {"imessage": transport})
    assert await restarted.resume() == 1
    assert transport.keys[0] == transport.keys[1]
    kinds = [e.kind for e in await journal.read()]
    assert kinds == [QUEUED, DELIVERED]
    assert await Outbox(journal, {"imessage": transport}).resume() == 0


async def test_outbox_does_not_retry_a_definite_refusal(journal: SqliteJournal) -> None:
    transport = FlakyTransport([SendError("blocked", retryable=False)])
    outbox = Outbox(journal, {"imessage": transport})
    with pytest.raises(SendError):
        await outbox.send("imessage", CONVERSATION, "hello")
    assert [e.kind for e in await journal.read()] == [QUEUED, FAILED]
    assert await outbox.resume() == 0


async def test_the_line_is_reserved_for_the_owner() -> None:
    fake = FakeInkbox()
    client = fake.client()
    await client.allow_only("orion-test", "+33611111111")
    paths = [path for path, _ in fake.rules]
    assert paths == [
        "/imessage/identities/orion-test/contact-rules",
        "/phone/identities/orion-test/phone-contact-rules",
        "/identities/orion-test",
    ]
    assert fake.rules[-1][1] == {
        "imessage_filter_mode": "whitelist",
        "phone_filter_mode": "whitelist",
    }
    await client.aclose()
