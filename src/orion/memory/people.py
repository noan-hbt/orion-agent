"""People: everyone in the owner's life, as a graph built from memory.

Nobody is stored as a "person record". A person is the set of facts whose `about` names them
and the commitments they owe or are owed, under one name: aliases ("Paul", "Paul Martin")
are merged by `person.aliased` events. Two people are linked when facts concern both. Being
derived from memory, the graph is always as current as memory, and forgetting a fact removes
it from the graph too.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orion.memory.render import format_commitment, format_fact
from orion.memory.service import MemoryService
from orion.memory.store import Commitment, Fact, name_key
from orion.memory.tools import origin_of
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult

_NOT_PEOPLE = {"owner", "orion"}


@dataclass
class Person:
    name: str
    aliases: set[str] = field(default_factory=set[str])
    facts: list[Fact] = field(default_factory=list[Fact])
    commitments: list[Commitment] = field(default_factory=list[Commitment])
    related: Counter[str] = field(default_factory=Counter[str])
    """Other people mentioned in the same facts, by number of shared facts."""


def canonical(name: str, aliases: dict[str, str]) -> str:
    """Follow aliases to the canonical name (loops are cut)."""
    seen: set[str] = set()
    current = name.strip()
    while name_key(current) in aliases and name_key(current) not in seen:
        seen.add(name_key(current))
        current = aliases[name_key(current)]
    return current


def people_index(
    facts: list[Fact],
    commitments: list[Commitment],
    aliases: dict[str, str],
    owner_names: set[str] | None = None,
) -> dict[str, Person]:
    """Name key → person. `owner_names` are left out (the owner is not "someone in their
    life")."""
    excluded = {name_key(n) for n in (owner_names or set())} | _NOT_PEOPLE
    people: dict[str, Person] = {}

    def person(name: str) -> Person | None:
        if not name.strip() or name_key(name) in excluded:
            return None
        main = canonical(name, aliases)
        if name_key(main) in excluded:
            return None
        found = people.setdefault(name_key(main), Person(main))
        if name_key(name) != name_key(main):
            found.aliases.add(name.strip())
        return found

    for fact in facts:
        involved = list({id(p): p for p in map(person, fact.about) if p is not None}.values())
        for target in involved:
            target.facts.append(fact)
            for other in involved:
                if other is not target:
                    target.related[other.name] += 1
    for commitment in commitments:
        for side in {commitment.debtor, commitment.creditor}:
            p = person(side)
            if p is not None:
                p.commitments.append(commitment)
    return people


def find(people: dict[str, Person], name: str, aliases: dict[str, str]) -> list[Person]:
    """The person a name means: exact (or alias) first, then first names and partial names."""
    key = name_key(canonical(name, aliases))
    if key in people:
        return [people[key]]
    return similar(people, name, aliases)


def similar(people: dict[str, Person], name: str, aliases: dict[str, str]) -> list[Person]:
    """People whose name contains every word of `name` ("Paul" → "Paul Martin")."""
    key = name_key(canonical(name, aliases))
    wanted = set(key.split())
    return [
        p
        for k, p in people.items()
        if k != key
        and (wanted <= set(k.split()) or any(wanted <= set(name_key(a).split()) for a in p.aliases))
    ]


def describe(person: Person) -> str:
    head = person.name + (
        f" (aussi : {', '.join(sorted(person.aliases))})" if person.aliases else ""
    )
    lines = [head]
    current = [f for f in person.facts if f.current]
    if current:
        lines += ["Ce que tu sais :", *(format_fact(f) for f in current)]
    open_items = [c for c in person.commitments if c.status.value == "open"]
    if open_items:
        lines += ["Engagements en cours :", *(format_commitment(c) for c in open_items)]
    if person.related:
        linked = ", ".join(f"{name} ({n})" for name, n in person.related.most_common(8))
        lines.append(f"Lié à : {linked}")
    return "\n".join(lines)


class PersonInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="A name or first name, e.g. 'Paul'")


class PeopleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SamePersonInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="The other name, e.g. 'Paulo'")
    same_as: str = Field(description="The name to keep, e.g. 'Paul Martin'")
    reason: str = Field(default="owner said so", max_length=300)


def people_tools(memory: MemoryService, owner_names: list[str]) -> list[Tool]:
    async def index() -> tuple[dict[str, Person], dict[str, str]]:
        aliases = await memory.store.aliases()
        facts = await memory.store.facts()
        commitments = await memory.store.commitments(status=None)
        return people_index(facts, commitments, aliases, set(owner_names)), aliases

    async def person(args: Any, ctx: ToolContext) -> ToolResult:
        people, aliases = await index()
        found = find(people, args.name, aliases)
        if not found:
            return ToolResult(f"Personne de connu sous le nom « {args.name} ».")
        if len(found) > 1:
            names = ", ".join(sorted(p.name for p in found))
            return ToolResult(f"Plusieurs personnes possibles : {names}. Précise laquelle.")
        profile = describe(found[0])
        others = [p.name for p in similar(people, found[0].name, aliases)]
        if others:
            profile += (
                f"\nNoms proches : {', '.join(sorted(others))} (si c'est la même personne, "
                "dis-le avec people, action same)"
            )
        return ToolResult(profile)

    async def people_list(args: Any, ctx: ToolContext) -> ToolResult:
        people, _ = await index()
        if not people:
            return ToolResult("Personne pour l'instant.")
        ranked = sorted(people.values(), key=lambda p: (-len(p.facts) - len(p.commitments), p.name))
        return ToolResult(
            "\n".join(
                f"- {p.name}: {len(p.facts)} fait(s)"
                + (f", {len(p.commitments)} engagement(s)" if p.commitments else "")
                for p in ranked[:60]
            )
        )

    async def same_person(args: Any, ctx: ToolContext) -> ToolResult:
        if name_key(args.name) == name_key(args.same_as):
            return ToolResult("Same name twice.", is_error=True)
        await memory.alias_person(args.name, args.same_as, args.reason, origin_of(ctx))
        return ToolResult(f"« {args.name} » et « {args.same_as} » : même personne, noté.")

    read: dict[str, Any] = {
        "effect": Effect.NONE,
        "reversibility": Reversibility.REVERSIBLE,
        "accepts_untrusted_context": True,
        "concurrency_safe": True,
    }
    return [
        Tool(
            name="person",
            description="Everything known about someone in the owner's life: facts, "
            "commitments, who they are linked to. Use it before talking about someone.",
            input_model=PersonInput,
            handler=person,
            **read,
        ),
        Tool(
            name="people",
            description="The people in the owner's life that memory knows of.",
            input_model=PeopleInput,
            handler=people_list,
            **read,
        ),
        Tool(
            name="same_person",
            description="Record that two names are the same person (a nickname, a full name).",
            input_model=SamePersonInput,
            handler=same_person,
            effect=Effect.INTERNAL,
            reversibility=Reversibility.REVERSIBLE,
        ),
    ]
