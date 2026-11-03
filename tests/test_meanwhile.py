from __future__ import annotations

from zoneinfo import ZoneInfo

from orion.context.meanwhile import meanwhile
from orion.core.events import NewEvent, Provenance, Trust
from orion.core.journal import SqliteJournal


async def test_an_initiative_already_in_the_conversation_is_not_news(
    journal: SqliteJournal,
) -> None:
    """03/11: the 09:29 initiative, there in the conversation, came back « hors de cette
    conversation »: it is delivered under a correlation of its own."""
    sent = Provenance(source="orion:outbox", actor="orion:guardian")
    said = "deux projets Cloud ont des clients OAuth inactifs. tu les utilises encore ?"
    await journal.append(
        [
            NewEvent(
                kind="outbox.queued",
                payload={"text": text},
                provenance=sent,
                trust=Trust.SYSTEM,
                correlation_id=correlation,
            )
            for text, correlation in ((said, "delivery"), ("Agatha 180 €", "mission"))
        ]
    )
    block = meanwhile(
        await journal.read(0, 100), set(), ZoneInfo("Europe/Paris"), frozenset({said})
    )
    assert block is not None
    assert "Agatha" in block and "OAuth" not in block


async def test_what_orion_sent_from_elsewhere_is_shown_whole(journal: SqliteJournal) -> None:
    """03/11: a task result sent outside the conversation came back cut at 160 characters, and
    Orion told him it could not see the rest (D231)."""
    report = "Vols trouvés : " + "Paris-Lisbonne 89 €, " * 20 + "le dernier à 7 h 10."
    await journal.append(
        [
            NewEvent(
                kind="outbox.queued",
                payload={"text": report},
                provenance=Provenance(source="orion:outbox", actor="orion:guardian"),
                trust=Trust.SYSTEM,
                correlation_id="fallback",
            )
        ]
    )
    block = meanwhile(await journal.read(0, 100), set(), ZoneInfo("Europe/Paris"))
    assert block is not None and "le dernier à 7 h 10." in block
