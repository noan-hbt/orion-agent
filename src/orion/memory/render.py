"""Text renderings of memory: for the model (compact, with ids) and for the owner (Markdown)."""

from __future__ import annotations

from collections.abc import Sequence

from orion.memory.model import Category, RuleBasis, SettingKey
from orion.memory.store import Commitment, Fact, Rule, SettingChange

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


_SETTING_LABELS = {
    SettingKey.DIGESTS: "heures des points du jour",
    SettingKey.QUIET_START: "début des heures calmes",
    SettingKey.QUIET_END: "fin des heures calmes",
    SettingKey.DAILY_BUDGET: "messages non demandés par jour, au plus",
}


def format_rule(rule: Rule) -> str:
    learned = " [appris]" if rule.basis is RuleBasis.INFERRED else ""
    return f"- ({rule.id}) {rule.rule}{learned}"


def format_setting(change: SettingChange) -> str:
    value = ", ".join(change.value) if isinstance(change.value, list) else change.value
    value = value if value != "" else "aucun"
    return f"- ({change.id}) {_SETTING_LABELS[change.key]} : {value} ({change.reason})"


def snapshot_block(
    facts: list[Fact],
    commitments: list[Commitment],
    max_chars: int,
    rules: Sequence[Rule] = (),
    settings: Sequence[SettingChange] = (),
    portrait: str | None = None,
) -> str | None:
    """Core memory shown once at the start of a conversation window: his portrait (D144), then
    the playbook, which changes how Orion behaves, then what it knows. The playbook is always
    whole: `max_chars` bounds only the facts and commitments (D185). What he pinned comes
    first, whatever its kind (D188)."""
    core = [f for f in facts if f.pinned] + [
        f for f in facts if not f.pinned and f.category in {Category.PROFILE, Category.PREFERENCE}
    ]
    lines = [*(format_rule(r) for r in rules), *(format_setting(c) for c in settings)]
    known: list[str] = []
    for line in [
        *(format_fact(f) for f in core),
        *(format_commitment(c) for c in commitments),
    ]:
        if sum(len(x) + 1 for x in known) + len(line) > max_chars:
            known.append("- …")
            break
        known.append(line)
    lines += known
    if portrait:
        lines.insert(0, portrait)
    if not lines:
        return None
    return "<memoire>\n" + "\n".join(lines) + "\n</memoire>"


def recall_block(facts: list[Fact]) -> str | None:
    """Memories relevant to the message they are attached to."""
    if not facts:
        return None
    return "<souvenirs>\n" + "\n".join(format_fact(f) for f in facts) + "\n</souvenirs>"


def reminder_block(rules: Sequence[Rule], facts: list[Fact]) -> str | None:
    """His standing instructions shown again once the window has drifted far from where they
    were last shown: a long conversation otherwise slides back to the model's own habits."""
    lines = [format_rule(r) for r in rules if r.active]
    lines += [format_fact(f) for f in facts if f.category == Category.PREFERENCE and f.current]
    if not lines:
        return None
    return (
        "<consignes>\nCe qu'il t'a demandé, toujours en vigueur :\n"
        + "\n".join(lines)
        + "\n</consignes>"
    )


def playbook_delta(
    rules: Sequence[Rule], settings: Sequence[SettingChange], shown: str
) -> str | None:
    """What changed in the playbook since the window started: new rules and settings, and
    rules retired since they were shown. Each change appears once, then stays in the window."""
    lines = [format_rule(r) for r in rules if r.active and r.id not in shown]
    lines += [format_setting(c) for c in settings if c.id not in shown]
    lines += [
        f"- ({r.id}) retirée : {r.retired_reason}"
        for r in rules
        if not r.active and f"({r.id})" in shown and f"({r.id}) retirée" not in shown
    ]
    if not lines:
        return None
    return "<manuel>\n" + "\n".join(lines) + "\n</manuel>"


def profile_markdown(
    facts: list[Fact],
    commitments: list[Commitment],
    rules: Sequence[Rule] = (),
    settings: Sequence[SettingChange] = (),
) -> str:
    """A human-readable view of everything Orion currently believes, grouped by category."""
    titles = {
        Category.PROFILE: "Toi",
        Category.PREFERENCE: "Tes préférences",
        Category.PERSON: "Les gens",
        Category.PROJECT: "Projets",
        Category.OTHER: "Divers",
    }
    out = ["# Ce qu'Orion sait", ""]
    if rules or settings:
        out += [
            "## Comment il travaille avec toi",
            "",
            *(format_rule(r) for r in rules),
            *(format_setting(c) for c in settings),
            "",
        ]
    for category, title in titles.items():
        section = [f for f in facts if f.category == category.value and f.current]
        if section:
            out += [f"## {title}", "", *(format_fact(f) for f in section), ""]
    if commitments:
        out += ["## Engagements en cours", "", *(format_commitment(c) for c in commitments), ""]
    return "\n".join(out)
