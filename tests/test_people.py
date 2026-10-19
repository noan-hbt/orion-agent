from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.memory.model import Category
from orion.memory.people import canonical, find, people_index, people_tools
from orion.memory.service import MemoryService, Origin
from orion.memory.store import MemoryStore, name_key
from orion.tools.base import Tool, ToolContext

OWNER = Origin(actor="orion:core", source="terminal:owner", trust=Trust.OWNER)
CTX = ToolContext(thread_id="owner", correlation_id="c")


@pytest.fixture
async def memory(journal: SqliteJournal) -> AsyncIterator[MemoryService]:
    store = MemoryStore(":memory:")
    await store.open()
    service = MemoryService(journal, store)
    await service.remember("Paul est allergique aux noix.", OWNER, about=["Paul"])
    await service.remember("Paul Martin et Léa sont mariés.", OWNER, about=["Paul Martin", "Léa"])
    await service.remember("Léa travaille chez Airbus.", OWNER, about=["Léa"])
    await service.remember("Noan aime le café serré.", OWNER, about=["Noan"],
                           category=Category.PREFERENCE)  # fmt: skip
    await service.add_commitment(
        "rendre le livre", OWNER, debtor="owner", creditor="Paul Martin", due="2026-10-25"
    )
    yield service
    await store.close()


def tool(tools: list[Tool], name: str) -> Tool:
    return next(t for t in tools if t.name == name)


def test_names_compare_without_case_or_accents() -> None:
    assert name_key("  Léa ") == name_key("lea") == "lea"
    assert canonical("Paulo", {"paulo": "Paul", "paul": "Paul Martin"}) == "Paul Martin"
    assert canonical("a", {"a": "b", "b": "a"}) in {"a", "b"}  # a loop does not hang


async def test_aliases_merge_people_and_links_are_counted(memory: MemoryService) -> None:
    facts = await memory.store.facts()
    commitments = await memory.store.commitments(status=None)
    apart = people_index(facts, commitments, {}, {"Noan"})
    assert set(apart) == {"paul", "paul martin", "lea"}  # the owner is not one of their people

    await memory.alias_person("Paul", "Paul Martin", "même personne", OWNER)
    aliases = await memory.store.aliases()
    people = people_index(facts, commitments, aliases, {"Noan"})
    paul = people["paul martin"]
    assert paul.aliases == {"Paul"} and len(paul.facts) == 2 and len(paul.commitments) == 1
    assert paul.related["Léa"] == 1 and people["lea"].related["Paul Martin"] == 1
    assert find(people, "paul", aliases) == [paul]
    assert find(people, "Martin", aliases) == [paul]  # a partial name
    assert find(people, "Jean", aliases) == []


async def test_person_tools(memory: MemoryService) -> None:
    tools = people_tools(memory, ["Noan"])
    person = tool(tools, "person")
    same = tool(tools, "same_person")
    exact = (await person.handler(person.parse('{"name": "Paul"}'), CTX)).content
    assert exact.startswith("Paul\n") and exact.endswith(
        "Noms proches : Paul Martin (si c'est la même personne, dis-le avec same_person)"
    )
    await memory.remember("Paul Durand est plombier.", OWNER, about=["Paul Durand"])
    ambiguous = await person.handler(person.parse('{"name": "Durand Paul"}'), CTX)
    assert ambiguous.content.startswith("Paul Durand\n")  # word order does not matter
    ambiguous = await person.handler(person.parse('{"name": "Martin"}'), CTX)
    assert ambiguous.content.startswith("Paul Martin\n")

    await same.handler(same.parse('{"name": "Paul", "same_as": "Paul Martin"}'), CTX)
    profile = (await person.handler(person.parse('{"name": "paul"}'), CTX)).content.splitlines()
    assert profile[0] == "Paul Martin (aussi : Paul)"
    assert any("allergique aux noix" in line for line in profile)
    assert any("rendre le livre" in line for line in profile)
    assert profile[-1] == "Lié à : Léa (1)"

    people = tool(tools, "people")
    listing = (await people.handler(people.parse("{}"), CTX)).content
    assert listing.splitlines()[0] == "- Paul Martin: 2 fait(s), 1 engagement(s)"
    assert "Noan" not in listing
    assert same.effect.value == "internal" and not same.accepts_untrusted_context


async def test_forgetting_a_source_keeps_aliases_consistent(memory: MemoryService) -> None:
    await memory.alias_person(
        "Paulo", "Paul Martin", "surnom", Origin(actor="x", source="mail:perso", trust=Trust.SYSTEM)
    )
    report = await memory.forget_source("mail:perso", "test", actor="owner:main")
    assert report.verified
    assert "paulo" not in await memory.store.aliases()
