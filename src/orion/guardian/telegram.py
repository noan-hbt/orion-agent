"""Telegram through the guardian process (D89): the bot's token never reaches the core.

The core uses the same Telegram client as in a single process, pointed at the guardian
(`GuardianClient.telegram`), which calls the Bot API with the token once its rules allow it:

- reads are allowed (`getMe`, `getUpdates`, `getFile`, and the download of a file he sent);
- a message, a file, « en train d'écrire » or the removal of buttons goes only to the owner's
  chat (`[channels.telegram] owner_id`), at most `max_sends_per_minute` messages a minute;
- nothing else: no webhook, no forwarding, no other chat, no change to the bot.

**Approvals.** The guardian writes the question itself, with Oui / Non buttons. The Bot API
keeps no history the guardian could read back, as it does with Inkbox: instead every update the
core reads passes through the guardian, which records the owner's answers to its own questions
(a button, a 👍 or 👎, or « oui » / « non » written after it) in its ledger. The core cannot
forge one: it never talks to Telegram itself.

    guardian.telegram.answer
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import TYPE_CHECKING, Any, cast

import structlog
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from orion.channels.imessage import answer_word
from orion.channels.telegram import MAX_TEXT, NO, REACTIONS, YES, TelegramClient, TelegramError
from orion.core.events import Trust
from orion.guardian.ledger import TELEGRAM_ANSWER

if TYPE_CHECKING:
    from orion.guardian.ledger import PendingApproval
    from orion.guardian.service import GuardianService

log = structlog.get_logger(__name__)

READS = frozenset({"getMe", "getUpdates", "getFile"})
TO_OWNER = frozenset({"sendMessage", "sendChatAction", "editMessageReplyMarkup"})
FILES = {"sendPhoto": "photo", "sendDocument": "document", "sendVoice": "voice"}
COUNTED = frozenset({"sendMessage", *FILES})
"""What counts against `max_sends_per_minute`: what he receives, not « typing »."""
MAX_FILE_BYTES = 50 * 1024 * 1024
"""What a bot may send."""


def answer(update: dict[str, Any], owner_id: int, questions: set[int]) -> dict[str, Any] | None:
    """The owner's answer to one of the guardian's questions in this update, if any: a button
    or a reaction on the question (`target`), or a word written after one (`message_id`)."""
    if not owner_id or not questions:
        return None
    if "callback_query" in update:
        query = cast(dict[str, Any], update["callback_query"])
        target = int(cast(dict[str, Any], query.get("message") or {}).get("message_id") or 0)
        data = str(query.get("data") or "")
        sender = cast(dict[str, Any], query.get("from") or {}).get("id")
        if sender == owner_id and target in questions and data in (YES, NO):
            return {"target": target, "answer": data == YES, "how": "button"}
    elif "message_reaction" in update:
        reaction = cast(dict[str, Any], update["message_reaction"])
        target = int(reaction.get("message_id") or 0)
        sender = cast(dict[str, Any], reaction.get("user") or {}).get("id")
        if sender != owner_id or target not in questions:
            return None
        for new in cast(list[dict[str, Any]], reaction.get("new_reaction") or []):
            label = REACTIONS.get(str(new.get("emoji") or ""))
            if label in ("like", "love", "dislike"):
                return {"target": target, "answer": label != "dislike", "how": "reaction"}
    elif "message" in update:
        message = cast(dict[str, Any], update["message"])
        sender = cast(dict[str, Any], message.get("from") or {}).get("id")
        chat = cast(dict[str, Any], message.get("chat") or {})
        message_id = int(message.get("message_id") or 0)
        said = answer_word(str(message.get("text") or ""))
        if (
            sender == owner_id
            and chat.get("type") == "private"
            and said is not None
            and message_id > min(questions)
        ):
            return {"message_id": message_id, "answer": said, "how": "word"}
    return None


def decided(approval_message: int, answers: list[dict[str, Any]], questions: set[int]) -> bool:
    """Whether the owner said yes to this question. A no on it wins; then a yes on it; then
    the first word written after it, unless another question came in between."""
    direct = [a for a in answers if a.get("target") == approval_message]
    if any(not a["answer"] for a in direct):
        return False
    if direct:
        return True
    later = sorted(
        (a for a in answers if int(a.get("message_id") or 0) > approval_message),
        key=lambda a: int(a["message_id"]),
    )
    for word in later:
        if any(approval_message < q < int(word["message_id"]) for q in questions):
            return False  # a newer question: the words after it answer that one
        return bool(word["answer"])
    return False


class TelegramGate:
    def __init__(self, service: GuardianService, client: TelegramClient) -> None:
        self.service = service
        self.client = client
        self.owner_id = service.config.channels.telegram.owner_id
        self.conversation = f"telegram:{self.owner_id}"
        """How approvals name the owner's chat (`ApprovalRequest.conversation_id`)."""

    def install(self) -> None:
        self.service.routes += [
            Route("/v1/telegram/file/{path:path}", self._download, methods=["GET"]),
            Route("/v1/telegram/{method}", self._call, methods=["POST"]),
        ]
        self.service.telegram = self

    async def aclose(self) -> None:
        await self.client.aclose()

    # -- what the guardian itself sends -----------------------------------------------------

    async def ask(self, text: str) -> int:
        """An approval question, with its two buttons. Returns its message id."""
        return await self.client.send_message(
            self.owner_id, text[:MAX_TEXT], [("Oui", YES), ("Non", NO)]
        )

    async def tell(self, text: str) -> None:
        if not self.owner_id:
            log.warning("guardian.telegram_owner_unknown")
            return
        await self.client.send_message(self.owner_id, text[:MAX_TEXT])

    async def owner_said_yes(self, approval: PendingApproval) -> bool:
        await self.service.ledger.catch_up()
        state = self.service.ledger.state
        return decided(int(approval.message_id), state.telegram_answers, self._questions())

    def _questions(self, open_only: bool = False) -> set[int]:
        return {
            int(a.message_id)
            for a in self.service.ledger.state.approvals.values()
            if a.conversation_id == self.conversation and not (open_only and a.used)
        }

    async def _observe(self, updates: list[dict[str, Any]]) -> None:
        """Every update the core reads: the owner's answers to the guardian's questions are
        recorded, once each."""
        await self.service.ledger.catch_up()
        questions = self._questions(open_only=True)
        for update in updates:
            update_id = int(update.get("update_id") or 0)
            if update_id in self.service.ledger.state.telegram_updates:
                continue
            found = answer(update, self.owner_id, questions)
            if found is not None:
                await self.service.ledger.record(
                    TELEGRAM_ANSWER, {"update_id": update_id, **found}, Trust.OWNER, "telegram"
                )

    # -- the core's requests ----------------------------------------------------------------

    async def _call(self, request: Request) -> Response:
        method = str(request.path_params["method"])
        try:
            loaded: Any = json.loads(await request.body() or b"{}")
        except json.JSONDecodeError:
            return await self._refuse(method, "the body is not JSON")
        if not isinstance(loaded, dict):
            return await self._refuse(method, "the body is not a JSON object")
        params = cast(dict[str, Any], loaded)
        if method in READS:
            return await self._forward(method, params)
        if method == "answerCallbackQuery":
            return await self._forward(
                method, {"callback_query_id": params.get("callback_query_id")}
            )
        if method not in TO_OWNER and method not in FILES:
            return await self._refuse(method, f"{method} is never allowed")
        if not self.owner_id or params.get("chat_id") != self.owner_id:
            return await self._refuse(method, "only the owner's chat")
        if method in COUNTED and not self.service.may_send():
            return await self._refuse(method, "too many messages this minute")
        if method in FILES:
            return await self._send_file(method, params)
        return await self._forward(method, params)

    async def _send_file(self, method: str, params: dict[str, Any]) -> Response:
        file = cast(dict[str, Any], params.get("file") or {})
        try:
            data = base64.b64decode(str(file.get("data") or ""), validate=True)
        except (binascii.Error, ValueError):
            return await self._refuse(method, "the file is not base64")
        if not data or len(data) > MAX_FILE_BYTES:
            return await self._refuse(method, "empty or too large file")
        try:
            sent = await self.client.send_file(
                self.owner_id,
                method,
                FILES[method],
                str(file.get("name") or "fichier"),
                data,
                str(params.get("caption") or ""),
            )
        except TelegramError as exc:
            return _failed(exc)
        return _ok({"message_id": sent})

    async def _forward(self, method: str, params: dict[str, Any]) -> Response:
        try:
            result = await self.client.call(method, **params)
        except TelegramError as exc:
            return _failed(exc)
        if method == "getUpdates":
            await self._observe(cast(list[dict[str, Any]], result or []))
        return _ok(result)

    async def _download(self, request: Request) -> Response:
        path = str(request.path_params["path"])
        try:
            data = await self.client.download(path)
        except TelegramError as exc:
            return _failed(exc)
        return Response(data, media_type="application/octet-stream")

    async def _refuse(self, method: str, reason: str) -> Response:
        await self.service.deny(f"telegram {method}", reason)
        return JSONResponse(
            {"ok": False, "error_code": 403, "description": f"guardian: {reason}"},
            status_code=403,
        )


def _ok(result: Any) -> Response:
    return JSONResponse({"ok": True, "result": result})


def _failed(exc: TelegramError) -> Response:
    """Telegram's own answer shape, so the core's client reads it as it reads Telegram."""
    status = exc.status or 502
    return JSONResponse(
        {"ok": False, "error_code": status, "description": str(exc)}, status_code=status
    )
