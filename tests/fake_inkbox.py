"""An in-memory Inkbox API, served through httpx.MockTransport to the real client."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from orion.channels.inkbox import InkboxClient

OWNER = "+33611111111"
CONVERSATION = "conv-owner"


@dataclass
class FakeInkbox:
    identity: dict[str, Any] | None = field(
        default_factory=lambda: {
            "id": "ident-1",
            "agent_handle": "orion-test",
            "imessage_enabled": True,
        }
    )
    messages: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    sent: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    calls: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    idempotency: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    fail_sends: list[int] = field(default_factory=list[int])
    """Status codes to answer the next sends with, in order."""
    _counter: int = 0

    def client(self, max_attempts: int = 1) -> InkboxClient:
        return InkboxClient(
            "test-key", transport=httpx.MockTransport(self._handle), max_attempts=max_attempts
        )

    def inbound(
        self, text: str, sender: str = OWNER, conversation: str = CONVERSATION
    ) -> dict[str, Any]:
        self._counter += 1
        message: dict[str, Any] = {
            "id": f"in-{self._counter}",
            "conversation_id": conversation,
            "direction": "inbound",
            "remote_number": sender,
            "content": text,
            "service": "imessage",
            "is_read": False,
            "created_at": f"2026-10-19T18:00:{self._counter:02d}+00:00",
        }
        self.messages[message["id"]] = message
        return message

    def react(self, message_id: str, reaction: str, sender: str = OWNER) -> None:
        self.messages[message_id].setdefault("reactions", []).append(
            {
                "id": f"r-{message_id}",
                "direction": "inbound",
                "reaction": reaction,
                "remote_number": sender,
            }
        )

    def texts(self) -> list[str]:
        return [m["text"] for m in self.sent]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        self.calls.append((request.method, path))
        assert request.headers["X-API-Key"] == "test-key"
        body: dict[str, Any] = json.loads(request.content) if request.content else {}
        if path.startswith("/identities/"):
            if self.identity is None:
                return httpx.Response(404, json={"detail": "not found"})
            return httpx.Response(200, json=self.identity)
        if path == "/imessage/triage-number":
            return httpx.Response(
                200, json={"number": "+15550000", "connect_command": "connect @x"}
            )
        if path == "/imessage/messages" and request.method == "POST":
            return self._send(request, body)
        if path == "/imessage/messages":
            unread = [
                m
                for m in self.messages.values()
                if m["direction"] == "inbound" and not m["is_read"]
            ]
            return httpx.Response(200, json=unread)
        if path.startswith("/imessage/messages/"):
            return httpx.Response(200, json=self.messages[path.rsplit("/", 1)[1]])
        if path == "/imessage/mark-read":
            for m in self.messages.values():
                if m["conversation_id"] == body["conversation_id"]:
                    m["is_read"] = True
            return httpx.Response(200, json={"updated": 1})
        if path in ("/imessage/typing", "/imessage/reactions"):
            return httpx.Response(204)
        return httpx.Response(404, json={"detail": f"unknown {path}"})

    def _send(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        key = request.headers["Idempotency-Key"]
        if self.fail_sends:
            return httpx.Response(self.fail_sends.pop(0), json={"detail": "nope"})
        if key in self.idempotency:
            return httpx.Response(200, json={"message": self.idempotency[key]})
        self._counter += 1
        message = {
            "id": f"out-{self._counter}",
            "conversation_id": body["conversation_id"],
            "direction": "outbound",
            "content": body["text"],
            "created_at": "2026-10-19T18:01:00+00:00",
        }
        self.messages[message["id"]] = message
        self.idempotency[key] = message
        self.sent.append({"id": message["id"], "text": body["text"], "key": key})
        return httpx.Response(200, json={"message": message})
