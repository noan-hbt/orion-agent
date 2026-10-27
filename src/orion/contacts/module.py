"""His address book: `[modules.contacts]` (on by default; empty until he imports it).

Orion knew the people in the owner's life only from what he said of them. His phone knows
them all: their numbers, their addresses, their birthdays. He exports his contacts as one
`.vcf` file (iPhone: Contacts, select all, share; Android: Contacts, export) and gives it to
Orion from the app (Réglages) or with `orion contacts import <file>`.

Each card is journaled once, and again only when it changed (`contact.card`). `contacts` (in
the drawer) finds someone's number or address. A birthday on a card becomes a birthday the
relations module watches (`relation.birthday`, D29), unless it is the owner's own card.
Nothing is sent anywhere, and nothing is learnt as a fact: the address book stays an address
book, read when needed.

    contact.card  contacts.imported
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel, ConfigDict, Field

from orion.contacts.vcard import Card, parse
from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.memory.store import name_key
from orion.modules.base import Module, ModuleSettings, Plate
from orion.relations.module import BIRTHDAY
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from orion.config import OrionConfig

log = structlog.get_logger(__name__)

CARD = "contact.card"
IMPORTED = "contacts.imported"
CARD_TRUST = Trust.SYSTEM
"""His own address book, imported by him: not his words, but not a stranger's either."""
FOUND_AT_MOST = 8


class ContactsSettings(ModuleSettings):
    enabled: bool = True


@dataclass(frozen=True)
class Imported:
    added: int
    changed: int
    unchanged: int
    birthdays: int

    def payload(self) -> dict[str, int]:
        return {
            "added": self.added,
            "changed": self.changed,
            "unchanged": self.unchanged,
            "birthdays": self.birthdays,
        }


@dataclass
class ContactsState:
    name: str = "contacts"
    cards: dict[str, Card] = field(default_factory=dict[str, Card])
    """uid → card"""
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.cards.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted or event.kind != CARD:
            return
        card = Card.from_payload(event.payload)
        self.cards[card.uid] = card


def _matches(card: Card, wanted: str) -> bool:
    key = name_key(wanted)
    digits = "".join(c for c in wanted if c.isdigit())
    return bool(
        (key and any(key in name_key(n) for n in (card.name, *card.nicknames)))
        or (len(digits) >= 6 and any(digits[-9:] in p for p in card.phones))
        or ("@" in wanted and wanted.strip().lower() in card.emails)
    )


class Contacts:
    def __init__(self, journal: EventStore, owner_names: list[str]) -> None:
        self._journal = journal
        self._owner = {name_key(n) for n in owner_names}
        self.state = ContactsState()
        self._runner = ProjectionRunner(journal, [self.state])

    async def sync(self) -> None:
        await self._runner.catch_up()

    def find(self, wanted: str) -> list[Card]:
        return sorted(
            (c for c in self.state.cards.values() if _matches(c, wanted)), key=lambda c: c.name
        )

    async def import_vcards(self, text: str, by: str) -> Imported:
        """Journal what is new or changed in `text` (a `.vcf`); `by`: who gave it."""
        await self.sync()
        added = changed = unchanged = birthdays = 0
        provenance = Provenance(source=f"contacts:{by}", actor=f"owner:{by}")
        events: list[NewEvent] = []
        for card in parse(text):
            known = self.state.cards.get(card.uid)
            if known == card:
                unchanged += 1
                continue
            added += known is None
            changed += known is not None
            events.append(
                NewEvent(kind=CARD, payload=card.payload(), provenance=provenance, trust=CARD_TRUST)
            )
            new_birthday = card.birthday and (known is None or known.birthday != card.birthday)
            if new_birthday and name_key(card.name) not in self._owner:
                assert card.birthday is not None
                birthdays += 1
                events.append(
                    NewEvent(
                        kind=BIRTHDAY,
                        payload={
                            "person": card.name,
                            "month": card.birthday.month,
                            "day": card.birthday.day,
                            "year": card.birthday.year,
                        },
                        provenance=provenance,
                        trust=CARD_TRUST,
                    )
                )
        report = Imported(added, changed, unchanged, birthdays)
        events.append(
            NewEvent(
                kind=IMPORTED, payload=report.payload(), provenance=provenance, trust=CARD_TRUST
            )
        )
        await self._journal.append(events)
        await self.sync()
        log.info("contacts.imported", by=by, **report.payload())
        return report


class FindInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    who: str = Field(min_length=2, max_length=120, description="A name, a nickname, a number")


def contacts_tools(contacts: Contacts) -> list[Tool]:
    async def find(args: Any, _ctx: ToolContext) -> ToolResult:
        await contacts.sync()
        if not contacts.state.cards:
            return ToolResult(
                "Son carnet d'adresses n'est pas encore importé : il l'exporte de son téléphone "
                "(un fichier .vcf) et te le donne depuis l'app, Réglages."
            )
        found = contacts.find(args.who)
        if not found:
            return ToolResult(f"Personne ne correspond à « {args.who} » dans son carnet.")
        lines = [f"- {c.line()}" for c in found[:FOUND_AT_MOST]]
        if len(found) > FOUND_AT_MOST:
            lines.append(f"(et {len(found) - FOUND_AT_MOST} autres : précise)")
        return ToolResult("\n".join(lines), trust=CARD_TRUST)

    return [
        Tool(
            name="contacts",
            description="His phone's address book: someone's number, address or birthday, by "
            "name, nickname or number.",
            input_model=FindInput,
            handler=find,
            effect=Effect.NONE,
            reversibility=Reversibility.REVERSIBLE,
            concurrency_safe=True,
            accepts_untrusted_context=True,
        )
    ]


class ContactsModule(Module):
    name = "contacts"
    settings_model = ContactsSettings

    def __init__(self, config: OrionConfig) -> None:
        super().__init__(config)
        self.contacts: Contacts | None = None

    async def assemble(self, plate: Plate) -> list[Tool]:
        self.contacts = Contacts(plate.journal, self.config.memory.owner_names)
        await self.contacts.sync()
        return contacts_tools(self.contacts)

    def doctor(self) -> list[str]:
        return ["his address book, imported from the app or with `orion contacts import`"]
