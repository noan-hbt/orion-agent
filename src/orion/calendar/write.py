"""Writing to the owner's calendar: CalDAV (iCloud, Fastmail, Nextcloud…) or Google Calendar.

Reading stays on the secret iCal address; writing needs its own credentials, which only the
guardian holds when Orion runs split (D28). An event Orion adds has no attendees: it goes on the
owner's calendar and invites nobody, so it speaks to no one on the owner's behalf. Changing or
cancelling an event he approved (D189) goes through the provider like his own click would:
guests are told, and an invitation's organizer keeps the last word (the provider refuses).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Annotated, Any, Literal, cast
from xml.etree import ElementTree
from xml.sax.saxutils import escape

import httpx
import icalendar
import recurring_ical_events  # pyright: ignore[reportMissingTypeStubs]
from pydantic import BaseModel, ConfigDict, Field

from orion.calendar.ics import shown_title, span
from orion.config import SecretRef
from orion.core.ids import new_id

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/{calendar}/events"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/calendar.events"


class CalDavWriter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["caldav"] = "caldav"
    url: str = Field(description="The calendar collection, e.g. https://caldav.fastmail.com/…/")
    username: str
    password: SecretRef = Field(description="An app password")


class GoogleWriter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["google"] = "google"
    calendar_id: str = "primary"
    client_id: str
    client_secret: SecretRef
    refresh_token: SecretRef = Field(description="From `orion google auth`")


Writer = Annotated[CalDavWriter | GoogleWriter, Field(discriminator="kind")]


class NewCalendarEvent(BaseModel):
    """What Orion asks to add. `meeting` names a meeting agreed with another Orion (D29): the
    guardian checks it against the confirmation it signed or received."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(min_length=1, max_length=120)
    start: datetime
    end: datetime
    location: str = Field(default="", max_length=200)
    notes: str = Field(default="", max_length=1000)
    meeting: str | None = None


class CalendarWriteError(RuntimeError):
    """The calendar refused or could not be reached."""


class CalendarChange(BaseModel):
    """What Orion asks to change on one of the owner's events, found by its UID and checked
    against the title and start the owner saw when he approved. `None` keeps a field as it is;
    a new start without a new end keeps the event's length. In a recurring event, `series`
    changes every occurrence (moved by as much as this one), otherwise this one alone."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    uid: str = Field(min_length=1)
    title: str
    start: datetime
    new_title: str | None = Field(default=None, min_length=1, max_length=120)
    new_start: datetime | None = None
    new_end: datetime | None = None
    new_location: str | None = Field(default=None, max_length=200)
    new_notes: str | None = Field(default=None, max_length=1000)
    cancel: bool = False
    series: bool = False

    def times(self, start: datetime, end: datetime) -> tuple[datetime, datetime]:
        """The event's start and end once changed, from what they are now."""
        begins = self.new_start or start
        ends = self.new_end or begins + (end - start)
        if ends <= begins:
            raise CalendarWriteError("the end must come after the start")
        return begins, ends


WriteEvent = Callable[[str, NewCalendarEvent, str | None], Awaitable[str]]
"""(account, event, grant) → the event's UID."""
ChangeEvent = Callable[[str, CalendarChange, str | None], Awaitable[None]]
"""(account, change, grant)"""


def _ics_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ics_time(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def to_ics(event: NewCalendarEvent, uid: str, now: datetime) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Orion//FR",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{_ics_time(now)}",
        f"DTSTART:{_ics_time(event.start)}",
        f"DTEND:{_ics_time(event.end)}",
        f"SUMMARY:{_ics_text(event.title)}",
    ]
    if event.location:
        lines.append(f"LOCATION:{_ics_text(event.location)}")
    if event.notes:
        lines.append(f"DESCRIPTION:{_ics_text(event.notes)}")
    lines += ["END:VEVENT", "END:VCALENDAR", ""]
    return "\r\n".join(lines)


async def write_event(
    http: httpx.AsyncClient, writer: CalDavWriter | GoogleWriter, event: NewCalendarEvent
) -> str:
    try:
        if isinstance(writer, CalDavWriter):
            return await _caldav(http, writer, event)
        return await _google(http, writer, event)
    except httpx.HTTPError as exc:
        raise CalendarWriteError(f"calendar unreachable: {exc!r}") from exc


async def _caldav(http: httpx.AsyncClient, writer: CalDavWriter, event: NewCalendarEvent) -> str:
    uid = f"orion-{new_id()}"
    response = await http.put(
        writer.url.rstrip("/") + f"/{uid}.ics",
        content=to_ics(event, uid, datetime.now(UTC)),
        headers={"content-type": "text/calendar; charset=utf-8", "if-none-match": "*"},
        auth=(writer.username, writer.password.resolve().get_secret_value()),
    )
    if response.status_code not in (200, 201, 204):
        raise CalendarWriteError(f"CalDAV answered {response.status_code}")
    return uid


async def google_access_token(http: httpx.AsyncClient, writer: GoogleWriter) -> str:
    response = await http.post(
        GOOGLE_TOKEN_URL,
        data={
            "client_id": writer.client_id,
            "client_secret": writer.client_secret.resolve().get_secret_value(),
            "refresh_token": writer.refresh_token.resolve().get_secret_value(),
            "grant_type": "refresh_token",
        },
    )
    if response.status_code != 200:
        raise CalendarWriteError(f"Google refused the refresh token ({response.status_code})")
    return str(response.json()["access_token"])


async def _google(http: httpx.AsyncClient, writer: GoogleWriter, event: NewCalendarEvent) -> str:
    token = await google_access_token(http, writer)
    body: dict[str, object] = {
        "summary": event.title,
        "start": {"dateTime": event.start.isoformat()},
        "end": {"dateTime": event.end.isoformat()},
        "source": {"title": "Orion", "url": "https://github.com/noan-hbt/orion-agent"},
    }
    if event.location:
        body["location"] = event.location
    if event.notes:
        body["description"] = event.notes
    response = await http.post(
        GOOGLE_EVENTS_URL.format(calendar=writer.calendar_id),
        json=body,
        headers={"authorization": f"Bearer {token}"},
        params={"sendUpdates": "none"},
    )
    if response.status_code not in (200, 201):
        raise CalendarWriteError(f"Google Calendar answered {response.status_code}")
    return str(response.json().get("iCalUID") or response.json()["id"])


# -- changing an event --------------------------------------------------------------------------

_CALDAV_QUERY = """<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
<d:prop><d:getetag/><c:calendar-data/></d:prop>
<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">
<c:prop-filter name="UID"><c:text-match collation="i;octet">{uid}</c:text-match></c:prop-filter>
</c:comp-filter></c:comp-filter></c:filter>
</c:calendar-query>"""
_DAV = "{DAV:}"
_CAL = "{urn:ietf:params:xml:ns:caldav}"


def _same(change: CalendarChange, title: str, start: datetime) -> None:
    """Only the very event the owner approved: if it changed since, nothing is written."""
    if title != change.title or start.astimezone(UTC) != change.start.astimezone(UTC):
        raise CalendarWriteError("the event is no longer the one the owner saw")


def _answered(provider: str, status: int) -> CalendarWriteError:
    if status == 403:
        return CalendarWriteError(f"{provider} refused: only the organizer can change this event")
    return CalendarWriteError(f"{provider} answered {status}")


def _moved(change: CalendarChange) -> bool:
    return change.new_start is not None or change.new_end is not None


def _for_series(
    change: CalendarChange, start: datetime, end: datetime, first: datetime
) -> CalendarChange:
    """The change of one occurrence, made to the whole series: moved by as much."""
    if not _moved(change):
        return change
    begins, ends = change.times(start, end)
    return change.model_copy(
        update={"new_start": first + (begins - start), "new_end": first + (ends - start)}
    )


async def change_event(
    http: httpx.AsyncClient, writer: CalDavWriter | GoogleWriter, change: CalendarChange
) -> None:
    try:
        if isinstance(writer, CalDavWriter):
            await _caldav_change(http, writer, change)
        else:
            await _google_change(http, writer, change)
    except httpx.HTTPError as exc:
        raise CalendarWriteError(f"calendar unreachable: {exc!r}") from exc


async def _caldav_change(
    http: httpx.AsyncClient, writer: CalDavWriter, change: CalendarChange
) -> None:
    auth = (writer.username, writer.password.resolve().get_secret_value())
    response = await http.request(
        "REPORT",
        writer.url,
        content=_CALDAV_QUERY.format(uid=escape(change.uid)),
        headers={"depth": "1", "content-type": "application/xml; charset=utf-8"},
        auth=auth,
    )
    if response.status_code != 207:
        raise _answered("CalDAV", response.status_code)
    found = [
        (str(item.findtext(f"{_DAV}href") or ""), item.findtext(f".//{_DAV}getetag"), data)
        for item in ElementTree.fromstring(response.content).iter(f"{_DAV}response")
        if (data := item.findtext(f".//{_CAL}calendar-data"))
    ]
    if not found or not found[0][0]:
        raise CalendarWriteError("that event is no longer in the calendar")
    href, etag, data = found[0]
    url = str(httpx.URL(writer.url).join(href))
    calendar: Any = icalendar.Calendar.from_ical(data)
    zone = change.start.tzinfo or UTC
    events: list[Any] = list(calendar.walk("VEVENT"))
    master = next((e for e in events if e.get("RECURRENCE-ID") is None), events[0])
    overrides = [e for e in events if e.get("RECURRENCE-ID") is not None]
    override = next((e for e in overrides if _utc(span(e, zone)[0]) == _utc(change.start)), None)
    seen = override if override is not None else _occurrence(calendar, change)
    start, end, _ = span(seen, zone)
    _same(change, shown_title(seen.get("SUMMARY")), start)
    recurring = master.get("RRULE") is not None or master.get("RDATE") is not None
    headers = {"if-match": etag} if etag else {}
    if change.cancel and (change.series or not recurring):
        response = await http.delete(url, headers=headers, auth=auth)
    else:
        if not recurring:
            _edit(master, change, start, end)
        elif change.series:
            _edit(master, _for_series(change, start, end, span(master, zone)[0]), start, end)
        else:
            _one_occurrence(calendar, master, override, change, start, end)
        response = await http.put(
            url,
            content=calendar.to_ical(),
            headers={"content-type": "text/calendar; charset=utf-8", **headers},
            auth=auth,
        )
    if response.status_code not in (200, 201, 204):
        raise _answered("CalDAV", response.status_code)


def _utc(moment: datetime) -> datetime:
    return moment.astimezone(UTC)


def _occurrence(calendar: Any, change: CalendarChange) -> Any:
    """The occurrence the owner saw, whether the event repeats or not."""
    zone = change.start.tzinfo or UTC
    query: Any = recurring_ical_events.of(calendar)  # pyright: ignore[reportUnknownMemberType]
    window = cast(list[Any], query.between(change.start, change.start + timedelta(minutes=1)))
    for event in window:
        if _utc(span(event, zone)[0]) == _utc(change.start):
            return event
    raise CalendarWriteError("the event is no longer the one the owner saw")


def _like(reference: date | datetime, moment: datetime) -> date | datetime:
    """`moment` written the way `reference` is (all day, floating, or in its zone)."""
    if not isinstance(reference, datetime):
        return moment.date()
    if reference.tzinfo is None:
        return moment.replace(tzinfo=None)
    return moment.astimezone(reference.tzinfo)


def _one_occurrence(
    calendar: Any,
    master: Any,
    override: Any,
    change: CalendarChange,
    start: datetime,
    end: datetime,
) -> None:
    """Change or cancel one occurrence of a series, leaving the others as they are."""
    original = master.decoded("DTSTART")
    recurrence = (
        override.decoded("RECURRENCE-ID") if override is not None else _like(original, start)
    )
    if change.cancel:
        if override is not None:
            calendar.subcomponents.remove(override)
        master.add("EXDATE", recurrence)
        return
    if override is None:
        override = deepcopy(master)
        for name in ("RRULE", "RDATE", "EXDATE", "DTSTART", "DTEND", "DURATION"):
            override.pop(name, None)
        override.add("RECURRENCE-ID", recurrence)
        override.add("DTSTART", _like(original, start))
        override.add("DTEND", _like(original, end))
        calendar.add_component(override)
    _edit(override, change, start, end)


def _edit(event: Any, change: CalendarChange, start: datetime, end: datetime) -> None:
    def put(name: str, value: object) -> None:
        event.pop(name, None)
        if value != "":
            event.add(name, value)

    if _moved(change):
        begins, ends = change.times(start, end)
        reference = event.decoded("DTSTART")
        timed = isinstance(reference, datetime) and reference.tzinfo is not None
        event.pop("DURATION", None)
        put("DTSTART", _like(reference, begins) if timed else _utc(begins))
        put("DTEND", _like(reference, ends) if timed else _utc(ends))
    for name, value in (
        ("SUMMARY", change.new_title),
        ("LOCATION", change.new_location),
        ("DESCRIPTION", change.new_notes),
    ):
        if value is not None:
            put(name, value)
    now = datetime.now(UTC)
    put("SEQUENCE", int(event.get("SEQUENCE", 0)) + 1)
    put("DTSTAMP", now)
    put("LAST-MODIFIED", now)


def _google_time(value: dict[str, Any], zone: tzinfo) -> datetime:
    if "dateTime" in value:
        return datetime.fromisoformat(str(value["dateTime"]))
    return datetime.combine(date.fromisoformat(str(value["date"])), time(0), zone)


async def _google_change(
    http: httpx.AsyncClient, writer: GoogleWriter, change: CalendarChange
) -> None:
    token = await google_access_token(http, writer)
    headers = {"authorization": f"Bearer {token}"}
    url = GOOGLE_EVENTS_URL.format(calendar=writer.calendar_id)
    zone = change.start.tzinfo or UTC

    async def listed(where: str, params: dict[str, str]) -> list[dict[str, Any]]:
        response = await http.get(where, params=params, headers=headers)
        if response.status_code != 200:
            raise _answered("Google Calendar", response.status_code)
        return cast(list[dict[str, Any]], response.json().get("items") or [])

    items = await listed(url, {"iCalUID": change.uid})
    if not items:
        raise CalendarWriteError("that event is no longer in the calendar")
    master = next((i for i in items if not i.get("recurringEventId")), items[0])
    item = master
    if master.get("recurrence"):
        window = {
            "timeMin": change.start.isoformat(),
            "timeMax": (change.start + timedelta(minutes=1)).isoformat(),
        }
        instances = await listed(f"{url}/{master['id']}/instances", window)
        item = next(
            (i for i in instances if _utc(_google_time(i["start"], zone)) == _utc(change.start)),
            master,
        )
    start, end = _google_time(item["start"], zone), _google_time(item["end"], zone)
    _same(change, shown_title(item.get("summary")), start)
    if change.series and master.get("recurrence"):
        change = _for_series(change, start, end, _google_time(master["start"], zone))
        item = master
        start, end = _google_time(master["start"], zone), _google_time(master["end"], zone)
    target = f"{url}/{item['id']}"
    updates = {"sendUpdates": "all"}  # his guests learn of it, as from his own click
    if change.cancel:
        response = await http.delete(target, params=updates, headers=headers)
        if response.status_code not in (200, 204, 410):
            raise _answered("Google Calendar", response.status_code)
        return
    body: dict[str, object] = {}
    if _moved(change):
        begins, ends = change.times(start, end)
        kept = {"timeZone": item["start"]["timeZone"]} if item["start"].get("timeZone") else {}
        body["start"] = {"dateTime": begins.isoformat(), "date": None, **kept}
        body["end"] = {"dateTime": ends.isoformat(), "date": None, **kept}
    for name, value in (
        ("summary", change.new_title),
        ("location", change.new_location),
        ("description", change.new_notes),
    ):
        if value is not None:
            body[name] = value
    response = await http.patch(target, json=body, params=updates, headers=headers)
    if response.status_code != 200:
        raise _answered("Google Calendar", response.status_code)


GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
LOOPBACK = "http://127.0.0.1:8765/"


def google_consent_url(client_id: str, state: str) -> str:
    """Where the owner agrees to let Orion add events (a 'Desktop app' OAuth client)."""
    query = httpx.QueryParams(
        {
            "client_id": client_id,
            "redirect_uri": LOOPBACK,
            "response_type": "code",
            "scope": GOOGLE_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    return f"{GOOGLE_AUTH_URL}?{query}"


async def google_refresh_token(
    http: httpx.AsyncClient, client_id: str, client_secret: str, redirected: str, state: str
) -> str:
    """From the address the browser was sent back to (it fails to load: that is expected)."""
    params = httpx.URL(redirected.strip()).params
    if params.get("state") != state:
        raise CalendarWriteError("that address does not come from this consent")
    code = params.get("code")
    if not code:
        raise CalendarWriteError(f"no code in that address ({params.get('error', 'refused')})")
    response = await http.post(
        GOOGLE_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": LOOPBACK,
            "grant_type": "authorization_code",
        },
    )
    if response.status_code != 200 or "refresh_token" not in response.json():
        raise CalendarWriteError(f"Google refused the code ({response.status_code})")
    return str(response.json()["refresh_token"])
