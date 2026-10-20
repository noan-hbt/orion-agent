"""The owner's mailboxes: read over IMAP, each mail reduced to a card in quarantine.

Only cards are journaled (`mail.card`), never message bodies: the journal stays small and the
mail stays where it lives. A card is third-party content and carries `Trust.THIRD_PARTY`
everywhere, so the guardian treats any conversation that shows one accordingly (D26).

    mail.card / mail.synced
"""

from __future__ import annotations

import asyncio
import imaplib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from pydantic import BaseModel, ConfigDict, Field

from orion.config import SecretRef
from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.initiative.engine import Alert, DigestLine
from orion.mail.imap import Batch, RawMail, fetch_since
from orion.mail.quarantine import Card, Category, Importance, Quarantine
from orion.memory.model import COMMITMENT_CREATED
from orion.memory.service import MemoryService, Origin
from orion.modules.base import ModuleSettings

log = structlog.get_logger(__name__)

CARD = "mail.card"
SYNCED = "mail.synced"
_QUIET = {Category.NEWSLETTER, Category.NOTIFICATION, Category.SPAM}


class MailAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$", description="e.g. 'perso', 'boulot'")
    host: str = Field(description="IMAP server, e.g. imap.gmail.com")
    port: int = 993
    username: str
    password: SecretRef = Field(description="An app password, never the account password")
    folder: str = "INBOX"
    smtp_host: str | None = Field(
        default=None, description="To send (e.g. smtp.gmail.com); read-only without"
    )
    smtp_port: int = Field(default=465, description="465 (TLS) or 587 (STARTTLS)")
    sender: str | None = Field(default=None, description="The From address; default: username")
    archive_folder: str | None = Field(
        default=None, description="Where archived mail goes, e.g. '[Gmail]/All Mail', 'Archive'"
    )
    drafts_folder: str | None = Field(
        default=None, description="Where drafts go, e.g. '[Gmail]/Drafts', 'Drafts'"
    )


class MailSettings(ModuleSettings):
    owner_emails: list[str] = Field(
        default_factory=list[str], description="The owner's addresses: their own mail is skipped"
    )
    accounts: list[MailAccount] = Field(default_factory=list[MailAccount])
    quarantine_model: str | None = Field(
        default=None, description="Model that reads mail; defaults to agent.extractor_model"
    )
    refresh_minutes: float = Field(default=5.0, ge=1.0)
    lookback_days: int = Field(default=2, ge=1, le=30)
    max_per_sync: int = Field(default=25, ge=1, le=200)
    alert_important: bool = True
    send_account: str | None = Field(
        default=None, description="Account new mail is sent from; default: the first that can"
    )


@dataclass(frozen=True)
class MailCard:
    id: str
    account: str
    from_addr: str
    from_name: str
    subject: str
    received_at: datetime
    card: Card
    message_id: str = ""
    uid: int = 0
    bulk: bool = False

    @property
    def sender(self) -> str:
        return self.from_name or self.from_addr

    def describe(self, zone: ZoneInfo) -> str:
        c = self.card
        tags = [c.category.value]
        if c.importance is Importance.HIGH:
            tags.append("important")
        if c.needs_reply:
            tags.append("réponse attendue")
        if c.due:
            tags.append(f"échéance {c.due.isoformat()}")
        if c.amount is not None:
            tags.append(f"{c.amount:.2f} {c.currency or ''}".strip())
        if c.read == "failed":
            tags.append("non lu par la quarantaine")
        when = self.received_at.astimezone(zone)
        about = c.summary or self.subject or "(sans objet)"
        return (
            f"({self.id}) {when:%d/%m %H:%M} {self.sender} <{self.from_addr}> — "
            f"« {self.subject} » : {about} [{', '.join(tags)}]"
        )


@dataclass
class MailState:
    name: str = "mail"
    cards: dict[str, MailCard] = field(default_factory=dict[str, MailCard])
    cursors: dict[str, tuple[int, int]] = field(default_factory=dict[str, tuple[int, int]])
    """account → (uidvalidity, last uid read)"""
    synced: dict[str, datetime] = field(default_factory=dict[str, datetime])
    promises: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    """card id → commitments it created (closed with proof when the owner answers)."""
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.cards.clear()
        self.cursors.clear()
        self.synced.clear()
        self.promises.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted:
            return
        payload: dict[str, Any] = event.payload
        if event.kind == CARD:
            card = MailCard(
                id=str(payload["card_id"]),
                account=str(payload["account"]),
                from_addr=str(payload["from_addr"]),
                from_name=str(payload["from_name"]),
                subject=str(payload["subject"]),
                received_at=datetime.fromisoformat(str(payload["received_at"])),
                card=Card.from_payload(payload["card"]),
                message_id=str(payload.get("message_id", "")),
                uid=int(payload.get("uid", 0)),
                bulk=bool(payload.get("bulk", False)),
            )
            self.cards[card.id] = card
        elif event.kind == COMMITMENT_CREATED and (event.correlation_id or "").startswith("m_"):
            card = str(event.correlation_id)
            self.promises.setdefault(card, []).append(str(payload["commitment_id"]))
        elif event.kind == SYNCED:
            account = str(payload["account"])
            self.cursors[account] = (int(payload["uidvalidity"]), int(payload["last_uid"]))
            self.synced[account] = datetime.fromisoformat(str(payload["at"]))


Fetch = Callable[[MailAccount, int, int, int, int, datetime], Awaitable[Batch]]
"""(account, uidvalidity, last_uid, lookback_days, limit, now) → new mail. The app password is
resolved by whoever fetches: the guardian process when Orion runs split (D28)."""


async def imap_fetch(
    account: MailAccount,
    uidvalidity: int,
    last_uid: int,
    lookback_days: int,
    limit: int,
    now: datetime,
) -> Batch:
    password = account.password.resolve().get_secret_value()
    return await asyncio.to_thread(
        fetch_since,
        account.host,
        account.port,
        account.username,
        password,
        account.folder,
        uidvalidity,
        last_uid,
        lookback_days,
        limit,
        now,
    )


class Mailbox:
    def __init__(
        self,
        journal: EventStore,
        settings: MailSettings,
        quarantine: Quarantine,
        timezone: str,
        fetch: Fetch | None = None,
        clock: Callable[[], datetime] | None = None,
        memory: MemoryService | None = None,
    ) -> None:
        self._journal = journal
        self.settings = settings
        self._quarantine = quarantine
        self._memory = memory
        self._zone = ZoneInfo(timezone)
        self._fetch = fetch or imap_fetch
        self._clock = clock or (lambda: datetime.now(self._zone))
        self._owners = {e.strip().lower() for e in settings.owner_emails}
        self.state = MailState()
        self._runner = ProjectionRunner(journal, [self.state])
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        await self._runner.catch_up()

    # -- sync ---------------------------------------------------------------------------------

    async def run(self) -> None:
        while True:
            await self.sync_once()
            await asyncio.sleep(self.settings.refresh_minutes * 60)

    async def ensure_fresh(self) -> None:
        await self._runner.catch_up()
        now = self._clock()
        stale = timedelta(minutes=self.settings.refresh_minutes)
        if any(
            now - self.state.synced.get(a.name, datetime.min.replace(tzinfo=self._zone)) > stale
            for a in self.settings.accounts
        ):
            await self.sync_once()

    async def sync_once(self) -> dict[str, int]:
        """Returns the number of new cards per account (failed accounts are absent)."""
        async with self._lock:
            await self._runner.catch_up()
            now = self._clock()
            added: dict[str, int] = {}
            for account in self.settings.accounts:
                uidvalidity, last_uid = self.state.cursors.get(account.name, (0, 0))
                try:
                    batch = await self._fetch(
                        account,
                        uidvalidity,
                        last_uid,
                        self.settings.lookback_days,
                        self.settings.max_per_sync,
                        now,
                    )
                except (LookupError, OSError, TimeoutError, imaplib.IMAP4.error) as exc:
                    log.warning("mail.sync_failed", account=account.name, error=repr(exc))
                    continue
                if batch.skipped:
                    log.info("mail.skipped_old", account=account.name, count=batch.skipped)
                added[account.name] = await self._record(account.name, batch, now)
            return added

    async def _record(self, account: str, batch: Batch, now: datetime) -> int:
        events: list[NewEvent] = []
        correlation = new_id()
        for mail in batch.mails:
            if mail.from_addr in self._owners:
                continue  # the owner's own mail (a copy, a note to self)
            events.append(await self._card_event(account, mail, correlation))
        events.append(
            NewEvent(
                kind=SYNCED,
                payload={
                    "account": account,
                    "uidvalidity": batch.uidvalidity,
                    "last_uid": batch.last_uid,
                    "at": now.isoformat(),
                },
                provenance=Provenance(source=f"mail:{account}", actor="orion:mail"),
                trust=Trust.SYSTEM,
                correlation_id=correlation,
            )
        )
        await self._journal.append(events)
        await self._runner.catch_up()
        for event in events:
            if event.kind == CARD:
                await self._promise(event.payload)
        if len(events) > 1:
            log.info("mail.synced", account=account, new=len(events) - 1)
        return len(events) - 1

    async def _promise(self, payload: dict[str, Any]) -> None:
        """A promise the mail carries joins the commitments ledger, marked as third-party."""
        card = Card.from_payload(payload["card"])
        if self._memory is None or card.promise is None or card.suspicious:
            return
        sender = str(payload["from_name"]) or str(payload["from_addr"])
        owner_owes = card.promise.by == "owner"
        await self._memory.add_commitment(
            f"{card.promise.what} (d'après un mail de {sender})",
            Origin(
                actor="orion:quarantine",
                source=f"mail:{payload['account']}:{payload['message_id']}",
                trust=Trust.THIRD_PARTY,
                correlation_id=str(payload["card_id"]),
            ),
            debtor="owner" if owner_owes else sender,
            creditor=sender if owner_owes else "owner",
            due=card.promise.due.isoformat() if card.promise.due else None,
        )

    async def _card_event(self, account: str, mail: RawMail, correlation: str) -> NewEvent:
        card = await self._quarantine.card(mail)
        return NewEvent(
            kind=CARD,
            payload={
                "card_id": f"m_{new_id()}",
                "account": account,
                "message_id": mail.message_id,
                "uid": mail.uid,
                "bulk": mail.bulk,
                "from_addr": mail.from_addr,
                "from_name": mail.from_name,
                "subject": mail.subject[:120],
                "received_at": mail.received_at.isoformat(),
                "card": card.payload(),
            },
            provenance=Provenance(
                source=f"mail:{account}:{mail.message_id}", actor="orion:quarantine"
            ),
            trust=Trust.THIRD_PARTY,
            correlation_id=correlation,
        )

    async def record(self, event: NewEvent) -> None:
        """A write to the mailbox (tidied, drafted), journaled."""
        await self._journal.append([event])

    # -- reading ------------------------------------------------------------------------------

    def recent(self, since: datetime, worth_it: bool = False) -> list[MailCard]:
        """Cards received since `since`, newest first; `worth_it` drops newsletters,
        notifications, spam and anything marked low."""
        found = [
            c
            for c in self.state.cards.values()
            if c.received_at >= since
            and (
                not worth_it
                or (c.card.category not in _QUIET and c.card.importance is not Importance.LOW)
            )
        ]
        return sorted(found, key=lambda c: c.received_at, reverse=True)

    def describe(self, card: MailCard) -> str:
        return card.describe(self._zone)

    # -- initiative ---------------------------------------------------------------------------

    async def alerts(self, now: datetime) -> list[Alert]:
        if not self.settings.alert_important:
            return []
        await self._runner.catch_up()
        return [
            Alert(
                key=f"mail:{c.id}",
                notice=f"Nouveau mail important (contenu tiers, résumé en quarantaine) : "
                f"{self.describe(c)}",
                why=f"{c.sender} attend sans doute une réponse"
                if c.card.needs_reply
                else "il a l'air important",
                fallback=f"Mail important de {c.sender} : « {c.subject} »",
                trust=Trust.THIRD_PARTY,
                expires_at=c.received_at + timedelta(hours=12),
                importance=0.7,
            )
            for c in self.recent(now - timedelta(hours=12), worth_it=True)
            if c.card.importance is Importance.HIGH
        ]

    async def digest(
        self, now: datetime, since: datetime, first_slot: bool, last_slot: bool
    ) -> list[DigestLine]:
        await self._runner.catch_up()
        received = self.recent(since)
        if not received:
            return []
        worth = [c for c in received if c.card.category not in _QUIET]
        worth = [c for c in worth if c.card.importance is not Importance.LOW]
        lines = [f"- Mail : {self.describe(c)}" for c in worth[:8]]
        rest = len(received) - len(lines)
        if rest:
            lines.append(f"- et {rest} autre(s) mail(s) sans intérêt particulier")
        return [DigestLine(line, Trust.THIRD_PARTY) for line in lines]
