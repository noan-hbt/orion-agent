"""Tidying the owner's mailbox (D75): archiving, unsubscribing, writing drafts.

Reading stays strictly read-only (`imap`). These are the only writes, each one explicit:

- **archive**: the messages move from the account's folder to its `archive_folder` (UID MOVE,
  or COPY then delete where the server has no MOVE). Nothing is ever deleted for good;
- **unsubscribe**: the List-Unsubscribe header is read here, from the server, by uid (not
  from anything the model wrote). A one-click HTTPS address (RFC 8058) gets its POST; a
  `mailto:` gets a short mail from the owner's account; a page to visit is returned for the
  owner or the browser, never opened here;
- **draft**: a reply in the owner's voice is put in `drafts_folder`. It never leaves: he reads
  it, changes it, and sends it himself.

Archiving and unsubscribing are approved by the owner, as one batch (`mail_tidy`), unless he
let Orion do it alone. They run where the passwords are: in the guardian when Orion runs split.
"""

from __future__ import annotations

import asyncio
import imaplib
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any, Literal, cast
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import structlog

from orion.mail.service import MailAccount
from orion.mail.smtp import ADDRESS, Outgoing, SendError, build, smtp_send

log = structlog.get_logger(__name__)

TidyAction = Literal["archive", "unsubscribe"]


@dataclass
class Tidied:
    done: int = 0
    pages: list[str] = field(default_factory=list[str])
    """Unsubscribe pages to visit (no one-click address, no mailto)."""
    failed: list[str] = field(default_factory=list[str])

    def payload(self) -> dict[str, Any]:
        return {"done": self.done, "pages": self.pages, "failed": self.failed}

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> Tidied:
        return cls(
            int(data.get("done", 0)),
            [str(p) for p in data.get("pages", [])],
            [str(f) for f in data.get("failed", [])],
        )


Tidy = Callable[[MailAccount, TidyAction, list[int], str | None], Awaitable[Tidied]]
"""(account, action, uids, grant) → what was done."""
Draft = Callable[[MailAccount, Outgoing], Awaitable[None]]


@dataclass(frozen=True)
class Unsubscribe:
    one_click: str | None = None
    mailto: str | None = None
    page: str | None = None


def parse_unsubscribe(header: str, post: str) -> Unsubscribe:
    """The List-Unsubscribe header (and List-Unsubscribe-Post) of one message."""
    links = re.findall(r"<([^>]+)>", header)
    https = next((u for u in links if u.lower().startswith("https://")), None)
    mailto = next((u for u in links if u.lower().startswith("mailto:")), None)
    one_click = "one-click" in post.lower().replace(" ", "")
    return Unsubscribe(
        one_click=https if https and one_click else None,
        mailto=mailto,
        page=https if https and not one_click else None,
    )


def _login(account: MailAccount) -> imaplib.IMAP4_SSL:
    password = account.password.resolve().get_secret_value()
    imap = imaplib.IMAP4_SSL(account.host, account.port, timeout=30)
    imap.login(account.username, password)
    return imap


def _quoted(folder: str) -> str:
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def archive_uids(account: MailAccount, uids: list[int]) -> int:
    """Blocking: run it in a thread."""
    if not account.archive_folder:
        raise SendError(f"mail account {account.name!r} has no archive_folder")
    with _login(account) as imap:
        status, _ = imap.select(account.folder)
        if status != "OK":
            raise imaplib.IMAP4.error(f"cannot open folder {account.folder!r}")
        wanted = ",".join(str(u) for u in uids)
        target = _quoted(account.archive_folder)
        status, _ = imap.uid("MOVE", wanted, target)
        if status != "OK":
            status, _ = imap.uid("COPY", wanted, target)
            if status != "OK":
                raise imaplib.IMAP4.error(f"cannot copy to {account.archive_folder!r}")
            imap.uid("STORE", wanted, "+FLAGS.SILENT", "(\\Deleted)")
            imap.expunge()
    return len(uids)


def unsubscribe_headers(account: MailAccount, uids: list[int]) -> dict[int, Unsubscribe]:
    """Blocking: the unsubscribe addresses of each message, read from the server."""
    found: dict[int, Unsubscribe] = {}
    with _login(account) as imap:
        status, _ = imap.select(account.folder, readonly=True)
        if status != "OK":
            raise imaplib.IMAP4.error(f"cannot open folder {account.folder!r}")
        for uid in uids:
            _, fetched = imap.uid(
                "FETCH",
                str(uid),
                "(BODY.PEEK[HEADER.FIELDS (LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST)])",
            )
            raw = b""
            for item in fetched:
                if isinstance(item, tuple):
                    parts = cast(tuple[Any, ...], item)
                    if len(parts) > 1 and isinstance(parts[1], bytes):
                        raw = parts[1]
            text = raw.decode("utf-8", "replace").replace("\r\n ", " ").replace("\r\n\t", " ")
            header = post = ""
            for line in text.splitlines():
                name, _, value = line.partition(":")
                if name.strip().lower() == "list-unsubscribe":
                    header = value.strip()
                elif name.strip().lower() == "list-unsubscribe-post":
                    post = value.strip()
            found[uid] = parse_unsubscribe(header, post)
    return found


def append_draft(account: MailAccount, message: EmailMessage) -> None:
    """Blocking: the draft lands in `drafts_folder`, flagged as a draft."""
    if not account.drafts_folder:
        raise SendError(f"mail account {account.name!r} has no drafts_folder")
    with _login(account) as imap:
        status, _ = imap.append(
            _quoted(account.drafts_folder),
            "(\\Draft \\Seen)",
            imaplib.Time2Internaldate(time.time()),
            message.as_bytes(),
        )
        if status != "OK":
            raise imaplib.IMAP4.error(f"cannot write to {account.drafts_folder!r}")


def _https(url: str) -> str:
    if not url.startswith("https://"):
        raise ValueError("an unsubscribe link must be https")
    return url


def _mailto(address: str) -> Outgoing:
    """The unsubscribe mail a sender's header asks for, held to what an unsubscribe is: one
    address (never `victim@x, other@y`), a subject and one short line, from his account."""
    parsed = urlparse(address)
    query = parse_qs(parsed.query)
    to = unquote(parsed.path).strip()
    if not re.fullmatch(ADDRESS, to) or "to" in query or "cc" in query or "bcc" in query:
        raise ValueError(f"not a single unsubscribe address: {to[:80]!r}")
    body = (query.get("body") or ["unsubscribe"])[0].strip()
    return Outgoing(
        to=[to],
        subject=(query.get("subject") or ["unsubscribe"])[0][:200] or "unsubscribe",
        body=body if body and len(body) <= 200 and "\n" not in body else "unsubscribe",
    )


async def local_tidy(
    account: MailAccount,
    action: TidyAction,
    uids: list[int],
    _grant: str | None = None,
    http: httpx.AsyncClient | None = None,
) -> Tidied:
    """Where the password is: in the guardian when split, else in the core."""
    outcome = Tidied()
    try:
        if action == "archive":
            outcome.done = await asyncio.to_thread(archive_uids, account, uids)
            return outcome
        targets = await asyncio.to_thread(unsubscribe_headers, account, uids)
    except (LookupError, OSError, TimeoutError, imaplib.IMAP4.error, SendError) as exc:
        log.warning("mail.tidy_failed", account=account.name, action=action, error=repr(exc))
        outcome.failed.append(f"{account.name}: {exc}")
        return outcome
    seen: set[str] = set()
    client = http or httpx.AsyncClient(timeout=15, follow_redirects=False)
    try:
        for target in targets.values():
            key = target.one_click or target.mailto or target.page or ""
            if not key or key in seen:
                if not key:
                    outcome.failed.append("aucune adresse de désabonnement")
                continue
            seen.add(key)
            try:
                if target.one_click:
                    response = await client.post(
                        _https(target.one_click), data={"List-Unsubscribe": "One-Click"}
                    )
                    response.raise_for_status()
                elif target.mailto:
                    await smtp_send(account, _mailto(target.mailto))
                else:
                    outcome.pages.append(str(target.page))
                    continue
                outcome.done += 1
            except (httpx.HTTPError, SendError, ValueError) as exc:
                log.warning("mail.unsubscribe_failed", error=repr(exc))
                outcome.failed.append(str(exc))
    finally:
        if http is None:
            await client.aclose()
    return outcome


async def local_draft(account: MailAccount, mail: Outgoing) -> None:
    await asyncio.to_thread(append_draft, account, build(account, mail))
