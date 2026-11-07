"""Everything Orion did, read back from the journal for the owner (console and app).

Every event of the journal is shown, not a hand-picked list: a kind this file does not know
still appears, named after its family and its verb, with the gist of its payload. Each entry
falls in one category the owner can filter on; the bookkeeping that only the code cares about
(model calls, thread rebases, syncs) goes in `technical`, out of "everything" but one tap away.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from orion.console.dashboard import moment
from orion.core.events import Event
from orion.core.journal import EventStore
from orion.guardian.autonomy import label

CATEGORIES = {
    "conversation": "Conversations",
    "action": "Actions",
    "initiative": "Initiative",
    "memory": "Mémoire",
    "system": "Système",
    "technical": "Technique",
}
"""Shown in this order; "everything" leaves `technical` out."""

LABELS = {
    "message.received": "tu as écrit",
    "contact.message": "un contact a écrit",
    "peer.message.received": "un autre Orion a écrit",
    "peer.message.handled": "a répondu à un autre Orion",
    "federation.chat": "a discuté avec un autre Orion",
    "federation.chat.sent": "a écrit à un autre Orion",
    "federation.chat.turn": "un autre Orion a écrit",
    "secretary.replied": "un contact a répondu au secrétariat",
    "mail.card": "mail reçu",
    "voice.live.started": "appel vocal commencé",
    "voice.live.ended": "appel vocal terminé",
    "call.requested": "appel demandé",
    "call.ended": "appel terminé",
    "tool.executed": "a utilisé un outil",
    "approval.requested": "a demandé ton accord",
    "approval.decided": "accord donné ou refusé",
    "mail.sent": "a envoyé un mail",
    "mail.drafted": "a préparé un brouillon",
    "mail.marked": "a classé un mail",
    "mail.tidied": "a rangé la boîte mail",
    "calendar.event.added": "a ajouté à l'agenda",
    "calendar.event.changed": "a modifié l'agenda",
    "web.searched": "a cherché sur le web",
    "web.read": "a lu une page web",
    "web.downloaded": "a téléchargé un fichier",
    "browser.page": "a navigué",
    "file.made": "a créé un fichier",
    "drive.uploaded": "a déposé sur le Drive",
    "outbox.queued": "message prêt à partir",
    "outbox.delivered": "message envoyé",
    "outbox.failed": "envoi échoué",
    "job.started": "a lancé une tâche",
    "job.finished": "tâche terminée",
    "job.crashed": "tâche plantée",
    "job.interrupted": "tâche interrompue",
    "initiative.triggered": "a eu une idée",
    "initiative.delivered": "a écrit le premier",
    "initiative.suppressed": "s'est tu",
    "initiative.weight": "a ajusté l'importance d'une source",
    "thinking.thought": "a réfléchi",
    "memory.fact.asserted": "a retenu",
    "memory.fact.invalidated": "a oublié",
    "memory.fact.pinned": "a épinglé un souvenir",
    "commitment.created": "nouvel engagement",
    "commitment.closed": "engagement clos",
    "playbook.rule.learned": "nouvelle règle",
    "playbook.rule.retired": "règle retirée",
    "autonomy.granted": "autonomie accordée",
    "autonomy.revoked": "autonomie retirée",
    "autonomy.offered": "a proposé de faire seul",
    "provenance.contained": "source mise en quarantaine",
    "journal.redacted": "effacé du journal",
    "config.changed": "réglages modifiés",
    "config.restarting": "redémarrage",
    "relation.birthday": "anniversaire",
    "feedback.reaction": "tu as réagi",
    "timer.scheduled": "rappel programmé",
    "timer.fired": "rappel déclenché",
    "timer.cancelled": "rappel annulé",
    "mission.started": "mission lancée",
    "mission.ran": "a avancé une mission",
    "task.started": "tâche commencée",
    "task.finished": "tâche finie",
    "curiosity.found": "a trouvé quelque chose pour toi",
    "chronicle.written": "chronique de la semaine écrite",
    "thinking.night.done": "réflexion de la nuit finie",
    "memory.night.done": "tri de la mémoire fini",
    "model.usage": "appel au modèle",
    "spend.recorded": "dépense",
    "turn.finished": "a fini de répondre",
    "conversation.message": "a répondu",
}

FAMILIES = {
    "advice": ("conseil", "initiative"),
    "approval": ("accord", "action"),
    "attachment": ("pièce jointe", "action"),
    "autonomy": ("autonomie", "system"),
    "awaiting": ("attente", "initiative"),
    "booking": ("réservation", "action"),
    "browser": ("navigateur", "action"),
    "calendar": ("agenda", "action"),
    "call": ("appel", "conversation"),
    "chronicle": ("chronique", "initiative"),
    "commitment": ("engagement", "memory"),
    "config": ("réglages", "system"),
    "console": ("console", "system"),
    "contact": ("contact", "memory"),
    "contacts": ("contacts", "memory"),
    "conversation": ("conversation", "conversation"),
    "curiosity": ("curiosité", "initiative"),
    "departure": ("départ", "action"),
    "doubt": ("doute", "memory"),
    "drive": ("Drive", "action"),
    "expert": ("expert", "action"),
    "federation": ("fédération", "conversation"),
    "feed": ("flux", "system"),
    "feedback": ("ta réaction", "memory"),
    "file": ("fichier", "action"),
    "guardian": ("gardien", "system"),
    "focus": ("concentration", "system"),
    "followup": ("relance", "action"),
    "hands": ("mains", "system"),
    "hook": ("hook", "action"),
    "initiative": ("initiative", "initiative"),
    "intention": ("intention", "initiative"),
    "job": ("tâche", "action"),
    "journal": ("journal", "system"),
    "library": ("bibliothèque", "memory"),
    "mail": ("mail", "action"),
    "meeting": ("rendez-vous", "action"),
    "memory": ("mémoire", "memory"),
    "message": ("message", "conversation"),
    "mission": ("mission", "action"),
    "mistake": ("erreur reconnue", "memory"),
    "notification": ("notifications", "system"),
    "mobile": ("app", "system"),
    "model": ("modèle", "technical"),
    "outbox": ("envoi", "action"),
    "paper": ("papier", "memory"),
    "peer": ("autre Orion", "conversation"),
    "person": ("personne", "memory"),
    "photo": ("photo", "memory"),
    "photos": ("photos", "memory"),
    "place": ("lieu", "action"),
    "playbook": ("manuel", "memory"),
    "projection": ("projection", "technical"),
    "provenance": ("source", "system"),
    "relation": ("proche", "memory"),
    "secretary": ("secrétariat", "conversation"),
    "settings": ("réglages", "system"),
    "situation": ("situation", "initiative"),
    "spend": ("dépense", "technical"),
    "task": ("tâche", "action"),
    "thinking": ("réflexion", "initiative"),
    "thread": ("fil", "technical"),
    "timer": ("minuteur", "action"),
    "tool": ("outil", "action"),
    "turn": ("tour", "technical"),
    "voice": ("voix", "conversation"),
    "watch": ("veille", "initiative"),
    "web": ("web", "action"),
}
"""First segment of a kind → (what it is about, its category)."""

VERBS = {
    "added": "ajouté",
    "agreed": "accepté",
    "answered": "répondu",
    "asserted": "noté",
    "cancelled": "annulé",
    "changed": "modifié",
    "closed": "clos",
    "confirmed": "confirmé",
    "consulted": "consulté",
    "contained": "en quarantaine",
    "created": "créé",
    "crashed": "planté",
    "declined": "refusé",
    "delivered": "remis",
    "done": "fini",
    "dropped": "abandonné",
    "ended": "fini",
    "failed": "échec",
    "filed": "classé",
    "finished": "fini",
    "fired": "déclenché",
    "found": "trouvé",
    "given": "donné",
    "granted": "accordé",
    "imported": "importé",
    "invalidated": "oublié",
    "kept": "gardé",
    "learned": "appris",
    "lent": "prêté",
    "made": "créé",
    "noted": "noté",
    "opened": "ouvert",
    "paired": "appairé",
    "passed": "passé",
    "planned": "prévu",
    "proposed": "proposé",
    "ran": "lancé",
    "received": "reçu",
    "removed": "retiré",
    "reopened": "rouvert",
    "replied": "répondu",
    "retired": "retiré",
    "revoked": "retiré",
    "scheduled": "programmé",
    "seen": "vu",
    "sent": "envoyé",
    "set": "fixé",
    "started": "commencé",
    "synced": "synchronisé",
    "updated": "mis à jour",
    "uploaded": "déposé",
    "used": "utilisé",
    "written": "écrit",
}

TECHNICAL = frozenset(
    {
        "model.usage",
        "spend.recorded",
        "turn.finished",
        "calendar.event.seen",
        "calendar.event.gone",
        "calendar.synced",
        "mail.synced",
        "feed.item",
        "feed.synced",
        "drive.changed",
        "memory.extracted",
        "memory.consolidated",
        "thinking.night.phase",
    }
)
"""Bookkeeping: true, journaled, but not something the owner reads by default."""

GIST_KEYS = (
    "text",
    "statement",
    "title",
    "subject",
    "summary",
    "query",
    "what",
    "why",
    "description",
    "note",
    "content",
    "name",
    "url",
    "tool",
    "action",
    "source",
    "reason",
)
"""Where the gist of an unknown payload usually sits, most telling first."""

HIDDEN = ("password", "secret", "token", "cookie", "otp")
"""Payload keys never shown back, whatever the event (fail closed)."""

GIST = 280
FIELD = 4000
SCAN_CHUNK = 1000
SCAN_MAX = 30_000
"""How far back one page looks for matches before handing a cursor to go on."""


@dataclass
class Entry:
    seq: int
    kind: str
    what: str
    detail: str
    category: str
    at: datetime
    silence: bool = False
    failed: bool = False
    fields: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    def as_json(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "what": self.what,
            "detail": self.detail,
            "category": self.category,
            "at": self.at.isoformat(),
            "silence": self.silence,
            "failed": self.failed,
            "fields": [list(f) for f in self.fields],
        }


@dataclass
class Page:
    entries: list[Entry]
    before: int | None
    """Pass it back as `before` to read older entries; None once the journal's start is read."""


def _short(value: object, limit: int = GIST) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _hidden(key: str) -> bool:
    return any(word in key.lower() for word in HIDDEN)


def _as_dict(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _content(message: object) -> str:
    """The text of a journaled chat message (a string, or a list of parts)."""
    content: object = _as_dict(message).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = (_as_dict(part).get("text") for part in cast(list[object], content))
        return " ".join(t for t in parts if isinstance(t, str))
    return ""


def _gist(payload: dict[str, Any]) -> str:
    for key in GIST_KEYS:
        value = payload.get(key)
        if value not in (None, "", [], {}) and not _hidden(key):
            return _short(value)
    return ""


def _arguments(raw: object) -> str:
    """A tool call's arguments as `key: value` pairs, short."""
    try:
        parsed: object = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return _short(raw, 160)
    pairs = [f"{k} : {_short(v, 80)}" for k, v in _as_dict(parsed).items() if not _hidden(k)]
    if not isinstance(parsed, dict):
        return _short(parsed, 160)
    return _short(", ".join(pairs), 200)


def _detail(event: Event, asked: dict[str, str]) -> str:
    p = event.payload
    match event.kind:
        case "message.received":
            channel = p.get("channel")
            return (
                f"{channel} · {_short(p.get('text', ''))}" if channel else _short(p.get("text", ""))
            )
        case "conversation.message":
            return _short(_content(p.get("message")))
        case "tool.executed":
            args = _arguments(p.get("arguments", ""))
            return f"{p.get('tool', '?')}" + (f" — {args}" if args else "")
        case "approval.requested":
            return str(p.get("tool", ""))
        case "approval.decided":
            tool = asked.get(str(p.get("approval_id")))
            answer = "oui" if p.get("approved") else "non"
            how = f" ({p['how']})" if p.get("how") else ""
            return f"{tool} : {answer}{how}" if tool else f"{answer}{how}"
        case "initiative.delivered":
            return f"{p.get('why', '')} — {p.get('arbiter', '')}".strip(" —")
        case "initiative.suppressed":
            return f"{p.get('fallback', p.get('key', ''))} ({p.get('reason', '')})"
        case "mail.sent":
            return f"à {', '.join(map(str, p.get('to', [])))} : {p.get('subject', '')}"
        case "calendar.event.added":
            return str(p.get("occurrence", {}).get("title", ""))
        case "autonomy.granted" | "autonomy.revoked" | "autonomy.offered":
            return label(str(p.get("action", "")))
        case "job.started" | "job.finished":
            return str(p.get("title") or p.get("job_id", ""))
        case "model.usage":
            tokens = p.get("prompt_tokens", 0), p.get("completion_tokens", 0)
            return f"{p.get('model', '')} · {tokens[0]} → {tokens[1]} jetons"
        case "turn.finished":
            return str(p.get("reason", ""))
        case _:
            return _gist(p)


def _what(kind: str) -> str:
    if kind in LABELS:
        return LABELS[kind]
    head, _, rest = kind.partition(".")
    family = FAMILIES.get(head, (head, ""))[0]
    last = rest.rsplit(".", 1)[-1] if rest else ""
    verb = VERBS.get(last, last.replace("_", " "))
    return f"{family} · {verb}" if verb else family


def category(event: Event) -> str:
    kind = event.kind
    if kind in TECHNICAL:
        return "technical"
    if kind == "conversation.message":
        message = _as_dict(event.payload.get("message"))
        said = message.get("role") == "assistant" and _content(message)
        return "conversation" if said else "technical"
    return FAMILIES.get(kind.partition(".")[0], ("", "system"))[1] or "system"


def _fields(payload: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        (key, "•••" if _hidden(key) else _short(value, FIELD))
        for key, value in payload.items()
        if value not in (None, "", [], {})
    ]


def describe(event: Event, zone: ZoneInfo, asked: dict[str, str] | None = None) -> Entry:
    kind = event.kind
    p = event.payload
    failed = bool(p.get("is_error")) or kind.endswith(("failed", "crashed"))
    return Entry(
        seq=event.seq,
        kind=kind,
        what=_what(kind),
        detail=_detail(event, asked or {}),
        category=category(event),
        at=moment(event, zone),
        silence=kind == "initiative.suppressed",
        failed=failed,
        fields=_fields(p),
    )


def _matches(entry: Entry, wanted: str, words: list[str]) -> bool:
    if wanted == "all" and entry.category == "technical":
        return False
    if wanted == "silences":
        if not entry.silence:
            return False
    elif wanted not in ("all", entry.category):
        return False
    if not words:
        return True
    haystack = " ".join(
        [entry.what, entry.detail, entry.kind, *(v for _, v in entry.fields)]
    ).lower()
    return all(w in haystack for w in words)


async def read_activity(
    journal: EventStore,
    zone: ZoneInfo,
    *,
    before: int | None = None,
    limit: int = 100,
    wanted: str = "all",
    query: str = "",
) -> Page:
    """Newest first, from `before` (exclusive) back, at most `limit` entries of category
    `wanted` (or "all", or "silences") whose text holds every word of `query`."""
    words = query.lower().split()
    top = await journal.last_seq() if before is None else before - 1
    entries: list[Entry] = []
    asked: dict[str, str] = {}
    scanned = 0
    while top > 0 and scanned < SCAN_MAX:
        low = max(0, top - SCAN_CHUNK)
        events = await journal.read(low, top - low)
        for e in events:
            if e.kind == "approval.requested" and not e.redacted:
                asked[str(e.payload.get("approval_id"))] = str(e.payload.get("tool"))
        for e in reversed(events):
            if e.redacted:
                continue
            entry = describe(e, zone, asked)
            if _matches(entry, wanted, words):
                entries.append(entry)
                if len(entries) == limit:
                    return Page(entries, e.seq if e.seq > 1 else None)
        scanned += top - low
        top = low
    return Page(entries, top + 1 if top > 0 else None)
