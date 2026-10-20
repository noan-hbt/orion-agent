"""Writing to the owner's calendar: CalDAV (iCloud, Fastmail, Nextcloud…) or Google Calendar.

Reading stays on the secret iCal address; writing needs its own credentials, which only the
guardian holds when Orion runs split (D28). An event Orion adds has no attendees: it goes on the
owner's calendar and invites nobody, so it speaks to no one on the owner's behalf.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

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


WriteEvent = Callable[[str, NewCalendarEvent, str | None], Awaitable[str]]
"""(account, event, grant) → the event's UID."""


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
