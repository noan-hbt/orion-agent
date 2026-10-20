"""The core's side of the guardian process (D28).

When Orion runs split, `orion serve` holds no secret but the LLM key: it reaches Inkbox,
calendars and mailboxes through the guardian, over a Unix socket. The Inkbox client is the
same as in a single process, pointed at the guardian, which adds the key once its rules allow
the request.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import httpx

from orion.calendar.service import CalendarAccount
from orion.channels.inkbox import GRANT_HEADER, InkboxClient
from orion.channels.telegram import TelegramClient
from orion.guardian.wire import ApprovalRequest, MailRequest, batch_from_json
from orion.mail.imap import Batch
from orion.mail.service import MailAccount

BASE_URL = "http://guardian"

TransportFactory = Callable[[], httpx.AsyncBaseTransport]


class GuardianUnavailableError(ConnectionError):
    """The guardian process cannot be reached, or refused the request."""


class GuardianClient:
    def __init__(self, transports: TransportFactory, timeout: float = 60.0) -> None:
        self._transports = transports
        self._http = httpx.AsyncClient(base_url=BASE_URL, transport=transports(), timeout=timeout)

    @classmethod
    def over_socket(cls, path: Path) -> GuardianClient:
        return cls(lambda: httpx.AsyncHTTPTransport(uds=str(path)))

    async def aclose(self) -> None:
        await self._http.aclose()

    async def health(self) -> dict[str, Any]:
        try:
            response = await self._http.get("/v1/health")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise GuardianUnavailableError(f"guardian unreachable: {exc!r}") from exc
        return response.json()

    async def heartbeat(self) -> None:
        """The core is alive: the guardian texts the owner when these stop (it can't)."""
        await self.post("/v1/heartbeat", {})

    def inkbox(self, max_attempts: int = 3) -> InkboxClient:
        """An Inkbox client without a key: the guardian adds it."""
        return InkboxClient(
            "held-by-the-guardian",
            f"{BASE_URL}/v1/inkbox",
            transport=self._transports(),
            max_attempts=max_attempts,
        )

    def telegram(self) -> TelegramClient:
        """A Telegram client without the bot's token: the guardian adds it (D89)."""
        return GuardedTelegramClient(f"{BASE_URL}/v1/telegram", self._transports())

    async def telegram_login(self, init_data: str) -> int | None:
        """Who opened the console from Telegram: the guardian checks the signature (D91)."""
        try:
            response = await self._http.post("/v1/telegram-login", content=init_data.encode())
        except httpx.TransportError as exc:
            raise GuardianUnavailableError(f"guardian unreachable: {exc!r}") from exc
        found = _json(response).get("user_id")
        return found if isinstance(found, int) else None

    async def request_approval(
        self, action: str, params: dict[str, Any], reason: str, conversation_id: str
    ) -> tuple[str, str]:
        """The guardian asks the owner itself. Returns (approval id, message id)."""
        body = ApprovalRequest(
            action=action, params=params, reason=reason[:400], conversation_id=conversation_id
        )
        data = await self.post("/v1/approvals", body.model_dump(mode="json"))
        return str(data["approval_id"]), str(data["message_id"])

    async def verify_webhook(
        self, request_id: str, timestamp: str, signature: str, body: bytes
    ) -> bool:
        response = await self._http.post(
            "/v1/inkbox-webhook/verify",
            content=body,
            headers={
                "x-inkbox-request-id": request_id,
                "x-inkbox-timestamp": timestamp,
                "x-inkbox-signature": signature,
            },
        )
        return response.status_code == 200 and bool(response.json().get("valid"))

    async def calendar(self, account: CalendarAccount) -> str:
        response = await self._http.get(f"/v1/calendar/{account.name}")
        response.raise_for_status()
        return response.text

    async def calendar_add(self, account: str, event: Any, grant: str | None) -> str:
        """Written by the guardian, if the owner agreed (or it is a meeting both Orions
        confirmed). Returns the event's UID."""
        data = await self.post(
            f"/v1/calendar/{account}/events",
            event.model_dump(mode="json"),
            headers={GRANT_HEADER: grant} if grant else None,
        )
        return str(data["uid"])

    async def mail(
        self,
        account: MailAccount,
        uidvalidity: int,
        last_uid: int,
        lookback_days: int,
        limit: int,
        now: datetime,
    ) -> Batch:
        cursor = MailRequest(
            uidvalidity=uidvalidity,
            last_uid=last_uid,
            lookback_days=lookback_days,
            limit=limit,
            now=now,
        )
        return batch_from_json(
            await self.post(f"/v1/mail/{account.name}", cursor.model_dump(mode="json"))
        )

    async def mail_send(self, account: MailAccount, mail: Any, grant: str | None) -> str:
        """Sent by the guardian, if the owner approved this very mail. Returns its Message-ID."""
        data = await self.post(
            f"/v1/mail/{account.name}/send",
            mail.model_dump(mode="json"),
            headers={GRANT_HEADER: grant} if grant else None,
        )
        return str(data["message_id"])

    async def mail_tidy(
        self, account: MailAccount, action: str, uids: list[int], grant: str | None
    ) -> Any:
        """Archived or unsubscribed by the guardian, if the owner approved this batch."""
        from orion.mail.tidy import Tidied

        data = await self.post(
            f"/v1/mail/{account.name}/tidy",
            {"action": action, "uids": uids},
            headers={GRANT_HEADER: grant} if grant else None,
        )
        return Tidied.from_payload(data)

    async def mail_draft(self, account: MailAccount, mail: Any) -> None:
        """A draft in the owner's drafts folder: it never leaves, so no approval."""
        await self.post(f"/v1/mail/{account.name}/draft", mail.model_dump(mode="json"))

    async def contact_send(
        self, contact: str, conversation_id: str, text: str, grant: str | None
    ) -> str:
        """A message the owner approved for one of their contacts (D54)."""
        data = await self.post(
            "/v1/contacts/send",
            {"contact": contact, "conversation_id": conversation_id, "text": text},
            headers={GRANT_HEADER: grant} if grant else None,
        )
        return str(data["message_id"])

    async def lend_session(
        self, name: str, params: dict[str, Any], grant: str | None
    ) -> dict[str, Any]:
        """A logged-in site session for one approved task (cookies: never stored by the
        core). Returns the browser storage state."""
        data = await self.post(
            f"/v1/sessions/{name}/lend", params, headers={GRANT_HEADER: grant} if grant else None
        )
        return dict(data["state"])

    # -- federation (D29) ----------------------------------------------------------------------

    async def peers(self) -> list[dict[str, Any]]:
        return list(await self.get("/v1/federation/peers"))

    async def federation_inbox(self, after: int) -> list[dict[str, Any]]:
        data = await self.get("/v1/federation/inbox", {"after": after})
        return list(data["items"])

    async def federation_send(
        self,
        peer: str,
        kind: str,
        context: str,
        body: dict[str, Any],
        grant: str | None = None,
    ) -> dict[str, Any]:
        """Signed and sent by the guardian, if the owner agreed to it (D29)."""
        return await self.post(
            "/v1/federation/send",
            {"peer": peer, "type": kind, "context": context, "body": body},
            headers={GRANT_HEADER: grant} if grant else None,
        )

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            response = await self._http.get(path, params=params)
        except httpx.TransportError as exc:
            raise GuardianUnavailableError(f"guardian unreachable: {exc!r}") from exc
        return _json(response)

    async def post(
        self, path: str, body: dict[str, Any], headers: dict[str, str] | None = None
    ) -> Any:
        try:
            response = await self._http.post(path, json=body, headers=headers)
        except httpx.TransportError as exc:
            raise GuardianUnavailableError(f"guardian unreachable: {exc!r}") from exc
        return _json(response)


class GuardedTelegramClient(TelegramClient):
    """The Bot API through the guardian: same calls, no token in the URL, and files sent as
    JSON so the guardian reads where they go before sending them itself."""

    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport) -> None:
        super().__init__("held-by-the-guardian", base_url, transport=transport)

    def method_url(self, method: str) -> str:
        return f"{self._base}/{method}"

    def file_url(self, file_path: str) -> str:
        return f"{self._base}/file/{file_path}"

    async def send_file(
        self, chat_id: int, method: str, field_name: str, name: str, data: bytes, caption: str
    ) -> int:
        file = {"name": name, "data": base64.b64encode(data).decode("ascii")}
        sent = await self.call(method, chat_id=chat_id, caption=caption or None, file=file)
        return int(cast(dict[str, Any], sent)["message_id"])


def _json(response: httpx.Response) -> Any:
    if response.status_code >= 400:
        try:
            detail = str(response.json().get("error", response.text))
        except ValueError:
            detail = response.text[:300]
        raise GuardianUnavailableError(detail)
    return response.json()
