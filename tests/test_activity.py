"""The activity page shows everything the journal holds, readable, searchable, page by page."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from orion.console.activity import read_activity
from orion.core.events import NewEvent, Provenance, Trust
from orion.core.journal import SqliteJournal

ZONE = ZoneInfo("Europe/Paris")
ORION = Provenance(source="orion:test", actor="orion:core")


def event(kind: str, **payload: object) -> NewEvent:
    return NewEvent(kind=kind, payload=payload, provenance=ORION, trust=Trust.SYSTEM)


async def test_every_kind_shows_and_bookkeeping_waits_one_tap_away(
    journal: SqliteJournal,
) -> None:
    await journal.append(
        [
            event("message.received", channel="telegram", text="Réserve chez Paul samedi"),
            event("tool.executed", tool="mail_send", arguments='{"to": "paul@x.fr"}'),
            event("model.usage", model="m", prompt_tokens=10, completion_tokens=2),
            event("booking.confirmed", title="Table chez Paul, samedi 20 h"),
            event("guardian.login.lent", site="impots.gouv.fr", password="hunter2"),
        ]
    )
    shown = (await read_activity(journal, ZONE)).entries
    kinds = [e.kind for e in shown]
    assert "model.usage" not in kinds  # bookkeeping is not in "everything"…
    assert {"message.received", "tool.executed", "booking.confirmed"} <= set(kinds)
    assert kinds.index("booking.confirmed") < kinds.index("message.received")  # newest first
    technical = (await read_activity(journal, ZONE, wanted="technical")).entries
    assert [e.kind for e in technical] == ["model.usage"]  # …but one filter away
    lent = next(e for e in shown if e.kind == "guardian.login.lent")
    assert "hunter2" not in str(lent.as_json())

    found = (await read_activity(journal, ZONE, query="paul samedi")).entries
    assert {e.kind for e in found} == {"message.received", "booking.confirmed"}


async def test_older_activity_comes_page_by_page(journal: SqliteJournal) -> None:
    await journal.append([event("web.searched", query=f"q{i}") for i in range(5)])
    first = await read_activity(journal, ZONE, limit=3)
    assert first.before is not None
    rest = await read_activity(journal, ZONE, limit=3, before=first.before)
    queries = [e.detail for e in first.entries + rest.entries]
    assert queries == [f"q{i}" for i in reversed(range(5))]
    assert rest.before is None
