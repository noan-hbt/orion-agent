"""Sending mail over SMTP, with the account's app password.

Only the guardian sends when Orion runs split (D28), and only what the owner approved word
for word: the recipients, the subject and the text it showed them.
"""

from __future__ import annotations

import asyncio
import base64
import smtplib
import ssl
from collections.abc import Awaitable, Callable
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from pydantic import BaseModel, ConfigDict, Field

from orion.core.events import NewEvent, Provenance, Trust
from orion.core.journal import EventStore
from orion.mail.service import MailAccount, Mailbox, MailCard
from orion.memory.model import CommitmentStatus
from orion.memory.service import MemoryService, Origin

ADDRESS = r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$"


class MailFile(BaseModel):
    """A file from Orion's store, attached to a mail (a paper, a dossier, a letter)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    file_id: str
    name: str
    content_type: str
    data: str = Field(description="Base64")


class Outgoing(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    to: list[str] = Field(min_length=1, max_length=10)
    cc: list[str] = Field(default_factory=list[str], max_length=10)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20_000)
    in_reply_to: str | None = Field(default=None, max_length=300)
    attachments: list[MailFile] = Field(default_factory=list[MailFile], max_length=5)

    def recipients(self) -> list[str]:
        return [*self.to, *self.cc]


class SendError(RuntimeError):
    """The mail could not be sent."""


Send = Callable[[MailAccount, Outgoing, str | None], Awaitable[str]]
"""(account, mail, grant) → the sent mail's Message-ID."""


def build(account: MailAccount, mail: Outgoing, sender_name: str = "") -> EmailMessage:
    message = EmailMessage()
    address = account.sender or account.username
    message["From"] = formataddr((sender_name, address)) if sender_name else address
    message["To"] = ", ".join(mail.to)
    if mail.cc:
        message["Cc"] = ", ".join(mail.cc)
    message["Subject"] = mail.subject
    message["Message-ID"] = make_msgid(domain=address.rsplit("@", 1)[-1])
    if mail.in_reply_to:
        message["In-Reply-To"] = mail.in_reply_to
        message["References"] = mail.in_reply_to
    message.set_content(mail.body)
    for attached in mail.attachments:
        maintype, _, subtype = attached.content_type.partition("/")
        message.add_attachment(
            base64.b64decode(attached.data),
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=attached.name,
        )
    return message


def _send(account: MailAccount, message: EmailMessage) -> None:
    if account.smtp_host is None:
        raise SendError(f"mail account {account.name!r} has no smtp_host: it cannot send")
    password = account.password.resolve().get_secret_value()
    context = ssl.create_default_context()
    try:
        if account.smtp_port == 465:
            with smtplib.SMTP_SSL(account.smtp_host, 465, context=context, timeout=30) as smtp:
                smtp.login(account.username, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(account.smtp_host, account.smtp_port, timeout=30) as smtp:
                smtp.starttls(context=context)
                smtp.login(account.username, password)
                smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        raise SendError(f"sending through {account.smtp_host} failed: {exc!r}") from exc


async def smtp_send(account: MailAccount, mail: Outgoing, _grant: str | None = None) -> str:
    message = build(account, mail)
    await asyncio.to_thread(_send, account, message)
    return str(message["Message-ID"])


SENT = "mail.sent"


class Postman:
    """Sends approved mail from the owner's account and journals it (`mail.sent`)."""

    def __init__(
        self,
        journal: EventStore,
        mailbox: Mailbox,
        send: Send | None = None,
        memory: MemoryService | None = None,
    ) -> None:
        self._journal = journal
        self._mailbox = mailbox
        self._send = send or smtp_send
        self._memory = memory

    def account(self, reply_to: MailCard | None) -> MailAccount | None:
        accounts = [a for a in self._mailbox.settings.accounts if a.smtp_host]
        if reply_to is not None:
            return next((a for a in accounts if a.name == reply_to.account), None)
        wanted = self._mailbox.settings.send_account
        if wanted is not None:
            return next((a for a in accounts if a.name == wanted), None)
        return accounts[0] if accounts else None

    @property
    def can_send(self) -> bool:
        return any(a.smtp_host for a in self._mailbox.settings.accounts)

    async def send(
        self,
        mail: Outgoing,
        reply_to: MailCard | None,
        grant: str | None,
        correlation_id: str | None = None,
        closes: list[str] | None = None,
    ) -> tuple[str, str]:
        """(account name, Message-ID). Commitments the mail fulfils are closed with it as
        proof: those it names in `closes`, and what the owner owed the mail it answers."""
        account = self.account(reply_to)
        if account is None:
            raise SendError("no mail account can send (add smtp_host to one)")
        if reply_to is not None and reply_to.message_id:
            mail = mail.model_copy(update={"in_reply_to": reply_to.message_id})
        message_id = await self._send(account, mail, grant)
        await self._journal.append(
            [
                NewEvent(
                    kind=SENT,
                    payload={
                        "account": account.name,
                        "to": mail.to,
                        "cc": mail.cc,
                        "subject": mail.subject,
                        "message_id": message_id,
                        **({"reply_to": reply_to.id} if reply_to else {}),
                    },
                    provenance=Provenance(source=f"mail:{account.name}", actor="orion:core"),
                    trust=Trust.SYSTEM,
                    correlation_id=correlation_id,
                )
            ]
        )
        await self._fulfil(reply_to, closes or [], message_id, correlation_id)
        return account.name, message_id

    async def _fulfil(
        self,
        reply_to: MailCard | None,
        closes: list[str],
        message_id: str,
        correlation_id: str | None,
    ) -> None:
        if self._memory is None:
            return
        await self._mailbox.start()
        owed = self._mailbox.state.promises.get(reply_to.id, []) if reply_to else []
        wanted = set(closes) | set(owed)
        if not wanted:
            return
        for commitment in await self._memory.store.commitments():
            if (
                commitment.id in wanted
                and commitment.status is CommitmentStatus.OPEN
                and (commitment.id in closes or commitment.debtor == "owner")
            ):
                await self._memory.close_commitment(
                    commitment.id,
                    CommitmentStatus.DONE,
                    Origin(
                        actor="orion:core",
                        source=f"mail:sent:{message_id}",
                        trust=Trust.SYSTEM,
                        correlation_id=correlation_id,
                    ),
                    evidence=f"mail envoyé {message_id}",
                )
