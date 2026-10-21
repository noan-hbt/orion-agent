"""Telegram channel, through a bot of the owner's own (D88).

Free and without a daily cap. It does what the iMessage channel does, through the Bot API:

- updates come by long polling (`getUpdates`); each one is journaled before anything else
  happens, so a restart resumes after the last one and requeues an owner message never
  answered. Only the owner's user id is obeyed; anyone else is journaled and handed to the
  secretary (`others`), which answers the contacts he allowed and nobody else;
- photos, voice messages and files reach the inbox as attachments the channel fetches itself:
  the download link carries the bot's token, which never leaves this module;
- the reply goes through the outbox as one message (cut only past Telegram's 4 096
  characters); a file goes as a photo, a voice message (OGG Opus) or a document;
- while Orion thinks, Telegram shows « en train d'écrire… »;
- an action is put to the owner with two buttons (Oui / Non), or a one-word answer; no answer
  before the timeout means no. Run split, the guardian writes the question and checks the
  answer itself (D89);
- his reactions on Orion's messages (👍, ❤️, 👎…) are signals for adaptation.

    telegram.inbound  (every update read: a message, a reaction, a button)
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import mimetypes
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qsl

import httpx
import structlog

from orion.adaptation.signals import FEEDBACK_REACTION, reaction_event
from orion.channels.imessage import (
    APPROVAL_DECIDED,
    APPROVAL_REQUESTED,
    ApprovalDesk,
    answer_word,
)
from orion.channels.inbox import Attachment, Incoming, OwnerInbox
from orion.channels.inbox import Inbound as InboxItem
from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.guardian.outbox import Outbox, SendError
from orion.guardian.policy import ActionProposal, Approval

log = structlog.get_logger(__name__)

CHANNEL = "telegram"
INBOUND = "telegram.inbound"
API = "https://api.telegram.org"
MAX_TEXT = 4096
MAX_CAPTION = 1024
TYPING_EVERY = 4.5
"""Telegram shows « typing » for five seconds."""
REACTIONS = {
    "👍": "like",
    "❤": "love",
    "❤️": "love",
    "🔥": "love",
    "😍": "love",
    "👎": "dislike",
    "😁": "laugh",
    "🤣": "laugh",
    "😂": "laugh",
    "‼": "emphasize",
    "🤔": "question",
    "👀": "eyes",
}
YES, NO = "yes", "no"


class TelegramError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


RECONNECT = (httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadError, httpx.WriteError)
"""Failures of the connection itself, before Telegram answered: worth one more try."""
RETRY_PAUSES = (0.5, 2.0)


class TelegramClient:
    """The Bot API, as little of it as Orion needs. The token is in every URL: errors never
    carry a URL, so it cannot reach a log."""

    def __init__(
        self,
        token: str,
        base_url: str = API,
        *,
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._base = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def token(self) -> str:
        """For checking what Telegram signed with it (a Mini App's launch data), in the
        process that holds it."""
        return self._token

    def method_url(self, method: str) -> str:
        return f"{self._base}/bot{self._token}/{method}"

    def file_url(self, file_path: str) -> str:
        return f"{self._base}/file/bot{self._token}/{file_path}"

    async def call(
        self, method: str, files: dict[str, tuple[str, bytes, str]] | None = None, **params: Any
    ) -> Any:
        url = self.method_url(method)
        attempt = 0
        while True:
            try:
                if files:
                    data = {k: str(v) for k, v in params.items() if v is not None}
                    response = await self._client.post(url, data=data, files=files)
                else:
                    body = {k: v for k, v in params.items() if v is not None}
                    response = await self._client.post(url, json=body)
                break
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                # A connection the network dropped while idle (the 23:16 approval that never
                # left, D98): tried again on a fresh one. The long poll retries on its own.
                if (
                    method != "getUpdates"
                    and isinstance(exc, RECONNECT)
                    and attempt < len(RETRY_PAUSES)
                ):
                    log.info("telegram.retry", method=method, error=type(exc).__name__)
                    await asyncio.sleep(RETRY_PAUSES[attempt])
                    attempt += 1
                    continue
                raise TelegramError(
                    f"{method}: transport error ({type(exc).__name__})", retryable=True
                ) from None
        try:
            answer = cast(dict[str, Any], response.json())
        except ValueError:
            raise TelegramError(
                f"{method}: HTTP {response.status_code}", response.status_code, True
            ) from None
        if not answer.get("ok"):
            status = response.status_code
            raise TelegramError(
                f"{method}: {status} {str(answer.get('description', ''))[:200]}",
                status,
                retryable=status == 429 or status >= 500,
            )
        return answer.get("result")

    async def me(self) -> dict[str, Any]:
        return cast(dict[str, Any], await self.call("getMe"))

    async def updates(self, offset: int, wait: int) -> list[dict[str, Any]]:
        found = await self.call(
            "getUpdates",
            offset=offset,
            timeout=wait,
            allowed_updates=["message", "message_reaction", "callback_query"],
        )
        return cast(list[dict[str, Any]], found)

    async def send_message(
        self, chat_id: int, text: str, buttons: list[tuple[str, str]] | None = None
    ) -> int:
        markup = (
            {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in buttons]]}
            if buttons
            else None
        )
        sent = await self.call("sendMessage", chat_id=chat_id, text=text, reply_markup=markup)
        return int(cast(dict[str, Any], sent)["message_id"])

    async def send_file(
        self, chat_id: int, method: str, field_name: str, name: str, data: bytes, caption: str
    ) -> int:
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        sent = await self.call(
            method,
            files={field_name: (name, data, content_type)},
            chat_id=chat_id,
            caption=caption[:MAX_CAPTION] or None,
        )
        return int(cast(dict[str, Any], sent)["message_id"])

    async def menu_button(self, chat_id: int, text: str, url: str) -> None:
        """The button beside his message field that opens a page inside Telegram (a Mini
        App): the console (D91)."""
        await self.call(
            "setChatMenuButton",
            chat_id=chat_id,
            menu_button={"type": "web_app", "text": text, "web_app": {"url": url}},
        )

    async def typing(self, chat_id: int) -> None:
        await self.call("sendChatAction", chat_id=chat_id, action="typing")

    async def answered(self, callback_id: str, chat_id: int, message_id: int) -> None:
        """A button was pressed: Telegram stops its spinner, and the buttons go away."""
        await self.call("answerCallbackQuery", callback_query_id=callback_id)
        await self.call(
            "editMessageReplyMarkup",
            chat_id=chat_id,
            message_id=message_id,
            reply_markup={"inline_keyboard": []},
        )

    async def file_bytes(self, file_id: str) -> bytes:
        found = cast(dict[str, Any], await self.call("getFile", file_id=file_id))
        return await self.download(str(found["file_path"]))

    async def download(self, file_path: str) -> bytes:
        url = self.file_url(file_path)
        try:
            response = await self._client.get(url)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TelegramError(
                f"download: transport error ({type(exc).__name__})", retryable=True
            ) from None
        if response.status_code != 200:
            raise TelegramError(f"download: HTTP {response.status_code}", response.status_code)
        return response.content


@dataclass(frozen=True)
class TelegramSettings:
    owner_id: int
    poll_seconds: int = 25
    approval_timeout_seconds: float = 900.0


@dataclass(frozen=True)
class Media:
    file_id: str
    content_type: str
    size: int | None = None


@dataclass(frozen=True)
class Message:
    ref: str
    """`telegram:<chat>:<message>`: what the inbox and the journal call it."""
    chat_id: int
    text: str
    media: tuple[Media, ...] = ()
    sent_at: datetime | None = None


@dataclass
class TelegramState:
    """Where polling stands, the owner's chat, and which of his messages were answered: rebuilt
    from the journal, so a restart neither loses nor repeats one."""

    name: str = "telegram-channel"
    offset: int = 0
    owner_chat: int | None = None
    owner_messages: dict[str, Message] = field(default_factory=dict[str, Message])
    handled: set[str] = field(default_factory=set[str])
    reactions: set[str] = field(default_factory=set[str])
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.offset, self.owner_chat, self._seq = 0, None, 0
        self.owner_messages.clear()
        self.handled.clear()
        self.reactions.clear()

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        payload: dict[str, Any] = event.payload
        if event.kind == INBOUND:
            self.offset = max(self.offset, int(payload["update_id"]) + 1)
            if event.redacted or not payload.get("is_owner") or payload.get("type") != "message":
                return
            self.owner_chat = int(payload["chat_id"])
            ref = str(payload["ref"])
            self.owner_messages[ref] = Message(
                ref,
                int(payload["chat_id"]),
                str(payload.get("text") or ""),
                tuple(Media(**m) for m in cast(list[dict[str, Any]], payload.get("media") or [])),
                _instant(payload.get("date")),
            )
        elif event.redacted:
            return
        elif event.kind == FEEDBACK_REACTION:
            self.reactions.add(str(payload.get("reaction_id")))
        elif event.kind in ("message.received", APPROVAL_DECIDED):
            self.handled.update(str(ref) for ref in payload.get("refs", []))

    def unhandled(self) -> list[Message]:
        return [m for ref, m in self.owner_messages.items() if ref not in self.handled]


class TelegramChannel:
    """Inbound handling, outbound transport, inbox outlet and approver for Telegram."""

    name = CHANNEL

    def __init__(
        self,
        client: TelegramClient,
        journal: EventStore,
        inbox: OwnerInbox,
        settings: TelegramSettings,
        desk: ApprovalDesk | None = None,
    ) -> None:
        self._client = client
        self._desk = desk
        self._journal = journal
        self._inbox = inbox
        self._settings = settings
        self.outbox = Outbox(journal, {CHANNEL: self})
        self.state = TelegramState()
        self._runner = ProjectionRunner(journal, [self.state])
        self._approvals: dict[int, asyncio.Future[tuple[bool, str, list[str]]]] = {}
        self.others: Callable[[Incoming], None] | None = None
        """Where a private message from someone else goes (the secretary, D54)."""
        inbox.register(self)

    async def start(self) -> None:
        """Catch up, finish sends interrupted by a crash, requeue unanswered owner messages."""
        await self._runner.catch_up()
        await self.outbox.resume()
        for message in self.state.unhandled():
            log.info("telegram.requeue", ref=message.ref)
            self._inbox.submit(self._inbound(message))

    def _inbound(self, message: Message) -> InboxItem:
        return InboxItem(
            CHANNEL,
            (message.ref,),
            message.text,
            context=str(message.chat_id),
            media=tuple(self._attachment(m) for m in message.media),
            sent_at=message.sent_at,
        )

    def _attachment(self, media: Media) -> Attachment:
        async def fetch() -> bytes:
            try:
                return await self._client.file_bytes(media.file_id)
            except TelegramError as exc:
                raise OSError(str(exc)) from exc  # what `photos` reports as unreadable

        return Attachment(f"telegram:{media.file_id}", media.content_type, media.size, fetch)

    # -- outbound: transport for the outbox, outlet for the inbox ------------------------------

    async def transmit(self, to: str, text: str, idempotency_key: str) -> str:
        try:
            return str(await self._client.send_message(int(to), text))
        except TelegramError as exc:
            raise SendError(str(exc), retryable=exc.retryable) from exc

    async def transmit_file(self, to: str, text: str, path: str, idempotency_key: str) -> str:
        file = Path(path)
        try:
            data = await asyncio.to_thread(file.read_bytes)
        except OSError as exc:
            raise SendError(f"the file cannot be read: {exc}", retryable=False) from exc
        content_type = mimetypes.guess_type(file.name)[0] or ""
        method, field_name, name = "sendDocument", "document", file.name
        if content_type in ("image/jpeg", "image/png", "image/webp"):
            method, field_name = "sendPhoto", "photo"
        elif content_type.startswith("audio/") or file.suffix in (".m4a", ".ogg", ".opus"):
            from orion.vocal.audio import AudioError, to_voice_note

            try:
                data = await to_voice_note(data)
                method, field_name, name = "sendVoice", "voice", f"{file.stem}.ogg"
            except AudioError:
                log.warning("telegram.voice_as_file", file=file.name, exc_info=True)
        try:
            sent = await self._client.send_file(int(to), method, field_name, name, data, text)
        except TelegramError as exc:
            raise SendError(str(exc), retryable=exc.retryable) from exc
        return str(sent)

    async def available(self) -> bool:
        await self._runner.catch_up()
        return self.state.owner_chat is not None

    async def deliver(self, text: str, correlation_id: str | None) -> None:
        await self._send_to_owner(text, correlation_id)

    async def deliver_file(self, path: Path, caption: str, correlation_id: str | None) -> None:
        chat = self._owner_chat()
        await self.outbox.send(CHANNEL, str(chat), caption, correlation_id, media=str(path))

    @asynccontextmanager
    async def working(self, item: InboxItem) -> AsyncGenerator[None]:
        chat = int(item.context) if item.context else self.state.owner_chat
        if chat is None:
            yield
            return
        typing = asyncio.create_task(self._keep_typing(chat))
        try:
            yield
        finally:
            typing.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await typing

    async def _keep_typing(self, chat: int) -> None:
        while True:
            try:
                await self._client.typing(chat)
            except TelegramError:
                log.warning("telegram.cosmetic_failed", call="typing", exc_info=True)
            await asyncio.sleep(TYPING_EVERY)

    def _owner_chat(self) -> int:
        if self.state.owner_chat is None:
            raise SendError("the owner has not written to Orion on Telegram yet", retryable=False)
        return self.state.owner_chat

    async def _send_to_owner(self, text: str, correlation_id: str | None = None) -> list[str]:
        chat = str(self._owner_chat())
        return [
            await self.outbox.send(CHANNEL, chat, part, correlation_id)
            for part in _parts(text.strip())
        ]

    # -- inbound ------------------------------------------------------------------------------

    async def poll_once(self) -> None:
        await self._runner.catch_up()
        for update in await self._client.updates(self.state.offset, self._settings.poll_seconds):
            await self.receive(update)

    async def poll_forever(self) -> None:
        while True:
            try:
                await self.poll_once()
            except TelegramError as exc:
                if not exc.retryable:
                    raise
                log.warning("telegram.poll_failed", error=str(exc))
                await asyncio.sleep(5)

    async def receive(self, update: dict[str, Any]) -> None:
        await self._runner.catch_up()
        update_id = int(update["update_id"])
        if update_id < self.state.offset:
            return
        if "message" in update:
            await self._message(update_id, cast(dict[str, Any], update["message"]))
        elif "message_reaction" in update:
            await self._reaction(update_id, cast(dict[str, Any], update["message_reaction"]))
        elif "callback_query" in update:
            await self._button(update_id, cast(dict[str, Any], update["callback_query"]))
        else:
            await self._record(update_id, "other", False, {})

    async def _message(self, update_id: int, message: dict[str, Any]) -> None:
        sender = cast(dict[str, Any], message.get("from") or {})
        chat = cast(dict[str, Any], message.get("chat") or {})
        is_owner = (
            self._settings.owner_id != 0
            and sender.get("id") == self._settings.owner_id
            and chat.get("type") == "private"
        )
        media = _media(message)
        text = str(message.get("text") or message.get("caption") or "")
        ref = f"telegram:{chat.get('id')}:{message.get('message_id')}"
        await self._record(
            update_id,
            "message",
            is_owner,
            {
                "ref": ref,
                "chat_id": chat.get("id"),
                "sender": sender.get("id"),
                "text": text,
                "date": message.get("date"),
                "media": [m.__dict__ for m in media],
                **({} if is_owner else {"name": _name(sender)}),
            },
            source=f"telegram:{chat.get('id')}",
            actor="owner:main" if is_owner else f"telegram:{sender.get('id')}",
        )
        if not is_owner:
            log.info(
                "telegram.stranger",
                user_id=sender.get("id"),
                name=sender.get("first_name"),
                hint="the owner? put this id in [channels.telegram] owner_id",
            )
            if self.others is not None and chat.get("type") == "private" and sender.get("id"):
                self.others(
                    Incoming(
                        CHANNEL,
                        f"{CHANNEL}:{sender['id']}",
                        f"{CHANNEL}:{chat['id']}",
                        ref,
                        text or ("(une pièce jointe)" if media else ""),
                    )
                )
            return
        if not media and self._approvals and self._resolve_by_word(text, ref):
            return
        self._inbox.submit(
            self._inbound(Message(ref, int(chat["id"]), text, media, _instant(message.get("date"))))
        )

    async def _reaction(self, update_id: int, reaction: dict[str, Any]) -> None:
        user = cast(dict[str, Any], reaction.get("user") or {})
        is_owner = user.get("id") == self._settings.owner_id != 0
        await self._record(update_id, "reaction", is_owner, {})
        if not is_owner:
            return
        target = int(reaction["message_id"])
        for new in cast(list[dict[str, Any]], reaction.get("new_reaction") or []):
            emoji = str(new.get("emoji") or "")
            label = REACTIONS.get(emoji, "custom")
            if target in self._approvals:
                self._resolve(target, label in ("like", "love"), label == "dislike", label)
                continue
            reaction_id = f"telegram:{update_id}:{emoji}"
            await self._runner.catch_up()
            if reaction_id in self.state.reactions:
                continue
            chat = cast(dict[str, Any], reaction.get("chat") or {})
            await self._journal.append(
                [
                    reaction_event(
                        str(target), label, emoji, reaction_id, f"telegram:{chat.get('id')}"
                    )
                ]
            )

    async def _button(self, update_id: int, query: dict[str, Any]) -> None:
        user = cast(dict[str, Any], query.get("from") or {})
        is_owner = user.get("id") == self._settings.owner_id != 0
        await self._record(update_id, "button", is_owner, {"data": query.get("data")})
        message = cast(dict[str, Any], query.get("message") or {})
        target = int(message.get("message_id") or 0)
        if is_owner and target in self._approvals:
            data = str(query.get("data") or "")
            self._resolve(target, data == YES, data == NO, "button")
        with contextlib.suppress(TelegramError):
            chat = cast(dict[str, Any], message.get("chat") or {})
            await self._client.answered(str(query["id"]), int(chat.get("id") or 0), target)

    async def _record(
        self,
        update_id: int,
        kind: str,
        is_owner: bool,
        payload: dict[str, Any],
        source: str = "telegram",
        actor: str = "telegram",
    ) -> None:
        await self._journal.append(
            [
                NewEvent(
                    kind=INBOUND,
                    payload={"update_id": update_id, "type": kind, "is_owner": is_owner, **payload},
                    provenance=Provenance(source=source, actor=actor),
                    trust=Trust.OWNER if is_owner else Trust.THIRD_PARTY,
                    correlation_id=new_id(),
                )
            ]
        )
        await self._runner.catch_up()

    # -- approvals ----------------------------------------------------------------------------

    async def approve(self, proposal: ActionProposal) -> Approval:
        chat = self.state.owner_chat
        if chat is None:
            return Approval(False, reached=False)
        approval_id = f"a_{new_id()}"
        arguments = proposal.arguments.model_dump(mode="json")
        await self._approval_event(
            APPROVAL_REQUESTED,
            {"approval_id": approval_id, "tool": proposal.tool.name, "arguments": arguments},
        )
        question = (
            f"Avant que je le fasse : {proposal.describe()}\n"
            + (f"Je demande parce que {proposal.ask_reason}.\n" if proposal.ask_reason else "")
            + "Oui ou non ?"
        )
        grant: str | None = None
        try:
            if self._desk is not None:
                # The guardian writes the question itself and will check the answer itself.
                grant, asked = await self._desk.request_approval(
                    proposal.tool.name, arguments, proposal.ask_reason, f"telegram:{chat}"
                )
                message_id = int(asked)
            else:
                message_id = await self._client.send_message(
                    chat, question[:MAX_TEXT], [("Oui", YES), ("Non", NO)]
                )
        except (TelegramError, ConnectionError):
            log.warning("telegram.approval_unsent", tool=proposal.tool.name, exc_info=True)
            approved, how, refs = False, "unsent", []
        else:
            future: asyncio.Future[tuple[bool, str, list[str]]] = (
                asyncio.get_running_loop().create_future()
            )
            self._approvals[message_id] = future
            try:
                approved, how, refs = await asyncio.wait_for(
                    future, self._settings.approval_timeout_seconds
                )
            except TimeoutError:
                approved, how, refs = False, "timeout", []
            finally:
                self._approvals.pop(message_id, None)
        await self._approval_event(
            APPROVAL_DECIDED,
            {
                "approval_id": approval_id,
                "approved": approved,
                "how": how,
                "refs": refs,
                **({"grant": grant} if grant else {}),
            },
        )
        return Approval(approved, grant if approved else None, reached=how != "unsent")

    def _resolve_by_word(self, text: str, ref: str) -> bool:
        answer = answer_word(text)
        if answer is None:
            return False
        future = self._approvals[next(reversed(self._approvals))]
        if not future.done():
            future.set_result((answer, "word", [ref]))
        return True

    def _resolve(self, message_id: int, yes: bool, no: bool, how: str) -> None:
        future = self._approvals.get(message_id)
        if future is None or future.done() or not (yes or no):
            return
        future.set_result((yes, how, []))

    async def _approval_event(self, kind: str, payload: dict[str, object]) -> None:
        await self._journal.append(
            [
                NewEvent(
                    kind=kind,
                    payload=payload,
                    provenance=Provenance(source="orion:guardian", actor="orion:guardian"),
                    trust=Trust.OWNER if kind == APPROVAL_DECIDED else Trust.SYSTEM,
                    correlation_id=new_id(),
                )
            ]
        )
        await self._runner.catch_up()


def _media(message: dict[str, Any]) -> tuple[Media, ...]:
    found: list[Media] = []
    photos = cast(list[dict[str, Any]], message.get("photo") or [])
    if photos:
        biggest = photos[-1]  # sizes come smallest first
        found.append(Media(str(biggest["file_id"]), "image/jpeg", biggest.get("file_size")))
    for key, default in (
        ("voice", "audio/ogg"),
        ("audio", "audio/mpeg"),
        ("document", "application/octet-stream"),
        ("video", "video/mp4"),
        ("video_note", "video/mp4"),
    ):
        item = cast(dict[str, Any] | None, message.get(key))
        if item:
            found.append(
                Media(
                    str(item["file_id"]),
                    str(item.get("mime_type") or default),
                    item.get("file_size"),
                )
            )
    return tuple(found)


LOGIN_MAX_AGE = 3600.0
"""Seconds a Mini App's signed launch data stays good for logging in."""


def check_init_data(init_data: str, token: str, now: float) -> int | None:
    """The Telegram user a Mini App was opened by, if Telegram really signed its launch data
    with this bot's token, less than an hour ago (core.telegram.org/bots/webapps)."""
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    given = pairs.pop("hash", "")
    if not given:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, given):
        return None
    signed = pairs.get("auth_date", "")
    if not signed.isdigit() or now - int(signed) > LOGIN_MAX_AGE:
        return None
    try:
        user: Any = json.loads(pairs.get("user") or "{}")
    except json.JSONDecodeError:
        return None
    found = cast(dict[str, Any], user).get("id") if isinstance(user, dict) else None
    return found if isinstance(found, int) and found > 0 else None


def _name(sender: dict[str, Any]) -> str:
    """How the owner will recognise them: « Angèle (@angele) »."""
    full = " ".join(str(sender.get(k) or "") for k in ("first_name", "last_name")).strip()
    handle = f"@{sender['username']}" if sender.get("username") else ""
    if full and handle:
        return f"{full} ({handle})"
    return full or handle


def _parts(text: str) -> list[str]:
    """One message, unless it is longer than Telegram takes: then cut at paragraphs."""
    if len(text) <= MAX_TEXT:
        return [text] if text else []
    parts: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        while len(paragraph) > MAX_TEXT:
            parts.append(paragraph[:MAX_TEXT])
            paragraph = paragraph[MAX_TEXT:]
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) > MAX_TEXT:
            parts.append(current)
            current = paragraph
        else:
            current = candidate
    return [*parts, current] if current else parts


def _instant(stamp: object) -> datetime | None:
    if isinstance(stamp, int | float):
        return datetime.fromtimestamp(stamp, UTC)
    return None
