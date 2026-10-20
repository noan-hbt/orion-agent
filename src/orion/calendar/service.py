"""The owner's agendas: synced into the journal, read by the model, used by the initiative.

Each sync reads every account's feed over a window (yesterday to `horizon_days` ahead) and
journals only what changed: `calendar.event.seen` for a new or modified occurrence,
`calendar.event.gone` for one that disappeared. The projection is rebuilt from those events like
any other. A feed that cannot be read changes nothing: an unreachable calendar is never taken
for an empty one.

    calendar.event.seen / calendar.event.gone / calendar.synced
    calendar.event.added   (Orion wrote it; shown until the feed carries it)
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field

from orion.calendar.ics import Occurrence, Standing, occurrences
from orion.calendar.write import (
    CalendarWriteError,
    NewCalendarEvent,
    WriteEvent,
    Writer,
    write_event,
)
from orion.config import SecretRef
from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.initiative.arbiter import Moment
from orion.initiative.engine import Alert, DigestLine
from orion.modules.base import ModuleSettings

log = structlog.get_logger(__name__)

SEEN = "calendar.event.seen"
GONE = "calendar.event.gone"
SYNCED = "calendar.synced"
ADDED = "calendar.event.added"
FEED_LAG = timedelta(days=1)
"""How long an event Orion added is kept while the feed does not show it yet (Google's iCal
address lags by hours)."""
_WEEKDAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


class CalendarAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$", description="e.g. 'perso', 'boulot'")
    url: SecretRef = Field(description="The calendar's secret iCal address (it is a password)")
    write: Writer | None = Field(
        default=None, description="How to add events (CalDAV or Google); read-only without"
    )


Fetch = Callable[[CalendarAccount], Awaitable[str]]
"""An account's feed. The secret address is resolved by whoever fetches: the guardian process
when Orion runs split (D28), so the core never holds it."""


class CalendarSettings(ModuleSettings):
    owner_emails: list[str] = Field(
        default_factory=list[str],
        description="The owner's addresses: tells their own events from invitations",
    )
    accounts: list[CalendarAccount] = Field(default_factory=list[CalendarAccount])
    refresh_minutes: float = Field(default=10.0, ge=1.0)
    horizon_days: int = Field(default=14, ge=1, le=90)
    heads_up_minutes: int = Field(default=15, ge=0, description="0: no alert before events")
    write_account: str | None = Field(
        default=None, description="Where Orion adds events; default: the first writable account"
    )

    def writable(self) -> CalendarAccount | None:
        accounts = [a for a in self.accounts if a.write is not None]
        if self.write_account is not None:
            return next((a for a in accounts if a.name == self.write_account), None)
        return accounts[0] if accounts else None


@dataclass
class CalendarState:
    name: str = "calendar"
    events: dict[tuple[str, str], Occurrence] = field(
        default_factory=dict[tuple[str, str], Occurrence]
    )
    """(account, occurrence key) → occurrence"""
    synced: dict[str, datetime] = field(default_factory=dict[str, datetime])
    added: dict[tuple[str, str], datetime] = field(default_factory=dict[tuple[str, str], datetime])
    """Events Orion wrote, and when: kept until the feed carries them."""
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.events.clear()
        self.synced.clear()
        self.added.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted:
            return
        payload: dict[str, Any] = event.payload
        if event.kind == SEEN:
            occurrence = Occurrence.from_payload(payload["occurrence"])
            self.events[(str(payload["account"]), occurrence.key)] = occurrence
        elif event.kind == ADDED:
            occurrence = Occurrence.from_payload(payload["occurrence"])
            where = (str(payload["account"]), occurrence.key)
            self.events[where] = occurrence
            self.added[where] = datetime.fromisoformat(str(payload["at"]))
        elif event.kind == GONE:
            where = (str(payload["account"]), str(payload["key"]))
            self.events.pop(where, None)
            self.added.pop(where, None)
        elif event.kind == SYNCED:
            self.synced[str(payload["account"])] = datetime.fromisoformat(str(payload["at"]))


async def fetch_feed(http: httpx.AsyncClient, account: CalendarAccount) -> str:
    url = account.url.resolve().get_secret_value()
    response = await http.get(url.replace("webcal://", "https://", 1))
    response.raise_for_status()
    return response.text


def _hhmm(moment: datetime) -> str:
    return f"{moment:%H:%M}"


def describe_day(day: date) -> str:
    return f"{_WEEKDAYS[day.weekday()]} {day.isoformat()}"


def render(account: str, o: Occurrence, multiple_accounts: bool) -> str:
    when = "toute la journée" if o.all_day else f"{_hhmm(o.start)}-{_hhmm(o.end)}"
    parts = [f"{when} {o.title}"]
    if o.location:
        parts.append(f"({o.location})")
    if o.standing is Standing.ACCEPTED:
        parts.append(f"[invitation de {o.organizer}]")
    if multiple_accounts:
        parts.append(f"[{account}]")
    line = " ".join(parts)
    return line + (f" — note : {o.description}" if o.description else "")


class Calendar:
    def __init__(
        self,
        journal: EventStore,
        settings: CalendarSettings,
        timezone: str,
        fetch: Fetch | None = None,
        clock: Callable[[], datetime] | None = None,
        write: WriteEvent | None = None,
    ) -> None:
        self._journal = journal
        self.settings = settings
        self._write = write or self._http_write
        self._zone = ZoneInfo(timezone)
        self._timezone = timezone
        self._http: httpx.AsyncClient | None = None
        self._fetch = fetch or self._http_fetch
        self._clock = clock or (lambda: datetime.now(self._zone))
        self.state = CalendarState()
        self._runner = ProjectionRunner(journal, [self.state])
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        await self._runner.catch_up()

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    async def _http_fetch(self, account: CalendarAccount) -> str:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=20.0, follow_redirects=True)
        return await fetch_feed(self._http, account)

    async def _http_write(self, account: str, event: NewCalendarEvent, _grant: str | None) -> str:
        found = next((a for a in self.settings.accounts if a.name == account), None)
        if found is None or found.write is None:
            raise CalendarWriteError(f"calendar {account!r} cannot be written to")
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=20.0, follow_redirects=True)
        return await write_event(self._http, found.write, event)

    # -- writing ------------------------------------------------------------------------------

    async def add(
        self,
        event: NewCalendarEvent,
        grant: str | None = None,
        correlation_id: str | None = None,
    ) -> tuple[str, Occurrence]:
        """Write `event` to the writable account; it shows at once, before the feed has it."""
        account = self.settings.writable()
        if account is None:
            raise CalendarWriteError("no calendar can be written to: add `write` to an account")
        uid = await self._write(account.name, event, grant)
        start, end = event.start.astimezone(self._zone), event.end.astimezone(self._zone)
        occurrence = Occurrence(
            key=f"{uid}@{start.isoformat()}",
            start=start,
            end=end,
            all_day=False,
            standing=Standing.OWN,
            title=event.title,
            location=event.location,
            description=event.notes,
        )
        await self._journal.append(
            [
                NewEvent(
                    kind=ADDED,
                    payload={
                        "account": account.name,
                        "uid": uid,
                        "at": self._clock().isoformat(),
                        "occurrence": occurrence.payload(),
                        **({"meeting": event.meeting} if event.meeting else {}),
                    },
                    provenance=Provenance(source=f"calendar:{account.name}", actor="orion:core"),
                    trust=Trust.SYSTEM,
                    correlation_id=correlation_id,
                )
            ]
        )
        await self._runner.catch_up()
        return account.name, occurrence

    # -- sync ---------------------------------------------------------------------------------

    async def run(self) -> None:
        while True:
            await self.sync_once()
            await asyncio.sleep(self.settings.refresh_minutes * 60)

    async def ensure_fresh(self) -> None:
        """Sync first if an account is older than the refresh interval (e.g. in `orion chat`)."""
        await self._runner.catch_up()
        now = self._clock()
        stale = timedelta(minutes=self.settings.refresh_minutes)
        if any(
            now - self.state.synced.get(a.name, datetime.min.replace(tzinfo=self._zone)) > stale
            for a in self.settings.accounts
        ):
            await self.sync_once()

    async def sync_once(self) -> dict[str, int]:
        """Returns the number of changes journaled per account (failed accounts are absent)."""
        async with self._lock:
            await self._runner.catch_up()
            now = self._clock()
            start = now - timedelta(days=1)
            end = now + timedelta(days=self.settings.horizon_days)
            changes: dict[str, int] = {}
            for account in self.settings.accounts:
                try:
                    feed = await self._fetch(account)
                    found = occurrences(
                        feed, start, end, self._timezone, self.settings.owner_emails
                    )
                except (LookupError, ValueError, httpx.HTTPError) as exc:
                    log.warning("calendar.sync_failed", account=account.name, error=str(exc))
                    continue
                changes[account.name] = await self._apply(account.name, found, start, end, now)
            return changes

    async def _apply(
        self,
        account: str,
        found: list[Occurrence],
        start: datetime,
        end: datetime,
        now: datetime,
    ) -> int:
        provenance = Provenance(source=f"calendar:{account}", actor=f"calendar:{account}")
        correlation = new_id()
        fresh = {o.key: o for o in found}
        events: list[NewEvent] = []
        for key, occurrence in fresh.items():
            if self.state.events.get((account, key)) != occurrence:
                events.append(
                    NewEvent(
                        kind=SEEN,
                        payload={"account": account, "occurrence": occurrence.payload()},
                        provenance=provenance,
                        trust=Trust.SYSTEM if occurrence.standing.visible else Trust.THIRD_PARTY,
                        correlation_id=correlation,
                    )
                )
        for (acc, key), known in self.state.events.items():
            if acc != account or key in fresh or not (known.end > start and known.start < end):
                continue
            added = self.state.added.get((acc, key))
            if (
                added is not None
                and now - added < FEED_LAG
                and not any(o.start == known.start and o.title == known.title for o in found)
            ):
                continue  # the feed does not carry it yet
            events.append(
                NewEvent(
                    kind=GONE,
                    payload={"account": account, "key": key},
                    provenance=provenance,
                    trust=Trust.SYSTEM,
                    correlation_id=correlation,
                )
            )
        events.append(
            NewEvent(
                kind=SYNCED,
                payload={"account": account, "at": now.isoformat(), "changes": len(events)},
                provenance=provenance,
                trust=Trust.SYSTEM,
                correlation_id=correlation,
            )
        )
        await self._journal.append(events)
        await self._runner.catch_up()
        if len(events) > 1:
            log.info("calendar.synced", account=account, changes=len(events) - 1)
        return len(events) - 1

    # -- reading ------------------------------------------------------------------------------

    def between(self, start: datetime, end: datetime) -> list[tuple[str, Occurrence]]:
        """Visible occurrences overlapping [start, end), sorted by start."""
        return sorted(
            (
                (account, o)
                for (account, _), o in self.state.events.items()
                if o.standing.visible and o.end > start and o.start < end
            ),
            key=lambda item: (item[1].start, item[1].title),
        )

    def pending(self, start: datetime, end: datetime) -> int:
        return sum(
            1
            for o in self.state.events.values()
            if o.standing is Standing.PENDING and o.end > start and o.start < end
        )

    def day(self, day: date, after: datetime | None = None) -> list[str]:
        start = datetime.combine(day, datetime.min.time(), self._zone)
        found = self.between(after or start, start + timedelta(days=1))
        multiple = len(self.settings.accounts) > 1
        return [render(account, o, multiple) for account, o in found]

    # -- initiative ---------------------------------------------------------------------------

    async def alerts(self, now: datetime) -> list[Alert]:
        if not self.settings.heads_up_minutes:
            return []
        await self._runner.catch_up()
        window = timedelta(minutes=self.settings.heads_up_minutes)
        found: list[Alert] = []
        for account, o in self.between(now, now + window):
            if o.all_day or not timedelta(0) < o.start - now <= window:
                continue
            minutes = max(1, int((o.start - now).total_seconds() // 60))
            where = f" ({o.location})" if o.location else ""
            found.append(
                Alert(
                    key=f"calendar:{account}:{o.key}",
                    notice=f"À l'agenda, bientôt : {o.title}{where}, de {_hhmm(o.start)} à "
                    f"{_hhmm(o.end)}.",
                    why=f"ça commence dans {minutes} min",
                    fallback=f"Dans {minutes} min : {o.title}{where}",
                    expires_at=o.start,
                    importance=0.9,
                )
            )
        return found

    async def presence(self, now: datetime) -> Moment:
        """Busy during a timed event; otherwise, when the last one ended (today)."""
        await self._runner.catch_up()
        today = datetime.combine(now.date(), datetime.min.time(), self._zone)
        timed = [o for _, o in self.between(today, now + timedelta(minutes=1)) if not o.all_day]
        if any(o.start <= now < o.end for o in timed):
            return Moment(busy=True)
        ended = [o.end for o in timed if o.end <= now]
        return Moment(free_since=max(ended) if ended else None)

    async def digest(
        self, now: datetime, since: datetime, first_slot: bool, last_slot: bool
    ) -> list[DigestLine]:
        """Today's agenda in the first digest, tomorrow's in the last one."""
        await self._runner.catch_up()
        lines: list[str] = []
        if first_slot:
            today = self.day(now.date(), after=now)
            if today:
                lines.append("- Agenda d'aujourd'hui : " + " ; ".join(today))
            day_end = datetime.combine(now.date(), datetime.min.time(), self._zone)
            waiting = self.pending(now, day_end + timedelta(days=self.settings.horizon_days))
            if waiting:
                lines.append(f"- {waiting} invitation(s) attendent sa réponse")
        if last_slot:
            tomorrow = self.day(now.date() + timedelta(days=1))
            if tomorrow:
                lines.append("- Agenda de demain : " + " ; ".join(tomorrow))
        return [DigestLine(line) for line in lines]
