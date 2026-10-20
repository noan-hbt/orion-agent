"""Getting into the control page: a one-time link, then a signed cookie.

The page shows the owner's whole life, so nothing gets in without a link Orion sent them
(`console_link`, or `orion console link` on the server). A link works once, for ten minutes,
and becomes a session cookie (HttpOnly, Secure, SameSite=Strict) signed with a key only the
core holds; deleting the key file logs every browser out.

Inside Telegram (D91), the console opens as a Mini App: Telegram signs who opened it, the
signature is checked with the bot's token, and only the owner's user id gets the cookie.

    console.link.created  console.link.used  console.login.telegram
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner

LINK_CREATED = "console.link.created"
LINK_USED = "console.link.used"
TELEGRAM_LOGIN = "console.login.telegram"
LINK_TTL = timedelta(minutes=10)
SESSION_DAYS = 7
COOKIE = "orion_console"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class LinkState:
    name: str = "console-links"
    links: dict[str, datetime] = field(default_factory=dict[str, datetime])
    """token hash → expiry, while unused."""
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.links.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        payload: dict[str, Any] = event.payload
        if event.kind == LINK_CREATED and not event.redacted:
            self.links[str(payload["hash"])] = datetime.fromisoformat(str(payload["expires_at"]))
        elif event.kind == LINK_USED:
            self.links.pop(str(payload["hash"]), None)


def session_key(data_dir: Path) -> bytes:
    path = data_dir / "console.key"
    if not path.exists():
        data_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(secrets.token_bytes(32))
        path.chmod(0o600)
    return path.read_bytes()


class Gate:
    def __init__(self, journal: EventStore, key: bytes) -> None:
        self._journal = journal
        self._key = key
        self.state = LinkState()
        self._runner = ProjectionRunner(journal, [self.state])

    async def new_link(self, now: datetime | None = None) -> str:
        token = secrets.token_urlsafe(24)
        expires = (now or datetime.now(UTC)) + LINK_TTL
        await self._record(LINK_CREATED, {"hash": _hash(token), "expires_at": expires.isoformat()})
        return token

    async def redeem(self, token: str, now: datetime | None = None) -> str | None:
        """A session cookie value, once, for a valid link."""
        await self._runner.catch_up()
        digest = _hash(token)
        expires = self.state.links.get(digest)
        if expires is None or (now or datetime.now(UTC)) > expires:
            return None
        await self._record(LINK_USED, {"hash": digest})
        return self.sign(time.time() + SESSION_DAYS * 86400)

    async def telegram_session(self, user_id: int) -> str:
        """A session cookie value for the owner, who opened the console from Telegram."""
        await self._record(TELEGRAM_LOGIN, {"user_id": user_id})
        return self.sign(time.time() + SESSION_DAYS * 86400)

    def sign(self, until: float) -> str:
        body = f"{int(until)}.{secrets.token_hex(8)}"
        mac = hmac.new(self._key, body.encode(), hashlib.sha256).hexdigest()
        return f"{body}.{mac}"

    def valid(self, cookie: str | None, now: float | None = None) -> bool:
        if not cookie or cookie.count(".") != 2:
            return False
        until, nonce, mac = cookie.split(".")
        expected = hmac.new(self._key, f"{until}.{nonce}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, expected):
            return False
        return until.isdigit() and int(until) > (now or time.time())

    async def _record(self, kind: str, payload: dict[str, object]) -> None:
        await self._journal.append(
            [
                NewEvent(
                    kind=kind,
                    payload=payload,
                    provenance=Provenance(source="console", actor="orion:console"),
                    trust=Trust.SYSTEM,
                )
            ]
        )
        await self._runner.catch_up()
