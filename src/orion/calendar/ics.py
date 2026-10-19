"""Reading an iCalendar feed: every occurrence in a time window, with who wrote it.

Calendars are read through their secret iCal address (Google, iCloud and Outlook all publish
one), so every provider works the same way and nothing needs write access.

Who wrote an event decides what Orion may read of it. A calendar invitation is text a stranger
can put in front of Orion, and hidden descriptions are a known way to smuggle instructions into
an assistant. So:

- the owner's own events (no organizer, or organized by one of `owner_emails`): everything;
- invitations the owner accepted: time, title and location only. The owner saw and adopted
  them; the description, which nobody rereads, is never kept;
- invitations not answered, or declined: nothing but the fact they exist.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from typing import Any, cast
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events  # pyright: ignore[reportMissingTypeStubs]

MAX_TITLE = 120
MAX_LOCATION = 120
MAX_DESCRIPTION = 600


class Standing(StrEnum):
    OWN = "own"
    ACCEPTED = "accepted"
    PENDING = "pending"
    DECLINED = "declined"

    @property
    def visible(self) -> bool:
        return self in (Standing.OWN, Standing.ACCEPTED)


@dataclass(frozen=True)
class Occurrence:
    key: str
    """Stable per occurrence: UID plus the start of this instance."""
    start: datetime
    end: datetime
    all_day: bool
    standing: Standing
    title: str = ""
    location: str = ""
    description: str = ""
    organizer: str = ""

    def payload(self) -> dict[str, object]:
        return {
            "key": self.key,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "all_day": self.all_day,
            "standing": self.standing.value,
            "title": self.title,
            "location": self.location,
            "description": self.description,
            "organizer": self.organizer,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Occurrence:
        return cls(
            key=str(payload["key"]),
            start=datetime.fromisoformat(str(payload["start"])),
            end=datetime.fromisoformat(str(payload["end"])),
            all_day=bool(payload["all_day"]),
            standing=Standing(str(payload["standing"])),
            title=str(payload.get("title", "")),
            location=str(payload.get("location", "")),
            description=str(payload.get("description", "")),
            organizer=str(payload.get("organizer", "")),
        )


def _text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _address(value: Any) -> str:
    return str(value or "").removeprefix("mailto:").removeprefix("MAILTO:").strip().lower()


def _moment(value: date | datetime, zone: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=zone) if value.tzinfo is None else value.astimezone(zone)
    return datetime.combine(value, time(0), zone)


def _standing(event: Any, owner_emails: frozenset[str]) -> tuple[Standing, str]:
    organizer = _address(event.get("ORGANIZER"))
    if not organizer or organizer in owner_emails:
        return Standing.OWN, organizer
    raw: Any = event.get("ATTENDEE") or []
    attendees: list[Any] = cast(list[Any], raw) if isinstance(raw, list) else [raw]
    for attendee in attendees:
        if _address(attendee) in owner_emails:
            status = str(attendee.params.get("PARTSTAT", "NEEDS-ACTION")).upper()
            if status in ("ACCEPTED", "TENTATIVE"):
                return Standing.ACCEPTED, organizer
            if status == "DECLINED":
                return Standing.DECLINED, organizer
            return Standing.PENDING, organizer
    # Someone else's event and the owner is not listed: nothing says the owner adopted it.
    return Standing.PENDING, organizer


def occurrences(
    feed: str,
    start: datetime,
    end: datetime,
    timezone: str,
    owner_emails: Iterable[str] = (),
) -> list[Occurrence]:
    """Occurrences overlapping [start, end), sorted by start. Raises ValueError on a feed that
    is not iCalendar (the caller must not take it for an empty calendar)."""
    zone = ZoneInfo(timezone)
    owners = frozenset(e.strip().lower() for e in owner_emails)
    try:
        calendar = icalendar.Calendar.from_ical(feed)
    except Exception as exc:
        raise ValueError(f"not an iCalendar feed: {exc}") from exc
    query: Any = recurring_ical_events.of(calendar)  # pyright: ignore[reportUnknownMemberType]
    found: list[Occurrence] = []
    for event in cast(list[Any], query.between(start, end)):
        if str(event.get("STATUS", "")).upper() == "CANCELLED":
            continue
        raw_start = event.decoded("DTSTART")
        all_day = not isinstance(raw_start, datetime)
        begins = _moment(raw_start, zone)
        if event.get("DTEND") is not None:
            ends = _moment(event.decoded("DTEND"), zone)
        elif event.get("DURATION") is not None:
            ends = begins + cast(timedelta, event.decoded("DURATION"))
        else:
            ends = begins + (timedelta(days=1) if all_day else timedelta(0))
        standing, organizer = _standing(event, owners)
        uid = str(event.get("UID", "")) or f"{begins.isoformat()}:{event.get('SUMMARY', '')}"
        occurrence = Occurrence(
            key=f"{uid}@{begins.isoformat()}",
            start=begins,
            end=ends,
            all_day=all_day,
            standing=standing,
            organizer=organizer,
        )
        if standing.visible:
            occurrence = replace(
                occurrence,
                title=_text(event.get("SUMMARY"), MAX_TITLE) or "(sans titre)",
                location=_text(event.get("LOCATION"), MAX_LOCATION),
                description=_text(event.get("DESCRIPTION"), MAX_DESCRIPTION)
                if standing is Standing.OWN
                else "",
            )
        found.append(occurrence)
    return sorted(found, key=lambda o: (o.start, o.key))
