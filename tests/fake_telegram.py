"""A Telegram Bot API that answers like the real one, in memory."""

from __future__ import annotations

import json
from typing import Any

import httpx

from orion.channels.telegram import TelegramClient

TOKEN = "123:SECRET"
OWNER = 4242
STRANGER = 777


class FakeTelegram:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.files: dict[str, bytes] = {}
        self._next_update = 1
        self._next_message = 100

    def client(self) -> TelegramClient:
        return TelegramClient(TOKEN, "https://tg.test", transport=httpx.MockTransport(self.handle))

    def sent(self, method: str = "sendMessage") -> list[dict[str, Any]]:
        return [params for name, params in self.calls if name == method]

    # -- what the owner (or a stranger) does --------------------------------------------------

    def _update(self, **body: Any) -> int:
        update_id = self._next_update
        self._next_update += 1
        self.updates.append({"update_id": update_id, **body})
        return update_id

    def write(self, text: str, sender: int = OWNER, **extra: Any) -> int:
        self._next_message += 1
        message = {
            "message_id": self._next_message,
            "from": {"id": sender, "first_name": "Noan"},
            "chat": {"id": sender, "type": "private"},
            "date": 1_760_000_000,
            "text": text,
            **extra,
        }
        self._update(message=message)
        return self._next_message

    def send_photo(self, caption: str, data: bytes) -> None:
        self.files["photo-big"] = data
        self.write(
            "",
            photo=[{"file_id": "photo-small", "file_size": 10}, {"file_id": "photo-big"}],
            caption=caption,
        )

    def react(self, message_id: int, emoji: str, sender: int = OWNER) -> None:
        self._update(
            message_reaction={
                "chat": {"id": sender, "type": "private"},
                "message_id": message_id,
                "user": {"id": sender},
                "new_reaction": [{"type": "emoji", "emoji": emoji}],
            }
        )

    def press(self, message_id: int, data: str, sender: int = OWNER) -> None:
        self._update(
            callback_query={
                "id": f"cb{message_id}",
                "from": {"id": sender},
                "data": data,
                "message": {"message_id": message_id, "chat": {"id": sender}},
            }
        )

    # -- the API --------------------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        assert TOKEN in path
        if "/file/" in path:
            return httpx.Response(200, content=self.files[path.rsplit("/", 1)[1]])
        method = path.rsplit("/", 1)[1]
        if request.headers.get("content-type", "").startswith("application/json"):
            params: dict[str, Any] = json.loads(request.content or b"{}")
        else:
            params = {"multipart": len(request.content)}
        self.calls.append((method, params))
        return httpx.Response(200, json={"ok": True, "result": self._result(method, params)})

    def _result(self, method: str, params: dict[str, Any]) -> Any:
        if method == "getMe":
            return {"id": 1, "username": "orion_test_bot"}
        if method == "getUpdates":
            ready = [u for u in self.updates if u["update_id"] >= params.get("offset", 0)]
            return ready
        if method == "getFile":
            return {"file_path": params["file_id"]}
        if method.startswith("send") and method != "sendChatAction":
            self._next_message += 1
            return {"message_id": self._next_message}
        return True
