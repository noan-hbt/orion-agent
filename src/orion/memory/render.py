"""Text renderings of memory: for the model (compact, with ids) and for the owner (Markdown)."""

from __future__ import annotations

from orion.memory.model import Category
from orion.memory.store import Commitment, Fact

_OWNER = {"owner": "toi", "orion": "Orion"}


def format_fact(fact: Fact) -> str:
    when = f" (depuis {fact.valid_from})" if fact.valid_from else ""
    past = "" if fact.current else f" [plus vrai : {fact.invalid_reason}]"
    return f"- ({fact.id}) {fact.statement}{when}{past}"


def format_commitment(c: Commitment) -> str:
    debtor = _OWNER.get(c.debtor, c.debtor)
    creditor = _OWNER.get(c.creditor, c.creditor)
    due = f", pour le {c.due}" if c.due else ""
    return f"- ({c.id}) {debtor} → {creditor} : {c.what}{due}"


def snapshot_block(facts: list[Fact], commitments: list[Commitment], max_chars: int) -> str | None:
    """Core memory shown once at the start of a conversation window."""
    core = [f for f in facts if f.category in {Category.PROFILE, Category.PREFERENCE}]
    lines: list[str] = []
    for line in [*(format_fact(f) for f in core), *(format_commitment(c) for c in commitments)]:
        if sum(len(x) + 1 for x in lines) + len(line) > max_chars:
            lines.append("- …")
            break
        lines.append(line)
    if not lines:
        return None
    return "<memoire>\n" + "\n".join(lines) + "\n</memoire>"


def recall_block(facts: list[Fact]) -> str | None:
    """Memories relevant to the message they are attached to."""
    if not facts:
        return None
    return "<souvenirs>\n" + "\n".join(format_fact(f) for f in facts) + "\n</souvenirs>"


def profile_markdown(facts: list[Fact], commitments: list[Commitment]) -> str:
    """A human-readable view of everything Orion currently believes, grouped by category."""
    titles = {
        Category.PROFILE: "Toi",
        Category.PREFERENCE: "Tes préférences",
        Category.PERSON: "Les gens",
        Category.PROJECT: "Projets",
        Category.OTHER: "Divers",
    }
    out = ["# Ce qu'Orion sait", ""]
    for category, title in titles.items():
        section = [f for f in facts if f.category == category.value and f.current]
        if section:
            out += [f"## {title}", "", *(format_fact(f) for f in section), ""]
    if commitments:
        out += ["## Engagements en cours", "", *(format_commitment(c) for c in commitments), ""]
    return "\n".join(out)
