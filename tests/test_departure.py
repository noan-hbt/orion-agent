"""The evening before a trip or an exam, Orion prepares it (D77)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from orion.calendar.ics import Occurrence, Standing
from orion.core.journal import SqliteJournal
from orion.departure.module import (
    Departures,
    DepartureSettings,
    departure_tools,
    papers_line,
    words,
)
from orion.papers.module import Paper
from orion.tools.base import ToolContext

PARIS = ZoneInfo("Europe/Paris")
CTX = ToolContext("owner", "c")
THURSDAY_7 = datetime(2026, 10, 22, 7, 10, tzinfo=PARIS)


def occurrence(title: str, start: datetime, location: str = "", days: int = 0) -> Occurrence:
    all_day = days > 0
    end = start + (timedelta(days=days) if all_day else timedelta(hours=2))
    return Occurrence(f"k-{title}", start, end, all_day, Standing.ACCEPTED, title, location)


class Agenda:
    def __init__(self, *events: Occurrence) -> None:
        self.events = list(events)

    def between(self, start: datetime, end: datetime) -> list[tuple[str, Occurrence]]:
        return [("perso", o) for o in self.events if o.end > start and o.start < end]


class Vault:
    def __init__(self, *papers: Paper) -> None:
        self._papers = list(papers)

    def papers(self) -> list[Paper]:
        return self._papers


def passport(valid_until: date) -> Paper:
    filed = datetime(2025, 1, 1, tzinfo=PARIS)
    return Paper("pa_1", "f_1", "Passeport", "passeport français", "", None, valid_until, filed)


def departures(
    journal: SqliteJournal,
    now: list[datetime],
    agenda: Agenda | None = None,
    vault: Vault | None = None,
) -> Departures:
    return Departures(
        journal,
        DepartureSettings(),
        "Europe/Paris",
        calendar=agenda,
        papers=vault,
        tools=("mail", "web_search"),
        clock=lambda: now[0],
    )


def test_words_ignore_case_and_accents() -> None:
    assert {"depart", "lisbonne"} <= words("Départ pour LISBONNE !")


async def test_a_flight_in_the_calendar_is_prepared_the_evening_before(
    journal: SqliteJournal,
) -> None:
    agenda = Agenda(
        occurrence("Vol TP433 pour Lisbonne", THURSDAY_7, "Orly"),
        occurrence("Déjeuner d'équipe", THURSDAY_7.replace(hour=12)),
    )
    now = [datetime(2026, 10, 21, 18, 0, tzinfo=PARIS)]
    trips = departures(journal, now, agenda, Vault(passport(date(2026, 12, 3))))
    assert await trips.alerts(now[0]) == []  # before the evening

    (alert,) = await trips.alerts(datetime(2026, 10, 21, 19, 5, tzinfo=PARIS))
    assert "Il part jeudi à 07:10 : Vol TP433 pour Lisbonne, Orly." in alert.notice
    assert "Passeport (passeport français) : valide jusqu'au 03/12/2026" in alert.notice
    assert "`mail`" in alert.notice and "`web_search`" in alert.notice
    assert "SILENCE" in alert.notice
    assert alert.expires_at == THURSDAY_7 - timedelta(hours=1)
    assert await trips.alerts(datetime(2026, 10, 22, 6, 30, tzinfo=PARIS)) == []  # too late


async def test_an_expired_passport_is_said_first(journal: SqliteJournal) -> None:
    agenda = Agenda(occurrence("Train pour Londres (Eurostar)", THURSDAY_7))
    now = [datetime(2026, 10, 21, 20, 0, tzinfo=PARIS)]
    trips = departures(journal, now, agenda, Vault(passport(date(2026, 9, 1))))
    (alert,) = await trips.alerts(now[0])
    assert "EXPIRÉ depuis le 01/09/2026" in alert.notice
    assert "expirée pour un voyage passe en premier" in alert.notice


def test_only_his_own_papers_with_an_end_count() -> None:
    filed = datetime(2025, 1, 1, tzinfo=PARIS)
    end = date(2027, 1, 1)
    mother = Paper("pa_2", "f_2", "Passeport de maman", "", "Maman", None, end, filed)
    payslip = Paper("pa_3", "f_3", "Paie", "fiche de paie", "", None, None, filed)
    assert papers_line([mother, payslip], date(2026, 10, 22)) == ""


async def test_an_exam_a_long_trip_and_ordinary_events(journal: SqliteJournal) -> None:
    friday = datetime(2026, 10, 23, 0, 0, tzinfo=PARIS)
    agenda = Agenda(
        occurrence(
            "Partiel de droit des contrats", THURSDAY_7.replace(hour=14, minute=0), "Amphi B"
        ),
        occurrence("Lisbonne", friday, "Lisbonne, Portugal", days=3),
        occurrence("Anniversaire", friday, "", days=1),
        occurrence("Réunion budget", THURSDAY_7.replace(hour=16)),
    )
    now = [datetime(2026, 10, 22, 19, 30, tzinfo=PARIS)]
    trips = departures(journal, now, agenda)
    alerts = await trips.alerts(now[0])
    notices = sorted(a.notice.splitlines()[0] for a in alerts)
    assert notices == ["Il part vendredi à 08:00 : Lisbonne, Lisbonne, Portugal."]
    now[0] = datetime(2026, 10, 21, 19, 30, tzinfo=PARIS)
    (exam,) = await trips.alerts(now[0])
    assert exam.notice.startswith("Il a jeudi à 14:00 : Partiel de droit des contrats, Amphi B.")
    assert "papiers rangés" not in exam.notice


async def test_a_departure_he_mentions_is_noted_listed_and_cancelled(
    journal: SqliteJournal,
) -> None:
    now = [datetime(2026, 10, 20, 12, 0, tzinfo=PARIS)]
    trips = departures(journal, now)
    plan, cancel, listing = departure_tools(trips)
    said = await plan.handler(
        plan.parse(
            json.dumps(
                {"what": "Train pour Lyon", "at": "2026-10-23 07:04", "where": "Gare de Lyon"}
            )
        ),
        CTX,
    )
    assert "jeudi à 19:00" in said.content
    shown = await listing.handler(listing.parse("{}"), CTX)
    assert "voyage : Train pour Lyon, vendredi 23/10 07:04, Gare de Lyon" in shown.content
    (alert,) = await trips.alerts(datetime(2026, 10, 22, 19, 0, tzinfo=PARIS))
    assert "Aucun papier" not in alert.notice  # no vault: nothing said about papers

    (departure_id,) = trips.state.planned
    await cancel.handler(cancel.parse(json.dumps({"departure": departure_id})), CTX)
    assert await trips.alerts(datetime(2026, 10, 22, 19, 0, tzinfo=PARIS)) == []
    past = await plan.handler(plan.parse('{"what": "Vol", "at": "2026-10-19 07:00"}'), CTX)
    assert past.is_error
