"""Reading a mailbox over IMAP, strictly read-only.

The folder is opened with EXAMINE and messages are fetched with BODY.PEEK, so Orion never
marks anything as read, moves or deletes it. Works with any provider that offers IMAP with an
app password (Gmail, iCloud, Fastmail, most hosts).
"""

from __future__ import annotations

import email
import email.policy
import imaplib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, cast

BODY_CHARS = 6000
_BULK = {"bulk", "list", "junk"}


@dataclass(frozen=True)
class RawMail:
    uid: int
    message_id: str
    from_addr: str
    from_name: str
    subject: str
    received_at: datetime
    body: str
    bulk: bool
    """Mailing list or bulk mail (List-Unsubscribe, Precedence): no need to read it closely."""


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    return " ".join(" ".join(parser.parts).split())


def parse_message(uid: int, raw: bytes, fallback_time: datetime) -> RawMail:
    message: EmailMessage = email.message_from_bytes(raw, policy=email.policy.default)
    name, address = parseaddr(str(message.get("From", "")))
    try:
        received = parsedate_to_datetime(str(message.get("Date", "")))
        if received.tzinfo is None:
            received = received.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        received = fallback_time
    body = ""
    part = message.get_body(preferencelist=("plain", "html"))
    if part is not None:
        try:
            content = str(part.get_content())
        except (LookupError, ValueError):
            content = ""
        body = html_to_text(content) if part.get_content_subtype() == "html" else content
    precedence = str(message.get("Precedence", "")).strip().lower()
    return RawMail(
        uid=uid,
        message_id=str(message.get("Message-ID", "")).strip() or f"uid:{uid}",
        from_addr=address.strip().lower(),
        from_name=" ".join(name.split())[:80],
        subject=" ".join(str(message.get("Subject", "")).split())[:200],
        received_at=received,
        body=" ".join(body.split())[:BODY_CHARS],
        bulk=message.get("List-Unsubscribe") is not None or precedence in _BULK,
    )


@dataclass(frozen=True)
class Batch:
    uidvalidity: int
    last_uid: int
    mails: list[RawMail]
    skipped: int = 0
    """New messages beyond the per-sync limit, not read (the oldest ones)."""


def fetch_since(
    host: str,
    port: int,
    username: str,
    password: str,
    folder: str,
    uidvalidity: int,
    last_uid: int,
    lookback_days: int,
    limit: int,
    now: datetime,
) -> Batch:
    """Messages after `last_uid` (or from the last `lookback_days` on a first sync, or when
    the server renumbered the folder), newest `limit` only. Blocking: run it in a thread."""
    with imaplib.IMAP4_SSL(host, port, timeout=30) as imap:
        imap.login(username, password)
        status, _ = imap.select(folder, readonly=True)
        if status != "OK":
            raise imaplib.IMAP4.error(f"cannot open folder {folder!r}")
        _, values = imap.response("UIDVALIDITY")
        current = int(values[0]) if values and values[0] else 0
        if current != uidvalidity or not last_uid:
            since = (now - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
            _, data = imap.uid("SEARCH", "SINCE", since)
            last_uid = 0
        else:
            _, data = imap.uid("SEARCH", "UID", f"{last_uid + 1}:*")
        found = sorted(int(u) for u in (data[0] or b"").split() if int(u) > last_uid)
        if not found:
            return Batch(current, last_uid, [])
        chosen = found[-limit:]
        mails: list[RawMail] = []
        for uid in chosen:
            _, fetched = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
            for item in fetched:
                if isinstance(item, tuple):
                    parts = cast(tuple[Any, ...], item)
                    if len(parts) > 1 and isinstance(parts[1], bytes):
                        mails.append(parse_message(uid, parts[1], now))
                        break
        return Batch(current, found[-1], mails, skipped=len(found) - len(chosen))


def search_window(
    host: str,
    port: int,
    username: str,
    password: str,
    folder: str,
    since: datetime,
    before: datetime | None,
    sender: str | None,
    limit: int,
    now: datetime,
) -> list[RawMail]:
    """Messages received between `since` and `before` (from `sender` if given), newest `limit`
    only, read live on the server however old they are (D171). Blocking: run it in a thread."""
    with imaplib.IMAP4_SSL(host, port, timeout=30) as imap:
        imap.login(username, password)
        status, _ = imap.select(folder, readonly=True)
        if status != "OK":
            raise imaplib.IMAP4.error(f"cannot open folder {folder!r}")
        criteria = ["SINCE", since.strftime("%d-%b-%Y")]
        if before is not None:
            # BEFORE is a day, exclusive: the day itself is in the window.
            criteria += ["BEFORE", (before + timedelta(days=1)).strftime("%d-%b-%Y")]
        if sender:
            criteria += ["FROM", '"' + sender.replace("\\", "").replace('"', "") + '"']
        _, data = imap.uid("SEARCH", *criteria)
        found = sorted(int(u) for u in (data[0] or b"").split())
        mails: list[RawMail] = []
        for uid in found[-limit:]:
            _, fetched = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
            for item in fetched:
                if isinstance(item, tuple):
                    parts = cast(tuple[Any, ...], item)
                    if len(parts) > 1 and isinstance(parts[1], bytes):
                        mails.append(parse_message(uid, parts[1], now))
                        break
        return mails
