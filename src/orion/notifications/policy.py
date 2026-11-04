"""What rings, how and where (D225): his choices, kind by kind.

Every message Orion sends of his own is of one kind (`KINDS`): his answers, the reminders he
asked for, the points of the day, what Orion says of his own accord, what he entrusted to
Orion, his contacts' messages, the spending, his hooks, the approvals. For each, the owner
chooses:

- the level: `off` (not sent: only for what Orion says unasked), `muette` (in the
  conversation, without ringing), `normale`, `urgente` (rings even in quiet hours);
- the channel: `auto` (where he last wrote, as before), one channel (`mobile`, `telegram`,
  `imessage`), or `partout`.

Four kinds are new (`direct`): they were never sent before D225, are off until he turns them
on, and go without the model, in a line (a mission's pass, Orion's night over, Orion stuck,
something that touches his security). A background task's result is not one of them: Orion
words it himself, as `compte_rendu` (D236).

    notification.set   (trust of whoever set it: the app is OWNER)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner

SET = "notification.set"
OFF = "off"
MUTE = "muette"
NORMAL = "normale"
URGENT = "urgente"
LEVELS = (OFF, MUTE, NORMAL, URGENT)
AUTO = "auto"
EVERYWHERE = "partout"
LEVEL_WORDS = {
    OFF: "jamais",
    MUTE: "sans sonner",
    NORMAL: "normalement",
    URGENT: "même la nuit",
}


@dataclass(frozen=True)
class Kind:
    name: str
    label: str
    help: str
    default: str = NORMAL
    levels: tuple[str, ...] = (MUTE, NORMAL, URGENT)
    channels: bool = True
    """Whether he chooses where it goes (an answer goes back where he wrote)."""
    direct: bool = False
    """New with D225: written without the model, off until he turns it on."""


KINDS: tuple[Kind, ...] = (
    Kind("reponse", "Mes réponses", "à tes messages", levels=(MUTE, NORMAL), channels=False),
    Kind(
        "rappel", "Tes rappels", "ce que tu m'as demandé de te rappeler, à une heure ou à un lieu"
    ),
    Kind(
        "compte_rendu", "Ce que tu m'as confié", "une tâche, un appel, une réservation qui aboutit"
    ),
    Kind("point", "Les points du jour", "ce qui compte, en un message", levels=LEVELS),
    Kind(
        "initiative",
        "Ce que je te dis de moi-même",
        "ma veille, un mail, ton agenda, une idée",
        levels=LEVELS,
    ),
    Kind(
        "proches",
        "Tes proches et les autres Orions",
        "ce qu'ils m'écrivent pour toi",
        levels=LEVELS,
    ),
    Kind("depense", "Ma dépense", "quand j'approche ou atteins un plafond", levels=LEVELS),
    Kind("hook", "Tes hooks", "ce que disent tes hooks (chacun peut sonner à sa façon)"),
    Kind("accord", "Mes demandes d'accord", "quand je te demande la permission", levels=(NORMAL,)),
    Kind("mission", "Une mission avance", "à chaque passage", OFF, LEVELS, direct=True),
    Kind(
        "nuit",
        "Ma nuit est finie",
        "le matin, quand j'ai fini de réfléchir",
        OFF,
        LEVELS,
        direct=True,
    ),
    Kind(
        "erreur",
        "Quand je bute",
        "une réponse que je n'ai pas pu finir, un message qui n'est pas parti",
        OFF,
        LEVELS,
        direct=True,
    ),
    Kind(
        "securite",
        "Sécurité",
        "un téléphone ou une machine relié, une autonomie accordée, mes réglages changés",
        OFF,
        LEVELS,
        direct=True,
    ),
)
BY_NAME = {k.name: k for k in KINDS}


@dataclass(frozen=True)
class Route:
    level: str = NORMAL
    channel: str = AUTO


class NotificationError(ValueError):
    """A choice that cannot be made (unknown kind, a level the kind does not take)."""


@dataclass
class NotificationState:
    name: str = "notifications"
    chosen: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])
    """kind → {"level": …, "channel": …}, as he last set them."""
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.chosen.clear()
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted or event.kind != SET:
            return
        p: dict[str, Any] = event.payload
        kind = str(p.get("kind", ""))
        if kind not in BY_NAME:
            return
        current = self.chosen.setdefault(kind, {})
        for key in ("level", "channel"):
            if p.get(key):
                current[key] = str(p[key])


class Notifications:
    def __init__(self, journal: EventStore) -> None:
        self._journal = journal
        self.state = NotificationState()
        self._runner = ProjectionRunner(journal, [self.state])

    async def sync(self) -> None:
        await self._runner.catch_up()

    def route(self, kind: str) -> Route:
        """His choice for this kind, or its default. An unknown kind goes as before."""
        found = BY_NAME.get(kind)
        if found is None:
            return Route()
        chosen = self.state.chosen.get(kind, {})
        level = chosen.get("level", found.default)
        if level not in found.levels:
            level = found.default
        channel = chosen.get("channel", AUTO) if found.channels else AUTO
        return Route(level, channel)

    async def choose(
        self,
        kind: str,
        level: str | None,
        channel: str | None,
        by: Provenance,
        trust: Trust,
        channels: list[str],
    ) -> Route:
        """Set how one kind rings and where it goes. `channels`: those that exist."""
        await self.sync()
        found = BY_NAME.get(kind)
        if found is None:
            raise NotificationError(f"Pas de type « {kind} » : {', '.join(BY_NAME)}.")
        payload: dict[str, str] = {"kind": kind}
        if level:
            if level not in found.levels:
                raise NotificationError(
                    f"« {found.label} » se règle sur : {', '.join(found.levels)}."
                )
            payload["level"] = level
        if channel:
            if not found.channels:
                raise NotificationError(f"« {found.label} » part là où il a écrit.")
            if channel not in (AUTO, EVERYWHERE, *channels):
                raise NotificationError(f"Canal : {', '.join([AUTO, EVERYWHERE, *channels])}.")
            payload["channel"] = channel
        if len(payload) == 1:
            raise NotificationError("Dis le niveau (level) ou le canal (channel).")
        await self._journal.append(
            [NewEvent(kind=SET, payload=payload, provenance=by, trust=trust)]
        )
        await self.sync()
        return self.route(kind)

    def view(self, channels: list[str]) -> dict[str, Any]:
        """What the app shows: every kind, his choice, and what may be chosen."""
        return {
            "kinds": [
                {
                    "kind": k.name,
                    "label": k.label,
                    "help": k.help,
                    "level": self.route(k.name).level,
                    "channel": self.route(k.name).channel,
                    "levels": list(k.levels),
                    "channels": [AUTO, *channels, EVERYWHERE] if k.channels else [],
                    "direct": k.direct,
                    "default": k.default,
                }
                for k in KINDS
            ],
            "words": LEVEL_WORDS,
        }

    def lines(self) -> list[str]:
        """For Orion (`notifications` list)."""
        out: list[str] = []
        for k in KINDS:
            route = self.route(k.name)
            where = f", {route.channel}" if k.channels and route.channel != AUTO else ""
            out.append(f"- {k.name} ({k.label}, {k.help}) : {route.level}{where}")
        return out
