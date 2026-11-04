"""Reading a mailbox over IMAP, strictly read-only.

The folder is opened with EXAMINE and messages are fetched with BODY.PEEK, so Orion never
marks anything as read, moves or deletes it. Works with any provider that offers IMAP with an
app password (Gmail, iCloud, Fastmail, most hosts).
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Literal, cast

BODY_CHARS = 6000
MAX_ATTACHMENTS = 20
_BULK = {"bulk", "list", "junk"}
SENT = "\\Sent"
ALL = "\\All"
ARCHIVE = "\\Archive"
"""Special-use folders (RFC 6154): the server says which of its folders is the sent mail or all
of it ("[Gmail]/Sent Mail", "[Gmail]/Messages envoyés", "Sent Messages"), whatever its name."""
Box = Literal["inbox", "sent", "archive"]
"""Where a mail is read: the inbox (synced), his sent mail, or his archived mail (Gmail: all of
it). Only the inbox is synced; the others are read when Orion asks (D238)."""


@dataclass(frozen=True)
class Attached:
    """A file attached to a mail: what it is, and, when asked for, its bytes."""

    name: str
    content_type: str
    size: int
    data: bytes = b""


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
    attachments: tuple[Attached, ...] = ()
    """Its attached files, without their bytes."""
    to: str = ""
    """Its recipients, as « Name <address>, … » (clipped)."""


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


def _leaves(part: EmailMessage) -> Iterator[EmailMessage]:
    if part.get_content_maintype() == "message":
        return  # a forwarded mail: its text is not a file to open
    if part.is_multipart():
        for sub in part.iter_parts():
            yield from _leaves(cast(EmailMessage, sub))
    else:
        yield part


def _files(message: EmailMessage) -> list[tuple[Attached, EmailMessage]]:
    """Every attached file, however deep (Apple Mail nests them in the alternative)."""
    body = message.get_body(preferencelist=("plain", "html"))
    found: list[tuple[Attached, EmailMessage]] = []
    for part in message.iter_parts() if message.is_multipart() else ():
        for leaf in _leaves(cast(EmailMessage, part)):
            if leaf is body:
                continue
            if leaf.get_content_disposition() != "attachment" and not leaf.get_filename():
                continue  # an inline image of the signature, or another body
            payload = leaf.get_payload(decode=True)
            data = payload if isinstance(payload, bytes) else b""
            name = " ".join(str(leaf.get_filename() or "").split())[:200] or "sans-nom"
            found.append((Attached(name, leaf.get_content_type(), len(data)), leaf))
    return found[:MAX_ATTACHMENTS]


def attachment_of(raw: bytes, index: int) -> Attached | None:
    """The mail's `index`-th attached file (from 1), with its bytes."""
    message: EmailMessage = email.message_from_bytes(raw, policy=email.policy.default)
    files = _files(message)
    if not 1 <= index <= len(files):
        return None
    info, part = files[index - 1]
    payload = part.get_payload(decode=True)
    return Attached(
        info.name, info.content_type, info.size, payload if isinstance(payload, bytes) else b""
    )


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
        attachments=tuple(info for info, _ in _files(message)),
        to=_recipients(message),
    )


def _recipients(message: EmailMessage) -> str:
    found = getaddresses([str(v) for v in message.get_all("To", []) + message.get_all("Cc", [])])
    named = [f"{' '.join(n.split())} <{a}>" if n.strip() else a for n, a in found if a]
    return ", ".join(named)[:300]


def _quoted(folder: str) -> str:
    if re.fullmatch(r"[\w./\[\]&+-]+", folder):
        return folder
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def special_folder(imap: imaplib.IMAP4, use: str) -> str:
    """The folder the server marks with that special use (`SENT`, `ALL`); for `ALL`, a server
    without one (iCloud) gives its `ARCHIVE`."""
    status, listed = imap.list()
    marked: dict[str, str] = {}
    for line in listed if status == "OK" else []:
        text = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
        found = re.match(r'\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$', text)
        if found is None:
            continue
        name = found["name"].strip()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        for flag in found["flags"].lower().split():
            marked.setdefault(flag, name)
    for wanted in (use, ARCHIVE) if use == ALL else (use,):
        if wanted.lower() in marked:
            return marked[wanted.lower()]
    raise imaplib.IMAP4.error(f"the server marks no folder {use}")


def open_folder(imap: imaplib.IMAP4, folder: str) -> None:
    """Opens `folder` read-only; a special use (`SENT`, `ALL`) is looked up on the server."""
    if folder.startswith("\\"):
        folder = special_folder(imap, folder)
    status, _ = imap.select(_quoted(folder), readonly=True)
    if status != "OK":
        raise imaplib.IMAP4.error(f"cannot open folder {folder!r}")


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
        open_folder(imap, folder)
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
    by: str = "FROM",
) -> list[RawMail]:
    """Messages received between `since` and `before` (from `sender` if given, or to them with
    `by` "TO"), newest `limit` only, read live on the server however old they are (D171).
    Blocking: run it in a thread."""
    with imaplib.IMAP4_SSL(host, port, timeout=30) as imap:
        imap.login(username, password)
        open_folder(imap, folder)
        criteria = ["SINCE", since.strftime("%d-%b-%Y")]
        if before is not None:
            # BEFORE is a day, exclusive: the day itself is in the window.
            criteria += ["BEFORE", (before + timedelta(days=1)).strftime("%d-%b-%Y")]
        if sender and sender.isascii():
            criteria += [by, '"' + sender.replace("\\", "").replace('"', "") + '"']
        elif sender:  # "Enchères": imaplib sends ASCII only, so the name goes as a literal
            criteria = ["CHARSET", "UTF-8", *criteria, by]
            imap.literal = sender.encode()  # pyright: ignore[reportAttributeAccessIssue]
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


def fetch_attachment(
    host: str,
    port: int,
    username: str,
    password: str,
    folder: str,
    uid: int,
    message_id: str,
    index: int,
) -> Attached | None:
    """One attached file of the mail `uid`, if that uid is still the mail `message_id` (the
    server may have renumbered the folder). Blocking: run it in a thread."""
    with imaplib.IMAP4_SSL(host, port, timeout=60) as imap:
        imap.login(username, password)
        open_folder(imap, folder)
        _, fetched = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
        for item in fetched:
            if isinstance(item, tuple):
                parts = cast(tuple[Any, ...], item)
                if len(parts) > 1 and isinstance(parts[1], bytes):
                    raw = parts[1]
                    if (
                        message_id
                        and parse_message(uid, raw, datetime.now(UTC)).message_id != message_id
                    ):
                        return None
                    return attachment_of(raw, index)
        return None
