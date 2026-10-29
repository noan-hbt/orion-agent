"""Writing to the owner's calendar: CalDAV (iCloud, Fastmail, Nextcloud…) or Google Calendar.

Reading stays on the secret iCal address; writing needs its own credentials, which only the
guardian holds when Orion runs split (D28). An event Orion adds has no attendees: it goes on the
owner's calendar and invites nobody, so it speaks to no one on the owner's behalf. For the same
reason Orion changes or cancels only the owner's own single events without guests (D189): moving
a meeting with guests would move it for them, and a series is the owner's to reshape.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, tzinfo
from typing import Annotated, Any, Literal, cast
from xml.etree import ElementTree
from xml.sax.saxutils import escape

import httpx
import icalendar
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
    a new start without a new end keeps the event's length."""

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


def _check(change: CalendarChange, title: str, start: datetime, single: bool, guests: bool) -> None:
    """Fail closed: only the very event the owner approved, his own, alone, without guests."""
    if not single:
        raise CalendarWriteError("a recurring event: Orion changes single events only")
    if guests:
        raise CalendarWriteError("it has guests: changing it would speak to them")
    if title != change.title or start.astimezone(UTC) != change.start.astimezone(UTC):
        raise CalendarWriteError("the event is no longer the one the owner saw")


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
        raise CalendarWriteError(f"CalDAV answered {response.status_code}")
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
    events: list[Any] = list(calendar.walk("VEVENT"))
    event = events[0]
    start, end, _ = span(event, change.start.tzinfo or UTC)
    _check(
        change,
        shown_title(event.get("SUMMARY")),
        start,
        single=len(events) == 1 and event.get("RRULE") is None,
        guests=event.get("ATTENDEE") is not None,
    )
    headers = {"if-match": etag} if etag else {}
    if change.cancel:
        response = await http.delete(url, headers=headers, auth=auth)
    else:
        _edit(event, change, start, end)
        response = await http.put(
            url,
            content=calendar.to_ical(),
            headers={"content-type": "text/calendar; charset=utf-8", **headers},
            auth=auth,
        )
    if response.status_code not in (200, 201, 204):
        raise CalendarWriteError(f"CalDAV answered {response.status_code}")


def _edit(event: Any, change: CalendarChange, start: datetime, end: datetime) -> None:
    def put(name: str, value: object) -> None:
        event.pop(name, None)
        if value != "":
            event.add(name, value)

    if change.new_start is not None or change.new_end is not None:
        begins, ends = change.times(start, end)
        event.pop("DURATION", None)
        put("DTSTART", begins.astimezone(UTC))
        put("DTEND", ends.astimezone(UTC))
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
    response = await http.get(url, params={"iCalUID": change.uid}, headers=headers)
    if response.status_code != 200:
        raise CalendarWriteError(f"Google Calendar answered {response.status_code}")
    items = cast(list[dict[str, Any]], response.json().get("items") or [])
    if not items:
        raise CalendarWriteError("that event is no longer in the calendar")
    item = items[0]
    zone = change.start.tzinfo or UTC
    start, end = _google_time(item["start"], zone), _google_time(item["end"], zone)
    _check(
        change,
        shown_title(item.get("summary")),
        start,
        single=len(items) == 1 and not item.get("recurrence") and not item.get("recurringEventId"),
        guests=bool(item.get("attendees")),
    )
    target = f"{url}/{item['id']}"
    if change.cancel:
        response = await http.delete(target, params={"sendUpdates": "none"}, headers=headers)
        if response.status_code not in (200, 204, 410):
            raise CalendarWriteError(f"Google Calendar answered {response.status_code}")
        return
    body: dict[str, object] = {}
    if change.new_start is not None or change.new_end is not None:
        begins, ends = change.times(start, end)
        body["start"] = {"dateTime": begins.isoformat(), "date": None}
        body["end"] = {"dateTime": ends.isoformat(), "date": None}
    for name, value in (
        ("summary", change.new_title),
        ("location", change.new_location),
        ("description", change.new_notes),
    ):
        if value is not None:
            body[name] = value
    response = await http.patch(target, json=body, params={"sendUpdates": "none"}, headers=headers)
    if response.status_code != 200:
        raise CalendarWriteError(f"Google Calendar answered {response.status_code}")


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
