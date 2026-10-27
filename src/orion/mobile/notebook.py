"""The rest of his notebook in the app (D135): his papers, the people in his life, the pages of
his weeks, Orion's browser when a page needs him, and what Orion follows for him: his agenda,
the mails that matter, Orion's nights (D140).

Read from the same places as Orion's own tools; the few things he does here are his own, journaled
as his (a meeting with someone noted), or go straight into the page (the browser hand-over, D116:
what he types is never journaled nor logged).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from orion.chronicle.module import Chronicle, week_title
from orion.computer.browser import BrowserError
from orion.computer.direct import BrowserDesk, Refused
from orion.computer.handover import act_on, focused
from orion.core.events import NewEvent, Provenance, Trust
from orion.core.journal import EventStore
from orion.mail.card import Importance
from orion.memory.model import Category
from orion.memory.store import name_key
from orion.mobile.control import ControlError
from orion.papers.module import Vault
from orion.relations.module import CONTACT, Relations, next_birthday

if TYPE_CHECKING:
    from orion.calendar.service import Calendar
    from orion.mail.service import Mailbox
    from orion.thinking.module import Thinking

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
    calendar: Callable[[], Calendar | None] = lambda: None
    mailbox: Callable[[], Mailbox | None] = lambda: None
    thinking: Callable[[], Thinking | None] = lambda: None

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
            # Memory names places, companies and the owner himself in `about` too: only someone
            # known as a person, or followed as one (a birthday, a rhythm, a meeting), is shown.
            current = [f for f in person.facts if f.current]
            about = sum(f.category == Category.PERSON for f in current)
            known = about > 0 and about >= sum(f.category == Category.PROFILE for f in current)
            followed = key in state.birthdays or key in state.rhythms or key in state.contacts
            if relations.is_owner(person.name) or not (known or followed):
                continue
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

    # -- what Orion follows for him (D140) ----------------------------------------------------

    def agenda(self, days: int = 14) -> list[dict[str, Any]]:
        """The next `days` of his calendars, as Orion reads them (read only: adding goes
        through Orion and its approval)."""
        calendar = self.calendar()
        if calendar is None:
            raise ControlError("the calendar is off")
        now = self.clock()
        return [
            {
                "key": o.key,
                "account": account,
                "title": o.title or "(sans titre)",
                "start": o.start.isoformat(),
                "end": o.end.isoformat(),
                "all_day": o.all_day,
                "location": o.location,
            }
            for account, o in calendar.between(now, now + timedelta(days=days))
        ][:200]

    def mails(self, days: int = 14) -> list[dict[str, Any]]:
        """The mails worth it from the last `days`, as Orion filed them: who, what, a
        summary. Never the mail's text: the app shows what Orion made of it."""
        mailbox = self.mailbox()
        if mailbox is None:
            raise ControlError("mail is off")
        since = self.clock() - timedelta(days=days)
        shown: list[dict[str, Any]] = []
        for m in mailbox.recent(since, worth_it=True)[:60]:
            c = m.card
            shown.append(
                {
                    "id": m.id,
                    "sender": m.sender,
                    "from": m.from_addr,
                    "subject": m.subject or "(sans objet)",
                    "at": m.received_at.isoformat(),
                    "summary": c.summary,
                    "category": c.category.value,
                    "important": c.importance is Importance.HIGH,
                    "needs_reply": c.needs_reply,
                    "due": c.due.isoformat() if c.due else None,
                    "amount": c.amount,
                    "currency": c.currency or "",
                }
            )
        return shown

    async def nights(self) -> dict[str, Any]:
        """Orion's nights (D117): his notes for the morning, what each night read and cost,
        and the budget he has for one."""
        thinking = self.thinking()
        if thinking is None:
            raise ControlError("the nights are off")
        await thinking.sync()
        notes = [t for t in thinking.state.thoughts if not t.question][-30:]
        return {
            "budget_usd": thinking.settings.budget_usd,
            "nights": list(reversed(thinking.state.nights))[:30],
            "notes": [
                {
                    "at": t.at.isoformat(),
                    "note": t.text,
                    "trusted": t.trust >= Trust.SYSTEM,
                }
                for t in reversed(notes)
            ],
        }

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
            offer = next((a for a in desk.asked() if a["token"] == found.offered), None)
            return {
                "done": "Merci, je reprends." if do == "done" else "D'accord, je laisse tomber.",
                "keep": offer,  # the login he just signed in with: the app asks at once (D142)
            }
        try:
            if do == "signin":  # his phone's passwords, typed into the page's form (D142)
                await desk.sign_in(
                    token, str(body.get("username", ""))[:200], str(body.get("password", ""))[:500]
                )
                do = "start"
            size: dict[str, int] = await desk.on_tab(found.thread, lambda s: act_on(s, do, body))
            extra: dict[str, Any] = {}
            if do == "type":  # into a sign-in form: offered to keep once he is done (D120)
                desk.note_typed(token, await desk.on_tab(found.thread, focused))
            elif do == "tap":  # the field he touched, for the app's keyboard to fit it (D143)
                touched = await desk.on_tab(found.thread, focused)
                extra["field"] = touched["type"] if touched else None
            form = await desk.on_tab(found.thread, lambda s: s.sign_in_form())
        except (BrowserError, Refused) as exc:
            raise ControlError(f"Pas pu : {exc}") from exc
        tab = desk.tab(found.thread)
        return {"url": tab.page.url if tab else "", **size, "form": form, **extra}

    # -- logins Orion asks him for, or offers to keep (D142) ------------------------------------

    def logins_asked(self) -> list[dict[str, str]]:
        desk = self.browser()
        return desk.asked() if desk is not None else []

    async def login_answer(self, token: str, answer: str, name: str = "") -> dict[str, str]:
        """`keep` (the login he signed in with, into the vault), `no`, or `saved` (he filled
        Orion's request: the app put `name` in the vault itself)."""
        desk = self._desk()
        try:
            if answer == "keep":
                return {"done": f"Gardé dans mon coffre, sous « {await desk.keep(token)} »."}
            if answer == "saved":
                await desk.saved(token, name)
                return {"done": f"Gardé dans mon coffre, sous « {name} »."}
        except Refused as exc:
            raise ControlError(str(exc)) from exc
        if answer != "no":
            raise ValueError("keep, saved or no")
        desk.decline(token)
        return {"done": "D'accord, je ne le garde pas."}


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
