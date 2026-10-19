"""The owner's agendas: synced into the journal, read by the model, used by the initiative.

Each sync reads every account's feed over a window (yesterday to `horizon_days` ahead) and
journals only what changed: `calendar.event.seen` for a new or modified occurrence,
`calendar.event.gone` for one that disappeared. The projection is rebuilt from those events like
any other. A feed that cannot be read changes nothing: an unreachable calendar is never taken
for an empty one.

    calendar.event.seen / calendar.event.gone / calendar.synced
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
from orion.config import SecretRef
from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.ids import new_id
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.initiative.engine import Alert, DigestLine
from orion.modules.base import ModuleSettings

log = structlog.get_logger(__name__)

SEEN = "calendar.event.seen"
GONE = "calendar.event.gone"
SYNCED = "calendar.synced"
_WEEKDAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]

Fetch = Callable[[str], Awaitable[str]]


class CalendarAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$", description="e.g. 'perso', 'boulot'")
    url: SecretRef = Field(description="The calendar's secret iCal address (it is a password)")


class CalendarSettings(ModuleSettings):
    owner_emails: list[str] = Field(
        default_factory=list[str],
        description="The owner's addresses: tells their own events from invitations",
    )
    accounts: list[CalendarAccount] = Field(default_factory=list[CalendarAccount])
    refresh_minutes: float = Field(default=10.0, ge=1.0)
    horizon_days: int = Field(default=14, ge=1, le=90)
    heads_up_minutes: int = Field(default=15, ge=0, description="0: no alert before events")


@dataclass
class CalendarState:
    name: str = "calendar"
    events: dict[tuple[str, str], Occurrence] = field(
        default_factory=dict[tuple[str, str], Occurrence]
    )
    """(account, occurrence key) → occurrence"""
    synced: dict[str, datetime] = field(default_factory=dict[str, datetime])
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.events.clear()
        self.synced.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted:
            return
        payload: dict[str, Any] = event.payload
        if event.kind == SEEN:
            occurrence = Occurrence.from_payload(payload["occurrence"])
            self.events[(str(payload["account"]), occurrence.key)] = occurrence
        elif event.kind == GONE:
            self.events.pop((str(payload["account"]), str(payload["key"])), None)
        elif event.kind == SYNCED:
            self.synced[str(payload["account"])] = datetime.fromisoformat(str(payload["at"]))


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
    ) -> None:
        self._journal = journal
        self.settings = settings
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

    async def _http_fetch(self, url: str) -> str:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=20.0, follow_redirects=True)
        response = await self._http.get(url.replace("webcal://", "https://", 1))
        response.raise_for_status()
        return response.text

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
                    url = account.url.resolve().get_secret_value()
                    feed = await self._fetch(url)
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
            if acc == account and key not in fresh and known.end > start and known.start < end:
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
                )
            )
        return found

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
