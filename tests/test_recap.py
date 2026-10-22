"""The evening recap: the last point of the day opens with what Orion did (D93)."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from orion.core.events import NewEvent, Provenance, Trust
from orion.core.journal import SqliteJournal
from orion.core.projections import ProjectionRunner
from orion.recap.module import EXECUTED, Recap, RecapState

PARIS = ZoneInfo("Europe/Paris")


def executed(tool: str, arguments: dict[str, object], error: bool = False) -> NewEvent:
    return NewEvent(
        kind=EXECUTED,
        payload={"tool": tool, "arguments": json.dumps(arguments), "is_error": error},
        provenance=Provenance(source="thread:owner", actor="orion:core"),
        trust=Trust.SYSTEM,
    )


async def test_the_last_point_tells_what_orion_did_today(journal: SqliteJournal) -> None:
    await journal.append(
        [
            executed("contact_message", {"contact": "Angèle", "text": "Il arrive à 19 h"}),
            executed("mail_send", {"to": ["a@b.fr"], "subject": "Devis"}, error=True),
            executed("calendar_add", {"title": "Dentiste"}),
            executed("memory_search", {"query": "x"}),  # a read: not something done for him
            # Opening his account is not something done: seen for real, « connecté à ton
            # compte » after a sign-in that had failed on the site.
            executed("browser", {"action": "login", "session": "hyperplanning"}),
            executed("browser", {"action": "send", "what": "réserver la salle B12"}),
        ]
    )
    state = RecapState(PARIS)
    recap = Recap(ProjectionRunner(journal, [state]), state)
    now = datetime.now(PARIS)
    assert await recap.digest(now, now, first_slot=True, last_slot=False) == []
    [line] = await recap.digest(now, now, first_slot=False, last_slot=True)
    assert "écrit à Angèle : « Il arrive à 19 h »" in line.text
    assert "ajouté à ton agenda : « Dentiste »" in line.text
    assert "Devis" not in line.text and "memory" not in line.text
    assert "fait une démarche en ligne : « réserver la salle B12 »" in line.text
    assert "compte" not in line.text
    # If Orion's own wording fails, he reads this, never Orion's note to itself.
    assert line.plain.startswith("Aujourd'hui, j'ai :") and "ouvre le point" not in line.plain
