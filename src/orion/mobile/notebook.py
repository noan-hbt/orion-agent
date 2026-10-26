"""The rest of his notebook in the app (D135): his papers, the people in his life, the pages of
his weeks, and Orion's browser when a page needs him.

Read from the same places as Orion's own tools; the few things he does here are his own, journaled
as his (a meeting with someone noted), or go straight into the page (the browser hand-over, D116:
what he types is never journaled nor logged).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from orion.chronicle.module import Chronicle, week_title
from orion.computer.browser import BrowserError
from orion.computer.direct import BrowserDesk, Refused
from orion.computer.handover import act_on, focused
from orion.core.events import NewEvent, Provenance, Trust
from orion.core.journal import EventStore
from orion.memory.store import name_key
from orion.mobile.control import ControlError
from orion.papers.module import Vault
from orion.relations.module import CONTACT, Relations, next_birthday

FROM_APP = Provenance(source="app", actor="owner:main")
EXPIRING = timedelta(days=90)


@dataclass
class Notebook:
    journal: EventStore
    clock: Callable[[], datetime]
    papers: Callable[[], Vault | None] = lambda: None
    relations: Callable[[], Relations | None] = lambda: None
    chronicle: Callable[[], Chronicle | None] = lambda: None
    browser: Callable[[], BrowserDesk | None] = lambda: None

    # -- his papers -----------------------------------------------------------------------------

    async def _vault(self) -> Vault:
        vault = self.papers()
        if vault is None:
            raise ControlError("papers are off")
        await vault.sync()
        return vault

    async def paper_list(self, query: str = "") -> list[dict[str, Any]]:
        vault = await self._vault()
        today = self.clock().date()
        shown: list[dict[str, Any]] = []
        for paper in await vault.search(query, limit=80):
            kept = vault.store.get(paper.file_id)
            until = paper.valid_until
            shown.append(
                {
                    "id": paper.id,
                    "label": paper.label,
                    "about": paper.about,
                    "holder": paper.holder,
                    "issued": paper.issued.isoformat() if paper.issued else None,
                    "valid_until": until.isoformat() if until else None,
                    "expired": until is not None and until < today,
                    "expiring": until is not None and today <= until <= today + EXPIRING,
                    "type": kept.content_type if kept else "",
                }
            )
        return shown

    async def paper_file(self, paper_id: str) -> tuple[Path, str, str]:
        """(the file, its type, a name to save it under), only from the vault's folder."""
        vault = await self._vault()
        paper = vault.state.papers.get(paper_id)
        kept = vault.store.get(paper.file_id) if paper is not None else None
        if paper is None or kept is None:
            raise ControlError("no such paper")
        path = kept.path.resolve()
        if not path.is_relative_to(vault.store.folder.resolve()) or not path.is_file():
            raise ControlError("no such paper")
        return path, kept.content_type, kept.name

    # -- the people in his life -------------------------------------------------------------

    async def _relations(self) -> Relations:
        relations = self.relations()
        if relations is None:
            raise ControlError("relations are off")
        await relations.sync()
        return relations

    async def people(self) -> list[dict[str, Any]]:
        relations = await self._relations()
        index, _aliases = await relations.people()
        state = relations.state
        today = self.clock().date()
        shown: list[dict[str, Any]] = []
        for key, person in index.items():
            birthday = state.birthdays.get(key)
            rhythm = state.rhythms.get(key)
            last = state.contacts.get(key)
            due = None
            if rhythm is not None:
                due = (last.date() if last else today) + timedelta(days=rhythm[0])
            facts = [f for f in person.facts if f.current]
            shown.append(
                {
                    "name": person.name,
                    "facts": [f.statement for f in facts[-8:]],
                    "trusted": all(f.trust >= Trust.SYSTEM for f in facts),
                    "commitments": [c.what for c in person.commitments if c.status.value == "open"],
                    "birthday": _birthday(birthday, today),
                    "every_days": rhythm[0] if rhythm else None,
                    "last_seen": last.date().isoformat() if last else None,
                    "due": due.isoformat() if due else None,
                    "overdue": due is not None and due < today,
                }
            )
        return sorted(shown, key=lambda p: (not p["overdue"], -len(p["facts"]), p["name"]))

    async def saw(self, name: str) -> None:
        """He saw them today: the rhythm counts from now."""
        relations = await self._relations()
        index, _aliases = await relations.people()
        if name_key(name) not in index:
            raise ControlError("no such person")
        await self.journal.append(
            [
                NewEvent(
                    kind=CONTACT,
                    payload={
                        "person": name,
                        "at": self.clock().isoformat(),
                        "how": "noté dans l'app",
                    },
                    provenance=FROM_APP,
                    trust=Trust.OWNER,
                )
            ]
        )
        await relations.sync()

    # -- the pages of his weeks -------------------------------------------------------------

    async def pages(self) -> list[dict[str, Any]]:
        chronicle = self.chronicle()
        if chronicle is None:
            raise ControlError("the chronicle is off")
        await chronicle.sync()
        return [
            {
                "week": page.week.isoformat(),
                "title": week_title(page.week),
                "text": page.text,
                "trusted": page.trust >= Trust.SYSTEM,
            }
            for page in sorted(chronicle.state.pages.values(), key=lambda p: p.week, reverse=True)
        ][:60]

    # -- Orion's browser, handed to him (D116) ----------------------------------------------

    def _desk(self) -> BrowserDesk:
        desk = self.browser()
        if desk is None:
            raise ControlError("the browser is off")
        return desk

    def handovers(self) -> list[dict[str, str]]:
        desk = self.browser()
        if desk is None:
            return []
        return [
            {"token": token, "why": found.why}
            for token, found in list(desk.handovers.items())
            if desk.handover(token) is not None
        ]

    async def shot(self, token: str) -> bytes:
        desk = self._desk()
        found = desk.handover(token)
        if found is None:
            raise ControlError("that hand-over is over")
        try:
            picture: bytes = await desk.on_tab(found.thread, lambda s: s.screenshot(full=False))
        except (BrowserError, Refused) as exc:
            raise ControlError(f"no picture: {exc}") from exc
        return picture

    async def act(self, token: str, body: dict[str, Any]) -> dict[str, Any]:
        """One of his actions on the tab, as from Telegram's live view."""
        desk = self._desk()
        found = desk.handover(token)
        if found is None:
            raise ControlError("that hand-over is over")
        do = str(body.get("do", ""))
        if do in ("done", "cancel"):
            await desk.give_back(token, done=do == "done")
            return {
                "done": "Merci, je reprends." if do == "done" else "D'accord, je laisse tomber."
            }
        try:
            size: dict[str, int] = await desk.on_tab(found.thread, lambda s: act_on(s, do, body))
            if do == "type":  # into a sign-in form: offered to keep once he is done (D120)
                desk.note_typed(token, await desk.on_tab(found.thread, focused))
        except (BrowserError, Refused) as exc:
            raise ControlError(f"Pas pu : {exc}") from exc
        tab = desk.tab(found.thread)
        return {"url": tab.page.url if tab else "", **size}


def _birthday(found: tuple[int, int, int | None, str] | None, today: date) -> dict[str, Any] | None:
    if found is None:
        return None
    month, day, year, _name = found
    coming = next_birthday(month, day, today)
    return {
        "next": coming.isoformat(),
        "in_days": (coming - today).days,
        "age": coming.year - year if year else None,
    }
