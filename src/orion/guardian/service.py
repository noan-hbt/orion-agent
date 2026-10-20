"""The guardian process (D28): what protects is kept apart from what acts.

`orion guardian` runs this service on a Unix socket. It holds the secrets (the Inkbox key and
webhook signing key, the calendars' secret addresses, the mail passwords, the federation key)
and `orion serve`, which holds only the LLM key, reaches the outside world through it. Each
request is checked against rules that hold even if the core is compromised:

- **Inkbox**: reads are allowed; a message, a typing indicator or a read receipt goes only to a
  conversation the guardian itself checked is the owner's (and at most `max_sends_per_minute`
  messages a minute); the core can never change the line's settings (the whitelist stays on),
  act for another identity, or call anyone but the owner without an approval;
- **approvals**: the guardian writes the approval message itself, from the exact action and
  its parameters, so the owner sees what will run. A gated request then presents the
  approval, and the guardian checks with Inkbox that the owner really said yes (a 👍 on that
  message, or a "oui" after it) before letting it through, once;
- **Telegram** (D89): the same rules for the bot (`orion.guardian.telegram`), which holds the
  bot's token; the owner's answers to its questions are read as the core's updates pass;
- **calendars and mail**: fetched here, so the secret addresses and passwords never reach the
  core; what comes back is the content the core asked for, nothing else.

Policy that needs the conversation (the trust of the context a tool runs in) stays in the
core (`orion.guardian.policy`): the guardian only sees requests, never the agent's reasoning.
"""

from __future__ import annotations

import asyncio
import imaplib
import json
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import httpx
import structlog
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import BaseRoute, Route

from orion.calendar.service import CalendarAccount, CalendarSettings, fetch_feed
from orion.calendar.write import CalendarWriteError, NewCalendarEvent, write_event
from orion.channels.imessage import REACTION_NO, REACTION_YES, answer_word, normalize_number
from orion.channels.inkbox import GRANT_HEADER, InkboxClient, InkboxError, verify_signature
from orion.channels.telegram import TelegramError
from orion.config import OrionConfig
from orion.core.ids import new_id
from orion.federation.wire import CONFIRM
from orion.guardian.ledger import (
    APPROVAL_REQUESTED,
    APPROVAL_USED,
    AUTONOMOUS,
    AUTONOMY_GRANTED,
    AUTONOMY_REVOKED,
    CORE_BACK,
    CORE_SILENT,
    DENIED,
    MAIL_SENT,
    OWNER_CONVERSATION,
    SESSION_LENT,
    WRITTEN,
    Ledger,
    PendingApproval,
)
from orion.guardian.limits import Limits, host_in, missing, within
from orion.guardian.wire import ApprovalRequest, batch_to_json, mail_request
from orion.mail.service import Fetch as MailFetch
from orion.mail.service import MailAccount, MailSettings, imap_fetch
from orion.mail.smtp import Outgoing, SendError, smtp_send
from orion.secretary.settings import REPLY_WINDOW_HOURS, Contact, SecretarySettings

if TYPE_CHECKING:
    from orion.guardian.telegram import TelegramGate

log = structlog.get_logger(__name__)

CalendarFetch = Callable[[CalendarAccount], Awaitable[str]]
CalendarWrite = Callable[[CalendarAccount, NewCalendarEvent], Awaitable[str]]
MailSend = Callable[[MailAccount, Outgoing, str | None], Awaitable[str]]
Renderer = Callable[[dict[str, Any]], str]
"""Turns an approved action's parameters into the sentence the owner reads."""
Match = Callable[[dict[str, Any]], bool]

_ALWAYS_READABLE = (
    re.compile(r"/imessage/messages"),
    re.compile(r"/imessage/messages/[^/]+"),
    re.compile(r"/imessage/triage-number"),
    re.compile(r"/phone/calls"),
    re.compile(r"/phone/calls/[^/]+/transcripts"),
)
_TO_OWNER = ("/imessage/messages", "/imessage/typing", "/imessage/mark-read")
MAX_MEDIA_BYTES = 10 * 1024 * 1024 + 4096
"""Inkbox's limit for one file, plus the multipart envelope."""


class GuardianError(RuntimeError):
    """The guardian cannot start: a secret or the Inkbox identity is missing."""


def _generic(action: str) -> Renderer:
    def render(params: dict[str, Any]) -> str:
        return f"Avant que je le fasse : {action}({json.dumps(params, ensure_ascii=False)})"

    return render


def _autonomy_grant(params: dict[str, Any]) -> str:
    from orion.guardian.autonomy import label

    action = str(params.get("action"))
    times = int(params.get("approved_times") or 0)
    said = f"Tu m'as dit oui {times} fois de suite pour {label(action)}. " if times else ""
    bounds = _limits(params).describe()
    within_bounds = f" ({bounds})" if bounds else ""
    return (
        f"{said}Je peux désormais {label(action)}{within_bounds} sans te demander à chaque "
        "fois ? Je te préviendrai à chaque fois, et un mot suffit pour me le retirer."
    )


def _limits(params: dict[str, Any]) -> Limits:
    raw: dict[str, Any] = {
        "recipients": list(params.get("recipients") or []),
        "domains": list(params.get("domains") or []),
    }
    return Limits.model_validate(raw)


def _computer_task_send(params: dict[str, Any]) -> str:
    sites = (
        f"\nSites où elle peut envoyer : {', '.join(params['sites'])}"
        if params.get("sites")
        else ""
    )
    session = (
        f"\nAvec ta session connectée « {params['session']} »." if params.get("session") else ""
    )
    return (
        "Avant que je la lance sur mon ordinateur, voici la tâche (elle peut envoyer ce "
        f"qu'elle dit) :\n{params.get('brief')}{sites}{session}"
    )


async def _body(request: Request) -> dict[str, Any]:
    try:
        loaded: Any = json.loads(await request.body() or b"{}")
    except json.JSONDecodeError:
        return {}
    return cast(dict[str, Any], loaded) if isinstance(loaded, dict) else {}


def _calendar_add(params: dict[str, Any]) -> str:
    where = f"\nLieu : {params['location']}" if params.get("location") else ""
    notes = f"\nNote : {params['notes']}" if params.get("notes") else ""
    return (
        f"Avant que je l'ajoute à ton agenda : « {params.get('title')} », "
        f"du {_when(params.get('start'))} au {_when(params.get('end'))}.{where}{notes}"
    )


def _mail_send(params: dict[str, Any]) -> str:
    cc = f"\nCc : {', '.join(params['cc'])}" if params.get("cc") else ""
    files = list(params.get("attachments") or [])
    attached = f"\n\nPièce(s) jointe(s) : {', '.join(files)}" if files else ""
    return (
        f"Avant que je l'envoie, voici le mail :\nÀ : {', '.join(params.get('to', []))}{cc}\n"
        f"Objet : {params.get('subject')}\n\n{params.get('body')}{attached}\n"
    )


def _mail_tidy(params: dict[str, Any]) -> str:
    what = "archiver" if params.get("action") == "archive" else "me désabonner de"
    cards = list(params.get("cards") or [])
    given: list[Any] = list(params.get("senders") or [])
    senders = ", ".join(str(s) for s in given) or "(non précisé)"
    return f"Je peux {what} ces {len(cards)} mail(s) ? Expéditeurs : {senders}"


def _when(value: object) -> str:
    try:
        return f"{datetime.fromisoformat(str(value)):%d/%m %H:%M}"
    except ValueError:
        return str(value)


def _contact_message(params: dict[str, Any]) -> str:
    return f"Avant que je l'envoie à {params.get('contact')} : « {params.get('text')} »"


def _call_someone(params: dict[str, Any]) -> str:
    return (
        f"Avant que je le fasse : appeler {params.get('name')} au {params.get('number')}.\n"
        f"Objectif : {params.get('goal')}"
    )


def refused(reason: str, status: int = 403) -> JSONResponse:
    return JSONResponse({"error": f"guardian: {reason}"}, status_code=status)


class GuardianService:
    def __init__(
        self,
        config: OrionConfig,
        ledger: Ledger,
        inkbox: InkboxClient | None,
        *,
        fetch_calendar: CalendarFetch | None = None,
        fetch_mail: MailFetch | None = None,
        clock: Callable[[], datetime] | None = None,
        write_calendar: CalendarWrite | None = None,
        send_mail: MailSend | None = None,
    ) -> None:
        self.config = config
        self.ledger = ledger
        self._inkbox = inkbox
        self._owner = normalize_number(config.channels.imessage.owner_number)
        self._handle = config.channels.imessage.identity
        self._identity_id: str | None = None
        self._zone = ZoneInfo(config.agent.timezone)
        self.clock = clock or (lambda: datetime.now(self._zone))
        self._http: httpx.AsyncClient | None = None
        self._fetch_calendar = fetch_calendar or self._http_calendar
        self._fetch_mail = fetch_mail or imap_fetch
        self._write_calendar = write_calendar or self._http_calendar_write
        self._send_mail = send_mail or smtp_send
        self._sends: deque[float] = deque()
        secretary = SecretarySettings.model_validate(config.modules.get("secretary", {}))
        self.contacts: dict[str, Contact] = (
            {normalize_number(c.number): c for c in secretary.contacts} if secretary.enabled else {}
        )
        """The people the owner lets write to Orion (D54), by number."""
        self._contact_cap = secretary.max_replies_per_day
        self._contact_sends: dict[tuple[str, date], int] = {}
        self.monotonic: Callable[[], float] = time.monotonic
        self._last_beat = self.monotonic()
        """Since the guardian started, until the core's first heartbeat."""
        self._core_silent = False
        self.renderers: dict[str, Renderer] = {
            "call_someone": _call_someone,
            "contact_message": _contact_message,
            "calendar_add": _calendar_add,
            "mail_send": _mail_send,
            "mail_tidy": _mail_tidy,
            "autonomy_grant": _autonomy_grant,
            "computer_task_send": _computer_task_send,
        }
        self.telegram: TelegramGate | None = None
        """Telegram through the guardian (D89), installed by `orion.guardian.telegram`."""
        self.routes: list[BaseRoute] = []
        """Extra routes for the core (federation adds its own)."""
        self.listeners: list[tuple[str, Starlette, str, int]] = []
        """Public listeners: (name, app, host, port). Federation adds its own."""

    async def start(self) -> None:
        await self.ledger.catch_up()
        if self._inkbox is None:
            return
        if not self._handle or not self._owner:
            raise GuardianError("channels.imessage needs `identity` and `owner_number`")
        identity = await self._inkbox.identity(self._handle)
        if identity is None:
            raise GuardianError(
                f"Inkbox identity {self._handle!r} not found: run `orion inkbox setup`"
            )
        self._identity_id = identity.id

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
        if self._inkbox is not None:
            await self._inkbox.aclose()

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/v1/health", self._health, methods=["GET"]),
                Route("/v1/heartbeat", self._heartbeat, methods=["POST"]),
                Route(
                    "/v1/inkbox/api/v1/{path:path}",
                    self._inkbox_proxy,
                    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                ),
                Route("/v1/approvals", self._approval, methods=["POST"]),
                Route("/v1/autonomy", self._autonomy, methods=["GET"]),
                Route("/v1/autonomy/grant", self._autonomy_grant, methods=["POST"]),
                Route("/v1/autonomy/revoke", self._autonomy_revoke, methods=["POST"]),
                Route("/v1/sessions/{name}/lend", self._lend_session, methods=["POST"]),
                Route("/v1/inkbox-webhook/verify", self._verify_webhook, methods=["POST"]),
                Route("/v1/calendar/{account}", self._calendar, methods=["GET"]),
                Route("/v1/calendar/{account}/events", self._calendar_add, methods=["POST"]),
                Route("/v1/mail/{account}", self._mail, methods=["POST"]),
                Route("/v1/mail/{account}/send", self._mail_send, methods=["POST"]),
                Route("/v1/mail/{account}/tidy", self._mail_tidy, methods=["POST"]),
                Route("/v1/mail/{account}/draft", self._mail_draft, methods=["POST"]),
                Route("/v1/contacts/send", self._contact_send, methods=["POST"]),
                *self.routes,
            ]
        )

    async def deny(self, what: str, reason: str) -> JSONResponse:
        log.warning("guardian.denied", what=what, reason=reason)
        await self.ledger.record(DENIED, {"what": what, "reason": reason})
        return refused(reason)

    async def _health(self, _request: Request) -> Response:
        return JSONResponse(
            {
                "ok": True,
                "instance": self.config.instance,
                "inkbox": self._inkbox is not None,
                "telegram": self.telegram is not None,
            }
        )

    # -- watching the core ----------------------------------------------------------------------

    async def _heartbeat(self, _request: Request) -> Response:
        self._last_beat = self.monotonic()
        if self._core_silent:
            self._core_silent = False
            await self.ledger.record(CORE_BACK, {})
            await self._tell_owner("core-back", "C'est reparti, je suis de nouveau là.")
        return JSONResponse({"ok": True})

    async def check_core(self) -> None:
        """Text the owner once when the core stops beating: it cannot say so itself."""
        limit = self.config.guardian.core_silence_alert_seconds
        silent_for = self.monotonic() - self._last_beat
        if not limit or self._core_silent or silent_for < limit:
            return
        self._core_silent = True
        log.warning("guardian.core_silent", seconds=round(silent_for))
        await self.ledger.record(CORE_SILENT, {"seconds": round(silent_for)})
        await self._tell_owner(
            "core-silent",
            f"Je ne réponds plus depuis {round(silent_for / 60)} min : mon cœur est arrêté ou "
            "bloqué. Mes messages ne partiront pas tant qu'il ne repart pas. Regarde les logs du "
            "serveur (`journalctl -u orion-core`).",
        )

    async def watch_core(self, every: float = 30.0) -> None:
        while True:
            await asyncio.sleep(every)
            await self.check_core()

    async def _tell_owner(self, what: str, text: str) -> None:
        await self.ledger.catch_up()
        conversations = sorted(self.ledger.state.owner_conversations)
        if (self._inkbox is None or not conversations) and self.telegram is not None:
            try:
                await self.telegram.tell(text)
            except TelegramError:
                log.exception("guardian.tell_owner_failed", what=what, channel="telegram")
            return
        if self._inkbox is None or not conversations:
            log.warning("guardian.owner_unreachable", what=what)
            return
        try:
            await self._inkbox.send(conversations[0], text, f"{what}-{new_id()}", self._identity_id)
        except InkboxError:
            log.exception("guardian.tell_owner_failed", what=what)

    # -- Inkbox -------------------------------------------------------------------------------

    async def _inkbox_proxy(self, request: Request) -> Response:
        if self._inkbox is None:
            return refused("Inkbox is not configured on the guardian", 503)
        path = "/" + str(request.path_params["path"])
        params = list(request.query_params.multi_items())
        content = await request.body()
        if request.method == "POST" and path == "/imessage/media":
            # A file for Orion to send: uploading sends nothing; what goes out, and to whom, is
            # the message that names it, checked below like any other.
            if len(content) > MAX_MEDIA_BYTES:
                return await self.deny("POST /imessage/media", "file too large")
            return await self._forward(request, path, [], content)
        try:
            loaded: Any = json.loads(content) if content else {}
        except json.JSONDecodeError:
            return await self.deny(f"{request.method} {path}", "the body is not JSON")
        body: dict[str, Any] = cast(dict[str, Any], loaded) if isinstance(loaded, dict) else {}
        reason = await self._inkbox_policy(
            request.method, path, dict(params), body, request.headers.get(GRANT_HEADER)
        )
        if reason is not None:
            return await self.deny(f"{request.method} {path}", reason)
        return await self._forward(request, path, params, content)

    async def _forward(
        self, request: Request, path: str, params: list[tuple[str, str]], content: bytes
    ) -> Response:
        if self._inkbox is None:  # pragma: no cover - checked by the caller
            return refused("Inkbox is not configured on the guardian", 503)
        headers = {
            name: request.headers[name]
            for name in ("content-type", "idempotency-key", "prefer")
            if name in request.headers
        }
        try:
            response = await self._inkbox.forward(request.method, path, params, content, headers)
        except InkboxError as exc:
            log.warning("guardian.inkbox_unreachable", path=path, error=str(exc))
            return refused(f"Inkbox unreachable: {exc}", 502)
        return Response(
            response.content,
            status_code=response.status_code,
            media_type=response.headers.get("content-type"),
        )

    async def _inkbox_policy(
        self,
        method: str,
        path: str,
        params: dict[str, str],
        body: dict[str, Any],
        grant: str | None,
    ) -> str | None:
        """None when the request may go; otherwise why not."""
        for scoped in (params.get("agent_identity_id"), body.get("agent_identity_id")):
            if scoped is not None and scoped != self._identity_id:
                return "requests act for Orion's own identity only"
        if method == "GET":
            if path == f"/identities/{self._handle}" or any(
                pattern.fullmatch(path) for pattern in _ALWAYS_READABLE
            ):
                return None
            return f"reading {path} is not allowed"
        if method == "POST" and path in _TO_OWNER:
            conversation = str(body.get("conversation_id", ""))
            if not await self.is_owner_conversation(conversation):
                return await self._to_contact(
                    path, conversation, body_media=bool(body.get("media_urls"))
                )
            if path == "/imessage/messages" and not self.may_send():
                return "too many messages this minute"
            return None
        if method == "PUT" and path == "/phone/hosted-agent-config":
            return None
        if method == "POST" and path == "/phone/place-call":
            number = normalize_number(str(body.get("to_number", "")))
            streamed = body.get("client_websocket_url")
            if streamed is not None and streamed != self.config.voice.live_url:
                return "a call is streamed only to Orion's own live voice"
            if number and number == self._owner:
                return None
            if streamed is not None or body.get("mode") != "hosted_agent":
                return "only the owner's calls are led live"
            reason = str(body.get("reason", ""))
            return await self.use_grant(
                grant,
                "call_someone",
                lambda p: _same_number(p, number) and _mentions(reason, p),
                consume=True,
            )
        if (
            method == "POST"
            and path == f"/identities/{self._handle}/phone-contact-rules"
            and body.get("direction") == "outbound"
            and body.get("action") == "allow"
        ):
            number = normalize_number(str(body.get("match_target", "")))
            return await self.use_grant(
                grant, "call_someone", lambda p: _same_number(p, number), consume=False
            )
        return f"{method} {path} is not allowed from the core"

    async def _to_contact(
        self, path: str, conversation: str, starting: bool = False, body_media: bool = False
    ) -> str | None:
        """A contact's conversation (D54): Orion only answers, within hours of their last
        message (`starting` lifts that, for a message the owner approved), a few times a day."""
        contact = await self.contact_of(conversation)
        if contact is None:
            return "that conversation is not the owner's, nor a contact's"
        if path != "/imessage/messages":
            return None
        if body_media:
            return "Orion sends files to the owner only"
        if not starting and not contact[1]:
            return f"{contact[0].name} has not written to Orion in the last hours"
        key = (normalize_number(contact[0].number), self.clock().date())
        if self._contact_sends.get(key, 0) >= self._contact_cap:
            return f"too many messages to {contact[0].name} today"
        if not self.may_send():
            return "too many messages this minute"
        self._contact_sends[key] = self._contact_sends.get(key, 0) + 1
        return None

    async def contact_of(self, conversation_id: str) -> tuple[Contact, bool] | None:
        """The contact whose one-to-one conversation this is (only they wrote in it), and
        whether they wrote in the last `REPLY_WINDOW_HOURS`. Checked with Inkbox each time:
        the conversation's messages, not the core, say who it is with."""
        if not self.contacts or not conversation_id or self._inkbox is None:
            return None
        messages = await self._inkbox.recent(conversation_id, self._identity_id, limit=50)
        inbound = [m for m in messages if m.direction == "inbound"]
        senders = {normalize_number(m.sender or "") for m in inbound}
        if any(m.is_group for m in messages) or len(senders) != 1:
            return None
        contact = self.contacts.get(senders.pop())
        if contact is None:
            return None
        window = timedelta(hours=REPLY_WINDOW_HOURS)
        recent = any(self.clock() - datetime.fromisoformat(m.created_at) < window for m in inbound)
        return contact, recent

    async def _contact_send(self, request: Request) -> Response:
        """A message the owner approved for a contact (`contact_message`): it may start a
        conversation, so it needs their yes to this very text."""
        body = await _body(request)
        name, text = str(body.get("contact", "")), str(body.get("text", ""))
        conversation = str(body.get("conversation_id", ""))
        reason = await self.use_grant(
            request.headers.get(GRANT_HEADER),
            "contact_message",
            lambda p: p.get("contact") == name and p.get("text") == text,
            consume=True,
        )
        if reason is None:
            contact = await self.contact_of(conversation)
            if contact is None or contact[0].name != name:
                reason = f"that conversation is not {name}'s"
            else:
                reason = await self._to_contact("/imessage/messages", conversation, starting=True)
        if reason is not None:
            return await self.deny(f"message to {name}", reason)
        if self._inkbox is None:  # pragma: no cover - contact_of needs Inkbox
            return refused("iMessage is not configured", 503)
        sent = await self._inkbox.send(conversation, text, f"contact-{new_id()}", self._identity_id)
        return JSONResponse({"message_id": sent.id})

    def may_send(self) -> bool:
        """A message to the owner fits in `max_sends_per_minute` (and is counted)."""
        now = time.monotonic()
        while self._sends and now - self._sends[0] > 60:
            self._sends.popleft()
        if len(self._sends) >= self.config.guardian.max_sends_per_minute:
            return False
        self._sends.append(now)
        return True

    async def is_owner_conversation(self, conversation_id: str) -> bool:
        """Checked with Inkbox once, then remembered: a one-to-one conversation in which the
        owner's number wrote to Orion."""
        await self.ledger.catch_up()
        if conversation_id in self.ledger.state.owner_conversations:
            return True
        if not conversation_id or self._inkbox is None:
            return False
        messages = await self._inkbox.recent(conversation_id, self._identity_id, limit=50)
        if any(m.is_group for m in messages) or not any(
            m.direction == "inbound" and normalize_number(m.sender or "") == self._owner
            for m in messages
        ):
            return False
        await self.ledger.record(OWNER_CONVERSATION, {"conversation_id": conversation_id})
        log.info("guardian.owner_conversation", conversation=conversation_id)
        return True

    # -- approvals ----------------------------------------------------------------------------

    async def _approval(self, request: Request) -> Response:
        try:
            asked = ApprovalRequest.model_validate_json(await request.body())
        except ValidationError:
            return refused("malformed approval request", 400)
        approval_id = f"g_{new_id()}"
        render = self.renderers.get(asked.action, _generic(asked.action))
        question = (
            render(asked.params)
            + "\n"
            + (f"Je demande parce que {asked.reason}.\n" if asked.reason else "")
        )
        if asked.conversation_id.startswith("telegram:"):
            if self.telegram is None:
                return refused("Telegram is not configured on the guardian", 503)
            if asked.conversation_id != self.telegram.conversation:
                return await self.deny("approval", "that chat is not the owner's")
            try:
                message_id = str(await self.telegram.ask(question + "Oui ou non ?"))
            except TelegramError as exc:
                return refused(f"Telegram unreachable: {exc}", 502)
        else:
            if self._inkbox is None:
                return refused("approvals go through iMessage, which is not configured", 503)
            if not await self.is_owner_conversation(asked.conversation_id):
                return await self.deny("approval", "that conversation is not the owner's")
            text = question + "👍 pour oui, 👎 pour non (ou réponds oui / non)."
            sent = await self._inkbox.send(
                asked.conversation_id, text, approval_id, self._identity_id
            )
            message_id = sent.id
        expires = self.clock() + timedelta(seconds=self.config.guardian.approval_ttl_seconds)
        await self.ledger.record(
            APPROVAL_REQUESTED,
            {
                "approval_id": approval_id,
                "action": asked.action,
                "params": asked.params,
                "message_id": message_id,
                "conversation_id": asked.conversation_id,
                "expires_at": expires.isoformat(),
            },
        )
        return JSONResponse({"approval_id": approval_id, "message_id": message_id})

    # -- autonomy -----------------------------------------------------------------------------

    async def _autonomy(self, _request: Request) -> Response:
        await self.ledger.catch_up()
        return JSONResponse({"autonomous": sorted(self.ledger.state.autonomous)})

    async def _autonomy_grant(self, request: Request) -> Response:
        body = await _body(request)
        action = str(body.get("action", ""))
        if action not in self.config.guardian.autonomable:
            return await self.deny("autonomy", f"{action} can never be done without asking")
        try:
            limits = Limits.model_validate(body.get("limits") or {})
        except ValidationError:
            return refused("malformed limits", 400)
        unbounded = missing(action, limits)
        if unbounded is not None:
            return await self.deny("autonomy", unbounded)
        reason = await self.use_grant(
            request.headers.get(GRANT_HEADER),
            "autonomy_grant",
            lambda p: p.get("action") == action and _limits(p) == limits,
            consume=True,
        )
        if reason is not None:
            return await self.deny("autonomy", reason)
        await self.ledger.record(
            AUTONOMY_GRANTED, {"action": action, "limits": limits.model_dump()}
        )
        log.info("guardian.autonomy_granted", action=action, limits=limits.describe())
        return JSONResponse({"action": action})

    # -- logged-in sessions ---------------------------------------------------------------------

    async def _lend_session(self, request: Request) -> Response:
        """A site session (cookies) for one task the owner approved, or did alone within
        limits that cover every site of the session. Lent once per approval."""
        name = str(request.path_params["name"])
        params = await _body(request)
        await self.ledger.catch_up()
        domains = self.ledger.state.sessions.get(name)
        path = self.config.guardian.data_dir / "sessions" / f"{name}.json"
        if domains is None or not path.is_file():
            return refused(f"no session {name!r}", 404)
        grant = request.headers.get(GRANT_HEADER)
        if grant:
            reason = await self.use_grant(
                grant,
                "computer_task_send",
                lambda p: p.get("session") == name and p.get("brief") == params.get("brief"),
                consume=True,
            )
            if reason is not None:
                return await self.deny(f"session {name}", reason)
            how = grant
        else:
            bounds = self.ledger.state.limits.get("computer_task_send") or {}
            covered = all(host_in(d, list(bounds.get("domains") or [])) for d in domains)
            if not covered or not await self.autonomous("computer_task_send", params):
                return await self.deny(f"session {name}", "computer_task_send needs approval")
            how = "alone"
        state: Any = json.loads(path.read_text(encoding="utf-8"))
        await self.ledger.record(SESSION_LENT, {"name": name, "how": how})
        log.info("guardian.session_lent", name=name, how=how)
        return JSONResponse({"state": state, "domains": domains})

    async def _autonomy_revoke(self, request: Request) -> Response:
        """Taking autonomy back never needs asking."""
        action = str((await _body(request)).get("action", ""))
        await self.ledger.record(AUTONOMY_REVOKED, {"action": action})
        return JSONResponse({"action": action})

    async def autonomous(self, action: str, params: dict[str, Any] | None = None) -> bool:
        """The owner let Orion do this alone, within the limits they set, and today's
        allowance is not spent."""
        await self.ledger.catch_up()
        state = self.ledger.state
        if action not in state.autonomous or action not in self.config.guardian.autonomable:
            return False
        bounds = state.limits.get(action)
        if not within(action, Limits.model_validate(bounds) if bounds else None, params or {}):
            return False
        day = self.clock().date().isoformat()
        if state.autonomous_days.get(day, 0) >= self.config.guardian.max_autonomous_per_day:
            return False
        await self.ledger.record(AUTONOMOUS, {"action": action, "day": day})
        return True

    async def use_grant(
        self, grant: str | None, action: str, matches: Match, consume: bool
    ) -> str | None:
        """None when `grant` is the owner's yes to this very request; otherwise why not."""
        if not grant:
            return f"{action} needs the owner's approval"
        await self.ledger.catch_up()
        approval = self.ledger.state.approvals.get(grant)
        if approval is None or approval.action != action:
            return f"no approval {grant} for {action}"
        if approval.used:
            return "that approval was already used"
        if self.clock() >= approval.expires_at:
            return "that approval has expired"
        if not matches(approval.params):
            return "the request is not what the owner approved"
        if not await self._owner_said_yes(approval):
            return "the owner has not said yes"
        if consume:
            await self.ledger.record(APPROVAL_USED, {"approval_id": approval.id})
        return None

    async def _owner_said_yes(self, approval: PendingApproval) -> bool:
        if approval.conversation_id.startswith("telegram:"):
            return self.telegram is not None and await self.telegram.owner_said_yes(approval)
        if self._inkbox is None:
            return False
        message = await self._inkbox.message(approval.message_id, self._identity_id)
        votes = {r.reaction for r in message.reactions or [] if r.direction == "inbound"}
        if votes & REACTION_NO:
            return False
        if votes & REACTION_YES:
            return True
        recent = await self._inkbox.recent(approval.conversation_id, self._identity_id, limit=50)
        newer: list[str] = []
        for m in recent:  # newest first, down to the approval message
            if m.id == approval.message_id:
                break
            if m.direction == "inbound" and normalize_number(m.sender or "") == self._owner:
                newer.append(m.content or "")
        for text in reversed(newer):  # the first answer after the question decides
            answer = answer_word(text)
            if answer is not None:
                return answer
        return False

    async def _verify_webhook(self, request: Request) -> Response:
        try:
            key = self.config.channels.imessage.signing_key.resolve().get_secret_value()
        except LookupError:
            log.exception("guardian.no_signing_key")
            return JSONResponse({"valid": False})
        valid = verify_signature(
            key,
            request.headers.get("x-inkbox-request-id", ""),
            request.headers.get("x-inkbox-timestamp", ""),
            request.headers.get("x-inkbox-signature", ""),
            await request.body(),
        )
        return JSONResponse({"valid": valid})

    # -- calendars and mail -------------------------------------------------------------------

    async def _http_calendar(self, account: CalendarAccount) -> str:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=20.0, follow_redirects=True)
        return await fetch_feed(self._http, account)

    async def _calendar(self, request: Request) -> Response:
        name = str(request.path_params["account"])
        settings = CalendarSettings.model_validate(self.config.modules.get("calendar", {}))
        account = next((a for a in settings.accounts if a.name == name), None)
        if account is None:
            return refused(f"no calendar account {name!r}", 404)
        try:
            feed = await self._fetch_calendar(account)
        except (LookupError, httpx.HTTPError) as exc:
            log.warning("guardian.calendar_failed", account=name, error=str(exc))
            return refused(f"calendar {name!r} unreadable: {exc}", 502)
        return PlainTextResponse(feed, media_type="text/calendar")

    async def _http_calendar_write(self, account: CalendarAccount, event: NewCalendarEvent) -> str:
        if account.write is None:  # pragma: no cover - checked by the caller
            raise CalendarWriteError(f"calendar {account.name!r} cannot be written to")
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=20.0, follow_redirects=True)
        return await write_event(self._http, account.write, event)

    def _confirmed_meeting(self, event: NewCalendarEvent) -> bool:
        """A meeting both Orions signed for this very slot (D29), not written yet."""
        state = self.ledger.state
        if event.meeting is None or event.meeting in state.written_meetings:
            return False
        for exchanged in state.exchanged:
            envelope = exchanged.envelope
            if envelope.get("type") == CONFIRM and envelope.get("context") == event.meeting:
                try:
                    slot = datetime.fromisoformat(str(envelope["body"]["slot"]))
                except (KeyError, TypeError, ValueError):
                    continue
                if slot == event.start:
                    return True
        return False

    async def _calendar_add(self, request: Request) -> Response:
        name = str(request.path_params["account"])
        settings = CalendarSettings.model_validate(self.config.modules.get("calendar", {}))
        account = next((a for a in settings.accounts if a.name == name), None)
        if account is None or account.write is None:
            return refused(f"calendar {name!r} cannot be written to", 404)
        try:
            event = NewCalendarEvent.model_validate_json(await request.body())
        except ValidationError:
            return refused("malformed calendar event", 400)
        await self.ledger.catch_up()
        grant = request.headers.get(GRANT_HEADER)
        alone = not grant and await self.autonomous("calendar_add")
        if not self._confirmed_meeting(event) and not alone:
            reason = await self.use_grant(
                request.headers.get(GRANT_HEADER),
                "calendar_add",
                lambda p: _same_event(p, event, self._zone),
                consume=True,
            )
            if reason is not None:
                return await self.deny(f"calendar {name}", reason)
        try:
            uid = await self._write_calendar(account, event)
        except (LookupError, CalendarWriteError) as exc:
            log.warning("guardian.calendar_write_failed", account=name, error=str(exc))
            return refused(f"calendar {name!r} not written: {exc}", 502)
        await self.ledger.record(
            WRITTEN,
            {"account": name, "uid": uid, **({"meeting": event.meeting} if event.meeting else {})},
        )
        return JSONResponse({"uid": uid})

    async def _mail_send(self, request: Request) -> Response:
        name = str(request.path_params["account"])
        settings = MailSettings.model_validate(self.config.modules.get("mail", {}))
        account = next((a for a in settings.accounts if a.name == name), None)
        if account is None or account.smtp_host is None:
            return refused(f"mail account {name!r} cannot send", 404)
        try:
            mail = Outgoing.model_validate_json(await request.body())
        except ValidationError:
            return refused("malformed mail", 400)
        grant = request.headers.get(GRANT_HEADER)
        if grant or not await self.autonomous("mail_send", mail.model_dump()):
            reason = await self.use_grant(
                grant, "mail_send", lambda p: _same_mail(p, mail), consume=True
            )
            if reason is not None:
                return await self.deny(f"mail {name}", reason)
        try:
            message_id = await self._send_mail(account, mail, None)
        except (LookupError, SendError) as exc:
            log.warning("guardian.mail_send_failed", account=name, error=str(exc))
            return refused(f"mail not sent: {exc}", 502)
        await self.ledger.record(
            MAIL_SENT, {"account": name, "to": mail.recipients(), "message_id": message_id}
        )
        return JSONResponse({"message_id": message_id})

    async def _mail_tidy(self, request: Request) -> Response:
        """Archiving or unsubscribing, by uid: the guardian reads the unsubscribe addresses
        from the server itself. Approved as a batch (the number of mails must match), or done
        alone within an autonomy the owner granted."""
        from orion.mail.tidy import local_tidy

        name = str(request.path_params["account"])
        settings = MailSettings.model_validate(self.config.modules.get("mail", {}))
        account = next((a for a in settings.accounts if a.name == name), None)
        if account is None:
            return refused(f"no mail account {name!r}", 404)
        params = await _body(request)
        action = str(params.get("action"))
        raw: list[Any] = list(params.get("uids") or [])
        uids = [int(u) for u in raw]
        if action not in ("archive", "unsubscribe") or not uids:
            return refused("malformed tidy request", 400)
        grant = request.headers.get(GRANT_HEADER)
        if grant or not await self.autonomous("mail_tidy", {"action": action}):
            reason = await self.use_grant(
                grant,
                "mail_tidy",
                lambda p: p.get("action") == action and len(p.get("cards") or []) >= len(uids),
                consume=True,
            )
            if reason is not None:
                return await self.deny(f"mail tidy {name}", reason)
        outcome = await local_tidy(account, cast(Any, action), uids)
        log.info("guardian.mail_tidied", account=name, action=action, done=outcome.done)
        return JSONResponse(outcome.payload())

    async def _mail_draft(self, request: Request) -> Response:
        from orion.mail.tidy import local_draft

        name = str(request.path_params["account"])
        settings = MailSettings.model_validate(self.config.modules.get("mail", {}))
        account = next((a for a in settings.accounts if a.name == name), None)
        if account is None or not account.drafts_folder:
            return refused(f"mail account {name!r} has no drafts folder", 404)
        try:
            mail = Outgoing.model_validate_json(await request.body())
        except ValidationError:
            return refused("malformed draft", 400)
        try:
            await local_draft(account, mail)
        except (LookupError, OSError, imaplib.IMAP4.error, SendError) as exc:
            log.warning("guardian.draft_failed", account=name, error=repr(exc))
            return refused(f"draft not written: {exc}", 502)
        return JSONResponse({"ok": True})

    async def _mail(self, request: Request) -> Response:
        name = str(request.path_params["account"])
        settings = MailSettings.model_validate(self.config.modules.get("mail", {}))
        account = next((a for a in settings.accounts if a.name == name), None)
        if account is None:
            return refused(f"no mail account {name!r}", 404)
        try:
            cursor = mail_request(await request.body())
        except ValidationError:
            return refused("malformed mail request", 400)
        try:
            batch = await self._fetch_mail(
                account,
                cursor.uidvalidity,
                cursor.last_uid,
                cursor.lookback_days,
                cursor.limit,
                cursor.now,
            )
        except (LookupError, OSError, TimeoutError, imaplib.IMAP4.error) as exc:
            log.warning("guardian.mail_failed", account=name, error=repr(exc))
            return refused(f"mailbox {name!r} unreadable: {exc!r}", 502)
        return JSONResponse(batch_to_json(batch))


def _same_event(params: dict[str, Any], event: NewCalendarEvent, zone: ZoneInfo) -> bool:
    def at(value: object) -> datetime | None:
        try:
            moment = datetime.fromisoformat(str(value))
        except ValueError:
            return None
        return moment.replace(tzinfo=zone) if moment.tzinfo is None else moment

    return (
        str(params.get("title")) == event.title
        and str(params.get("location", "")) == event.location
        and str(params.get("notes", "")) == event.notes
        and at(params.get("start")) == event.start
        and at(params.get("end")) == event.end
    )


def _same_mail(params: dict[str, Any], mail: Outgoing) -> bool:
    return (
        list(params.get("to", [])) == mail.to
        and list(params.get("cc", [])) == mail.cc
        and str(params.get("subject")) == mail.subject
        and str(params.get("body")) == mail.body
        and list(params.get("attachments") or []) == [a.file_id for a in mail.attachments]
    )


def _same_number(params: dict[str, Any], number: str) -> bool:
    return bool(number) and normalize_number(str(params.get("number", ""))) == number


def _mentions(reason: str, params: dict[str, Any]) -> bool:
    """The call's brief carries the approved name and goal word for word."""
    return str(params.get("name", "")) in reason and str(params.get("goal", "")) in reason


__all__ = ["GuardianError", "GuardianService", "MailAccount", "refused"]
