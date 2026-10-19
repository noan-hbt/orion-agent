"""Inkbox API client (iMessage) and webhook verification.

A small async client over the documented REST endpoints (`https://inkbox.ai/api/v1`), rather
than the official SDK, which is synchronous. Only what Orion uses is modelled; unknown fields
in responses are ignored.

Sends carry an `Idempotency-Key`: retrying a send with the same key never produces a second
message, which is what lets the outbox retry safely after a crash.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import random
import time
from typing import Any, Literal

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = structlog.get_logger(__name__)

DEFAULT_BASE_URL = "https://inkbox.ai"
SIGNATURE_TOLERANCE_SECONDS = 300
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

Reaction = Literal["love", "like", "dislike", "laugh", "emphasize", "question", "eyes", "custom"]


class InkboxError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Identity(_Model):
    id: str
    agent_handle: str
    display_name: str | None = None
    imessage_enabled: bool = False


class TriageNumber(_Model):
    number: str
    connect_command: str


class _Subscription(_Model):
    id: str
    signing_key: str | None = None


class MessageReaction(_Model):
    id: str
    direction: Literal["inbound", "outbound"]
    reaction: Reaction
    custom_emoji: str | None = None
    remote_number: str | None = None


class IMessage(_Model):
    id: str
    conversation_id: str
    direction: Literal["inbound", "outbound"]
    remote_number: str | None = None
    sender_number: str | None = None
    content: str | None = None
    service: str = "imessage"
    is_group: bool = False
    is_read: bool = False
    created_at: str
    status: str | None = None
    was_downgraded: bool | None = None
    reactions: list[MessageReaction] | None = None

    @property
    def sender(self) -> str | None:
        return self.sender_number or self.remote_number


class ReactionEvent(_Model):
    id: str
    conversation_id: str
    target_message_id: str
    direction: Literal["inbound", "outbound"]
    reaction: Reaction
    custom_emoji: str | None = None
    remote_number: str | None = None
    created_at: str


class ActionItem(_Model):
    action: str
    details: str | None = None


class Call(_Model):
    id: str
    remote_phone_number: str
    direction: Literal["inbound", "outbound"]
    status: str
    origin: str | None = None
    mode: str | None = None
    reason: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    created_at: str
    duration_seconds: int | None = None
    hangup_reason: str | None = None
    post_call_action_items: list[ActionItem] = Field(default_factory=list[ActionItem])

    @property
    def over(self) -> bool:
        return self.status in ("completed", "failed", "canceled")


class TranscriptEntry(_Model):
    """One turn of a call ("party" is who spoke), or the marker of an abridged middle."""

    party: str | None = None
    text: str | None = None
    ts_ms: int | None = None
    marker: str | None = None
    omitted_turns: int | None = None


class Transcript(_Model):
    entries: list[TranscriptEntry] = Field(default_factory=list[TranscriptEntry])
    abridged: bool = False


class WebhookData(_Model):
    message: IMessage | None = None
    reaction: ReactionEvent | None = None
    call: Call | None = None
    transcript: Transcript | None = None
    outcome: str | None = None
    post_call_action_items: list[ActionItem] = Field(default_factory=list[ActionItem])


class WebhookEvent(_Model):
    id: str = Field(description="Stable per-event id (evt_...), the deduplication key")
    event_type: str
    timestamp: str
    data: WebhookData


def verify_signature(
    secret: str,
    request_id: str,
    timestamp: str,
    signature: str,
    body: bytes,
    now: float | None = None,
) -> bool:
    """Inkbox webhook signature: HMAC-SHA256 over `{request_id}.{timestamp}.{body}` with the
    identity's signing key (`whsec_` prefix stripped), sent as `sha256=<hex>`. Stale or
    future timestamps are rejected to stop replays."""
    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - sent_at) > SIGNATURE_TOLERANCE_SECONDS:
        return False
    key = secret.removeprefix("whsec_").encode()
    expected = hmac.new(key, f"{request_id}.{timestamp}.".encode() + body, hashlib.sha256)
    return hmac.compare_digest(expected.hexdigest(), signature.removeprefix("sha256="))


def parse_webhook(body: bytes) -> WebhookEvent:
    try:
        return WebhookEvent.model_validate_json(body)
    except ValidationError as exc:
        raise InkboxError(f"malformed webhook payload: {exc.error_count()} errors") from exc


class InkboxClient:
    """Async Inkbox client. `identity_id` scopes calls made with an org-wide (admin) key."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 20.0,
        max_attempts: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/api/v1",
            timeout=timeout,
            transport=transport,
            headers={"X-API-Key": api_key, "User-Agent": "orion-agent"},
        )
        self._max_attempts = max_attempts

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- identities ---------------------------------------------------------------------------

    async def identity(self, handle: str) -> Identity | None:
        try:
            return Identity.model_validate(await self._request("GET", f"/identities/{handle}"))
        except InkboxError as exc:
            if exc.status == 404:
                return None
            raise

    async def create_identity(self, handle: str, display_name: str) -> Identity:
        body = {"agent_handle": handle, "display_name": display_name, "imessage_enabled": True}
        return Identity.model_validate(await self._request("POST", "/identities/", json=body))

    async def enable_imessage(self, handle: str) -> Identity:
        data = await self._request(
            "PATCH", f"/identities/{handle}", json={"imessage_enabled": True}
        )
        return Identity.model_validate(data)

    async def triage_number(self) -> TriageNumber:
        return TriageNumber.model_validate(await self._request("GET", "/imessage/triage-number"))

    async def subscribe_webhook(self, identity_id: str, url: str, events: list[str]) -> str | None:
        """Create a webhook subscription. Returns the signing key when Inkbox mints one (only
        on the identity's first subscription; it is never shown again)."""
        data = await self._request(
            "POST",
            "/webhooks/subscriptions",
            json={"url": url, "event_types": events, "agent_identity_id": identity_id},
        )
        return _Subscription.model_validate(data).signing_key

    # -- iMessage -----------------------------------------------------------------------------

    async def send(
        self,
        conversation_id: str,
        text: str,
        idempotency_key: str,
        identity_id: str | None = None,
    ) -> IMessage:
        params = {"agent_identity_id": identity_id} if identity_id else None
        data = await self._request(
            "POST",
            "/imessage/messages",
            json={"conversation_id": conversation_id, "text": text},
            params=params,
            headers={"Idempotency-Key": idempotency_key, "Prefer": "idempotency-replay"},
        )
        return IMessage.model_validate(data.get("message", data))

    async def unread(self, identity_id: str | None = None, limit: int = 50) -> list[IMessage]:
        params: dict[str, Any] = {"is_read": False, "limit": limit}
        if identity_id:
            params["agent_identity_id"] = identity_id
        data = await self._request("GET", "/imessage/messages", params=params)
        return [IMessage.model_validate(m) for m in data]

    async def recent(
        self, conversation_id: str, identity_id: str | None = None, limit: int = 20
    ) -> list[IMessage]:
        """Latest messages of a conversation, newest first (to read tapbacks while polling)."""
        params: dict[str, Any] = {"conversation_id": conversation_id, "limit": limit}
        if identity_id:
            params["agent_identity_id"] = identity_id
        data = await self._request("GET", "/imessage/messages", params=params)
        return [IMessage.model_validate(m) for m in data]

    async def message(self, message_id: str, identity_id: str | None = None) -> IMessage:
        params = {"agent_identity_id": identity_id} if identity_id else None
        data = await self._request("GET", f"/imessage/messages/{message_id}", params=params)
        return IMessage.model_validate(data)

    async def typing(self, conversation_id: str) -> None:
        await self._request("POST", "/imessage/typing", json={"conversation_id": conversation_id})

    async def mark_read(self, conversation_id: str) -> None:
        await self._request(
            "POST", "/imessage/mark-read", json={"conversation_id": conversation_id}
        )

    async def react(self, message_id: str, reaction: Reaction) -> None:
        await self._request(
            "POST", "/imessage/reactions", json={"message_id": message_id, "reaction": reaction}
        )

    async def allow_only(self, handle: str, number: str) -> None:
        """Whitelist mode on iMessage and phone, with only `number` allowed: nobody else can
        reach Orion's line, so nobody else can talk to a voice that knows the owner's life."""
        rules = {"action": "allow", "match_type": "exact_number", "match_target": number}
        for path in (
            f"/imessage/identities/{handle}/contact-rules",
            f"/identities/{handle}/phone-contact-rules",
        ):
            try:
                await self._request("POST", path, json=rules)
            except InkboxError as exc:
                if exc.status != 409:  # already allowed
                    raise
        await self._request(
            "PATCH",
            f"/identities/{handle}",
            json={"imessage_filter_mode": "whitelist", "phone_filter_mode": "whitelist"},
        )

    async def allow_outbound_call(self, handle: str, number: str) -> None:
        """Let Orion's line call `number` (the line otherwise only talks to the owner)."""
        try:
            await self._request(
                "POST",
                f"/identities/{handle}/phone-contact-rules",
                json={
                    "action": "allow",
                    "match_type": "exact_number",
                    "match_target": number,
                    "direction": "outbound",
                },
            )
        except InkboxError as exc:
            if exc.status != 409:
                raise

    # -- phone --------------------------------------------------------------------------------

    async def place_call(
        self,
        to: str,
        reason: str,
        identity_id: str | None = None,
        origination: str = "shared_imessage_number",
        voicemail_message: str | None = None,
    ) -> Call:
        """Place a call driven by Inkbox Voice AI, with `reason` as its task brief."""
        body: dict[str, Any] = {
            "to_number": to,
            "origination": origination,
            "mode": "hosted_agent",
            "reason": reason,
            "hosted_agent_authority_mode": "contact_scoped",
        }
        if identity_id:
            body["agent_identity_id"] = identity_id
        if voicemail_message:
            body["on_voicemail"] = "leave_message"
            body["voicemail_message"] = voicemail_message[:1000]
        return Call.model_validate(await self._request("POST", "/phone/place-call", json=body))

    async def calls(self, identity_id: str | None = None, limit: int = 20) -> list[Call]:
        params: dict[str, Any] = {"limit": limit}
        if identity_id:
            params["agent_identity_id"] = identity_id
        data = await self._request("GET", "/phone/calls", params=params)
        return [Call.model_validate(c) for c in data]

    async def transcript(self, call_id: str) -> list[TranscriptEntry]:
        data = await self._request("GET", f"/phone/calls/{call_id}/transcripts")
        return [TranscriptEntry.model_validate(t) for t in data]

    async def voice_config(
        self, instructions: str, identity_id: str | None = None, voice: str | None = None
    ) -> None:
        """Full replace of the identity's Voice AI steering prompt (and voice)."""
        body: dict[str, Any] = {"instructions": instructions}
        if identity_id:
            body["agent_identity_id"] = identity_id
        if voice:
            body["voice"] = voice
        await self._request("PUT", "/phone/hosted-agent-config", json=body)

    async def answer_calls_with_voice_ai(self, identity_id: str | None = None) -> None:
        body: dict[str, Any] = {"incoming_call_action": "hosted_agent"}
        if identity_id:
            body["agent_identity_id"] = identity_id
        await self._request("PUT", "/phone/incoming-call-action", json=body)

    # -- transport ----------------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        attempt = 0
        while True:
            attempt += 1
            try:
                return await self._once(method, path, json, params, headers)
            except InkboxError as exc:
                if not exc.retryable or attempt >= self._max_attempts:
                    raise
                delay = min(8.0, 0.5 * 2 ** (attempt - 1)) * (0.5 + random.random())
                log.warning("inkbox.retry", path=path, attempt=attempt, status=exc.status)
                await asyncio.sleep(delay)

    async def _once(
        self,
        method: str,
        path: str,
        json: dict[str, Any] | None,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
    ) -> Any:
        try:
            response = await self._client.request(
                method, path, json=json, params=params, headers=headers
            )
        except httpx.TransportError as exc:
            raise InkboxError(f"{method} {path}: {exc!r}", retryable=True) from exc
        if response.status_code >= 400:
            detail = response.text[:300]
            raise InkboxError(
                f"{method} {path} -> {response.status_code}: {detail}",
                status=response.status_code,
                retryable=response.status_code in _RETRYABLE_STATUS,
            )
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()
