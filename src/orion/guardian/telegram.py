"""Telegram through the guardian process (D89): the bot's token never reaches the core.

The core uses the same Telegram client as in a single process, pointed at the guardian
(`GuardianClient.telegram`), which calls the Bot API with the token once its rules allow it:

- reads are allowed (`getMe`, `getUpdates`, `getFile`, and the download of a file he sent);
- a message, a file, « en train d'écrire » or the removal of buttons goes only to the owner's
  chat (`[channels.telegram] owner_id`), at most `max_sends_per_minute` messages a minute;
- a contact the owner allowed (`[modules.secretary]`, `telegram_id`) gets a plain text answer,
  only within `REPLY_WINDOW_HOURS` of their last message as the guardian saw it pass, a few a
  day; writing to them first takes the owner's yes to that very text (`contact_message`);
- the console's button in his chat (`setChatMenuButton`), only to the console's own address;
- nothing else: no webhook, no forwarding, no other chat, no change to the bot.

**The console in Telegram** (D91): the guardian checks the Mini App's signed launch data with
the token and tells the core who opened it (`/v1/telegram-login`); the token stays here.

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
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import structlog
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from orion.channels.imessage import answer_word
from orion.channels.telegram import (
    MAX_TEXT,
    NO,
    REACTIONS,
    YES,
    TelegramClient,
    TelegramError,
    check_init_data,
)
from orion.core.events import Trust
from orion.guardian.ledger import TELEGRAM_ANSWER
from orion.secretary.settings import REPLY_WINDOW_HOURS

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
        self.heard: dict[int, datetime] = {}
        """Contact's user id → when they last wrote, as seen passing through here. Kept in
        memory: after a restart, Orion answers them again once they write again."""

    def install(self) -> None:
        self.service.routes += [
            Route("/v1/telegram-login", self._login, methods=["POST"]),
            Route("/v1/telegram/file/{path:path}", self._download, methods=["GET"]),
            Route("/v1/telegram/{method}", self._call, methods=["POST"]),
        ]
        self.service.telegram = self

    async def aclose(self) -> None:
        await self.client.aclose()

    # -- what the guardian itself sends -----------------------------------------------------

    async def ask(self, text: str) -> int:
        """An approval question, with its two buttons. Returns its message id."""
        if not self.owner_id:
            raise TelegramError("the owner's Telegram id is not set", 503)
        if not self.service.may_send():
            raise TelegramError("too many messages to the owner this minute", 429)
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
            self._hear(update)
            update_id = int(update.get("update_id") or 0)
            if update_id in self.service.ledger.state.telegram_updates:
                continue
            found = answer(update, self.owner_id, questions)
            if found is not None:
                await self.service.ledger.record(
                    TELEGRAM_ANSWER, {"update_id": update_id, **found}, Trust.OWNER, "telegram"
                )

    def _hear(self, update: dict[str, Any]) -> None:
        message = cast(dict[str, Any], update.get("message") or {})
        sender = cast(dict[str, Any], message.get("from") or {}).get("id")
        chat = cast(dict[str, Any], message.get("chat") or {})
        if sender in self.service.telegram_contacts and chat.get("type") == "private":
            self.heard[int(sender)] = self.service.clock()

    def _answerable(self, user_id: int) -> str | None:
        """Why Orion may not answer this contact now, if it may not."""
        contact = self.service.telegram_contacts.get(user_id)
        if contact is None:
            return "only the owner's chat, or a contact's"
        last = self.heard.get(user_id)
        if last is None or self.service.clock() - last > timedelta(hours=REPLY_WINDOW_HOURS):
            return f"{contact.name} has not written to Orion in the last hours"
        return self.service.contact_quota(contact, f"telegram:{user_id}")

    async def contact_send(self, name: str, conversation: str, text: str) -> Response:
        """A message the owner approved for a contact (`contact_message`): it may start the
        conversation, but only to the contact of that name."""
        chat = conversation.removeprefix("telegram:")
        contact = self.service.telegram_contacts.get(int(chat)) if chat.isdigit() else None
        if contact is None or contact.name != name:
            return await self.service.deny(f"message to {name}", f"that chat is not {name}'s")
        reason = self.service.contact_quota(contact, f"telegram:{chat}")
        if reason is not None:
            return await self.service.deny(f"message to {name}", reason)
        try:
            sent = await self.client.send_message(contact.telegram_id, text[:MAX_TEXT])
        except TelegramError as exc:
            return JSONResponse({"error": f"Telegram: {exc}"}, status_code=502)
        return JSONResponse({"message_id": str(sent)})

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
        if method == "setChatMenuButton":
            return await self._menu(params)
        if method not in TO_OWNER and method not in FILES:
            return await self._refuse(method, f"{method} is never allowed")
        chat = params.get("chat_id")
        if not self.owner_id or chat != self.owner_id:
            # Someone else: a plain answer to a contact who just wrote, nothing more.
            if method != "sendMessage" or params.get("reply_markup") or not isinstance(chat, int):
                return await self._refuse(method, "only the owner's chat")
            reason = self._answerable(chat)
            if reason is not None:
                return await self._refuse(method, reason)
            return await self._forward(method, params)
        if method in COUNTED and not self.service.may_send():
            return await self._refuse(method, "too many messages this minute")
        if method in FILES:
            return await self._send_file(method, params)
        return await self._forward(method, params)

    async def _menu(self, params: dict[str, Any]) -> Response:
        """The console's button, in his chat only, opening the console only."""
        console = str((self.service.config.modules.get("console") or {}).get("public_url") or "")
        button = cast(dict[str, Any], params.get("menu_button") or {})
        url = str(cast(dict[str, Any], button.get("web_app") or {}).get("url") or "")
        home = console.rstrip("/") + "/console/"
        if not self.owner_id or params.get("chat_id") != self.owner_id:
            return await self._refuse("setChatMenuButton", "only the owner's chat")
        if not console.startswith("https://") or not url.startswith(home):
            return await self._refuse("setChatMenuButton", "only the console's own address")
        return await self._forward("setChatMenuButton", params)

    async def _login(self, request: Request) -> Response:
        """Who opened the console from Telegram, if Telegram signed it (D91)."""
        init_data = (await request.body()).decode(errors="replace")
        user = check_init_data(init_data, self.client.token, time.time())
        return JSONResponse({"user_id": user})

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
