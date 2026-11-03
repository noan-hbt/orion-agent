"""What can set a hook off (D225): the journal's events, an address, an hour.

Everything Orion sees or does is an event in the journal (`orion.core.journal`). A trigger is a
named family of those events, said in the owner's words (« un mail arrive », « tu arrives
quelque part »), with the gist of one event, short, for the owner and for Orion. Three more
are not read from the journal: `webhook` (a service posts to the hook's own address), `schedule`
(a cron line, in the owner's time zone) and `journal` (any kind of event, by pattern: for what
the list does not name).

The gist of an event keeps the event's trust: a mail's subject is a stranger's words, and what
a hook does with it is never more trusted than that (`Hook.trust_for`).
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast
from zoneinfo import ZoneInfo

from orion.console.activity import HIDDEN, describe
from orion.core.events import Event, Trust
from orion.memory.store import name_key

GIST = 400
"""Characters of an event's gist given to Orion with the hook's instruction."""

WEBHOOK = "webhook"
SCHEDULE = "schedule"
JOURNAL = "journal"

NEVER_MATCHED = ("hook.", "initiative.", "model.", "spend.", "thread.", "projection.")
"""Families no hook reacts to: a hook's own work, the bookkeeping of every model call. A hook
that reacted to what hooks do would set itself off forever."""

FAMILIES = {
    "vie": "Ce que je vois de ta vie",
    "orion": "Ce que je fais",
    "dehors": "Du dehors",
    "horaire": "À heure fixe",
}


def _always(_event: Event) -> bool:
    return True


def _text(value: object, limit: int = GIST) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _p(event: Event) -> dict[str, Any]:
    return event.payload


def _sub(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _mail(event: Event, _zone: ZoneInfo) -> str:
    p = _p(event)
    who = _text(p.get("from_name") or "", 60)
    address = _text(p.get("from_addr") or "", 80)
    sender = f"{who} <{address}>" if who and address else who or address or "?"
    card = _sub(p, "card")
    summary = _text(card.get("summary") or "", 200)
    importance = str(card.get("importance") or "")
    return " · ".join(
        part
        for part in (
            f"de {sender}",
            f"« {_text(p.get('subject'), 120)} »",
            summary,
            "important" if importance == "high" else "",
            "suspect" if card.get("suspicious") else "",
        )
        if part
    )


def _important_mail(event: Event) -> bool:
    return str(_sub(_p(event), "card").get("importance") or "") == "high"


def _agenda(event: Event, zone: ZoneInfo) -> str:
    p = _p(event)
    if event.kind == "calendar.event.gone":
        return f"disparu de l'agenda : {_text(p.get('key'), 120)}"
    occurrence = _sub(p, "occurrence")
    title = _text(occurrence.get("title") or "un rendez-vous", 120)
    start = str(occurrence.get("start") or "")
    when = start[:16].replace("T", " ") if start else ""
    standing = str(occurrence.get("standing") or "")
    verb = "invitation" if standing == "pending" else "rendez-vous"
    return f"{verb} : {title}" + (f", {when}" if when else "")


def _place(event: Event, _zone: ZoneInfo) -> str:
    p = _p(event)
    verb = "arrivé à" if p.get("event") == "arrive" else "parti de"
    return f"{verb} « {_text(p.get('place'), 80)} »"


def _arrived(event: Event) -> bool:
    return _p(event).get("event") == "arrive"


def _left(event: Event) -> bool:
    return _p(event).get("event") == "leave"


def _sensed(event: Event, _zone: ZoneInfo) -> str:
    p = _p(event)
    return f"{_text(p.get('kind'), 20)} : {_text(p.get('value'), 200)}"


def _feed(event: Event, _zone: ZoneInfo) -> str:
    p = _p(event)
    return f"« {_text(p.get('title'), 160)} » ({_text(p.get('link'), 160)})"


def _contact(event: Event, _zone: ZoneInfo) -> str:
    p = _p(event)
    return f"{_text(p.get('contact'), 60)} : {_text(p.get('text'), 300)}"


def _peer(event: Event, _zone: ZoneInfo) -> str:
    p = _p(event)
    who = p.get("peer") or p.get("label") or p.get("from") or "un autre Orion"
    return f"{_text(who, 60)} : {_text(p.get('text'), 300)}"


def _task(event: Event, _zone: ZoneInfo) -> str:
    p = _p(event)
    return _text(p.get("summary") or p.get("title") or p.get("task_id") or p.get("job_id"))


def _mission(event: Event, _zone: ZoneInfo) -> str:
    p = _p(event)
    return f"mission {p.get('mission_id', '?')} : {_text(p.get('said') or 'un passage', 300)}"


def _situation(event: Event, _zone: ZoneInfo) -> str:
    p = _p(event)
    verb = "ouverte" if event.kind == "situation.opened" else "close"
    return f"situation {verb} : {_text(p.get('title') or p.get('situation_id'), 160)}"


def _turn_failed(event: Event) -> bool:
    return str(_p(event).get("reason", "")) not in ("completed", "halted")


def _turn(event: Event, _zone: ZoneInfo) -> str:
    p = _p(event)
    detail = _text(p.get("detail"), 200)
    return f"un tour s'est arrêté ({p.get('reason', '?')})" + (f" : {detail}" if detail else "")


HOOK_THREAD = "hooks"
"""Where a hook's own tool calls run: a hook never reacts to them (it would feed itself)."""


def _tool_error(event: Event) -> bool:
    return bool(_p(event).get("is_error")) and _p(event).get("thread_id") != HOOK_THREAD


def _tool(event: Event, _zone: ZoneInfo) -> str:
    p = _p(event)
    return f"{p.get('tool', '?')} en erreur ({_text(p.get('decision'), 160)})"


def _owner_said(event: Event) -> bool:
    return event.provenance.actor == "owner:main"


def _said(event: Event, _zone: ZoneInfo) -> str:
    p = _p(event)
    return f"{p.get('channel', '?')} : {_text(p.get('text'), 300)}"


def generic(event: Event, zone: ZoneInfo) -> str:
    """Any event, as the activity view reads it back (`orion.console.activity`)."""
    entry = describe(event, zone)
    return _text(f"{entry.what} — {entry.detail}" if entry.detail else entry.what)


Gist = Callable[[Event, ZoneInfo], str]


@dataclass(frozen=True)
class Trigger:
    name: str
    label: str
    """What sets it off, in the owner's words: « un mail arrive »."""
    family: str
    kinds: frozenset[str] = frozenset()
    when: Callable[[Event], bool] = _always
    gist: Gist = generic
    needs: str = ""
    """The module it reads from, when one must be on (`mail`): said in the list."""
    match_required: bool = False
    """Too frequent to fire on every one: the hook must say which (`match`)."""

    def fits(self, event: Event) -> bool:
        return event.kind in self.kinds and not event.redacted and self.when(event)


def _t(
    name: str,
    label: str,
    family: str,
    kinds: tuple[str, ...] = (),
    when: Callable[[Event], bool] = _always,
    gist: Gist = generic,
    needs: str = "",
    match_required: bool = False,
) -> Trigger:
    return Trigger(name, label, family, frozenset(kinds), when, gist, needs, match_required)


TRIGGERS: tuple[Trigger, ...] = (
    # -- his life, as Orion sees it ------------------------------------------------------------
    _t("mail", "un mail arrive", "vie", ("mail.card",), gist=_mail, needs="mail"),
    _t(
        "mail_important",
        "un mail que je juge important arrive",
        "vie",
        ("mail.card",),
        _important_mail,
        _mail,
        "mail",
    ),
    _t(
        "agenda",
        "ton agenda bouge (un rendez-vous ajouté, déplacé, disparu, une invitation)",
        "vie",
        ("calendar.event.seen", "calendar.event.gone"),
        gist=_agenda,
        needs="calendar",
    ),
    _t("arrive", "tu arrives quelque part", "vie", ("place.event",), _arrived, _place, "places"),
    _t("leave", "tu pars de quelque part", "vie", ("place.event",), _left, _place, "places"),
    _t(
        "phone",
        "ton téléphone me dit quelque chose (sommeil, concentration, batterie, sport…)",
        "vie",
        ("phone.sensed",),
        gist=_sensed,
        needs="places",
    ),
    _t("feed", "un article paraît dans tes flux", "vie", ("feed.item",), gist=_feed),
    _t(
        "contact",
        "un de tes proches m'écrit",
        "vie",
        ("contact.message",),
        gist=_contact,
        needs="secretary",
    ),
    _t(
        "peer",
        "un autre Orion m'écrit",
        "vie",
        ("peer.message.received", "federation.chat.turn"),
        gist=_peer,
        needs="federation",
    ),
    _t("call", "un appel se termine", "vie", ("call.ended",), needs="voice"),
    _t(
        "message",
        "tu m'écris un mot précis (à dire dans « contient »)",
        "vie",
        ("message.received",),
        _owner_said,
        _said,
        match_required=True,
    ),
    _t("reaction", "tu réagis à un de mes messages", "vie", ("feedback.reaction",)),
    _t("file", "tu m'envoies une photo ou un fichier", "vie", ("attachment.kept", "photo.seen")),
    _t(
        "situation",
        "une situation de ta vie s'ouvre ou se clôt",
        "vie",
        ("situation.opened", "situation.closed"),
        gist=_situation,
    ),
    _t(
        "commitment",
        "un engagement est pris (par toi ou envers toi)",
        "vie",
        ("commitment.created",),
    ),
    _t("paper", "un papier rejoint ton coffre", "vie", ("paper.filed",), needs="papers"),
    _t("drive", "ton Drive change", "vie", ("drive.changed",), needs="drive"),
    _t("focus_end", "une concentration se termine", "vie", ("focus.ended",)),
    # -- what Orion does ------------------------------------------------------------------------
    _t(
        "task_done",
        "une tâche de fond se termine",
        "orion",
        ("task.finished", "job.finished"),
        gist=_task,
    ),
    _t("mission", "une mission avance", "orion", ("mission.ran",), gist=_mission),
    _t("approval", "je te demande ton accord", "orion", ("approval.requested",)),
    _t(
        "booking",
        "une réservation aboutit ou échoue",
        "orion",
        ("booking.confirmed", "booking.failed"),
        needs="booking",
    ),
    _t("curiosity", "ma curiosité trouve quelque chose", "orion", ("curiosity.found",)),
    _t("watch", "ma veille se termine", "orion", ("watch.done",)),
    _t("night", "ma nuit se termine", "orion", ("thinking.night.done", "memory.night.done")),
    _t("reminder", "un rappel se déclenche", "orion", ("timer.fired",)),
    _t(
        "turn_failed",
        "je n'arrive pas au bout d'une réponse",
        "orion",
        ("turn.finished",),
        _turn_failed,
        _turn,
    ),
    _t("tool_error", "un de mes outils échoue", "orion", ("tool.executed",), _tool_error, _tool),
    _t("send_failed", "un message ne part pas", "orion", ("outbox.failed",)),
    _t("config", "mes réglages changent", "orion", ("config.changed", "settings.changed")),
    _t(
        "autonomy", "tu me laisses faire seul quelque chose de plus", "orion", ("autonomy.granted",)
    ),
    _t(
        "device",
        "un téléphone ou une machine est relié",
        "orion",
        ("mobile.paired", "mobile.signed_in", "hands.paired"),
    ),
    _t(
        "memory",
        "je retiens quelque chose de toi",
        "orion",
        ("memory.fact.asserted",),
        match_required=True,
    ),
    # -- from outside, and at a set time ------------------------------------------------------
    _t(WEBHOOK, "un service appelle l'adresse du hook (GitHub, IFTTT, Home Assistant…)", "dehors"),
    _t(SCHEDULE, "à heure fixe (une ligne cron : « 0 9 * * 1 » = lundi 9 h)", "horaire"),
    _t(JOURNAL, "n'importe quel événement de mon journal, par motif (« mail.* »)", "dehors"),
)

BY_NAME = {t.name: t for t in TRIGGERS}


def trigger(name: str) -> Trigger | None:
    return BY_NAME.get(name)


def reacts(kind: str) -> bool:
    """Whether a hook may react to this kind of event at all."""
    return not kind.startswith(NEVER_MATCHED)


def pattern_fits(pattern: str, kind: str) -> bool:
    """`journal` hooks: « mail.* », « calendar.event.seen », « *.failed »."""
    return reacts(kind) and any(
        fnmatch.fnmatchcase(kind, p.strip()) for p in pattern.split(",") if p.strip()
    )


def haystack(event: Event, gist: str) -> str:
    """What `match` looks in: the gist and every field of the event, secrets left out."""
    parts = [gist, event.kind]
    for key, value in event.payload.items():
        if any(word in key.lower() for word in HIDDEN):
            continue
        parts.append(str(value))
    return name_key(" ".join(parts))


def matches(words: str, text: str) -> bool:
    """Every word of `words` is in `text` (case, accents and spacing do not matter); words
    separated by « | » are alternatives (« facture | relance »)."""
    if not words.strip():
        return True
    return any(
        all(word in text for word in name_key(option).split())
        for option in words.split("|")
        if option.strip()
    )


def event_trust(event: Event) -> Trust:
    """What a hook's work on this event may rely on: the event's own trust, never above what
    Orion itself vouches for (an owner's message is his, but a hook is not him speaking)."""
    return min(event.trust, Trust.SYSTEM)


def catalog_lines() -> list[str]:
    """The triggers, one line each, grouped: what `hooks` (triggers) shows Orion."""
    lines: list[str] = []
    for family, title in FAMILIES.items():
        lines.append(f"{title} :")
        for t in TRIGGERS:
            if t.family != family:
                continue
            extra = f" (module {t.needs})" if t.needs else ""
            extra += " — dire lesquels avec match" if t.match_required else ""
            lines.append(f"- {t.name} : {t.label}{extra}")
    return lines
