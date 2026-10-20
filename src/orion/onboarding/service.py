"""The getting-to-know: what Orion still wants to learn, asked one question a day.

Orion knows nothing on day one ("pas grand-chose"). For the first weeks it asks one question a
day through the initiative engine (so quiet hours, the budget and the verifier apply), never
while a conversation is going on, and skips what memory already holds. The answers go through
ordinary memory. A topic counts as asked once a question about it was delivered; the owner can
stop the whole thing at any time (`onboarding_stop`).

    onboarding.stopped
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import Field

from orion.core.events import Event, NewEvent, Provenance, Trust
from orion.core.hlc import Timestamp
from orion.core.journal import EventStore
from orion.core.projections import ProjectionRunner
from orion.initiative.engine import DELIVERED, Alert
from orion.memory.store import Fact
from orion.modules.base import ModuleSettings

STOPPED = "onboarding.stopped"
OWNER_MESSAGE = "message.received"
PREFIX = "onboarding:"


@dataclass(frozen=True)
class Topic:
    key: str
    question: str
    """Asked as is if the model's draft fails."""
    ask: str
    """What to find out, for the model that writes the question."""
    known_if: tuple[str, ...] = ()
    """Fact categories or words that mean memory already holds it."""
    needs_missing: tuple[str, ...] = ()
    """Modules whose absence makes the topic relevant (connecting sources)."""


TOPICS: tuple[Topic, ...] = (
    Topic(
        "style",
        "tu préfères que je te parle comment ? Court, direct, avec un peu d'humour ?",
        "comment il préfère que tu lui parles : ton, longueur des messages, humour, "
        "à quel point tu peux être direct",
        ("preference",),
    ),
    Topic(
        "work",
        "tu fais quoi dans la vie, en ce moment ?",
        "ce qu'il fait dans la vie (travail ou études) et à quoi ressemble sa semaine",
        ("travail", "boulot", "études", "étudiant", "métier", "job", "entreprise"),
    ),
    Topic(
        "home",
        "tu vis où, et avec qui ?",
        "où il vit et avec qui",
        ("habite", "vit à", "appartement", "coloc"),
    ),
    Topic(
        "people",
        "qui sont les personnes qui comptent le plus pour toi ?",
        "les personnes qui comptent le plus pour lui (prénoms et liens), pour que "
        "tu les reconnaisses quand il en parle",
        ("person",),
    ),
    Topic(
        "rhythm",
        "tu te lèves et te couches vers quelle heure, d'habitude ?",
        "ses horaires habituels (lever, coucher, moments où il ne veut pas être dérangé)",
        ("réveil", "lève", "couche", "horaires"),
    ),
    Topic(
        "hopes",
        "qu'est-ce qui t'aiderait le plus au quotidien, de ma part ?",
        "ce qu'il attend surtout de toi au quotidien (rappels, organisation, "
        "recherches, appels, autre chose)",
    ),
    Topic(
        "projects",
        "tu as des projets ou des objectifs en ce moment ?",
        "ses projets ou objectifs du moment, pour l'aider à les tenir",
        ("project",),
    ),
    Topic(
        "sources",
        "tu veux que je lise ton agenda et tes mails ? Je t'explique comment me les brancher.",
        "s'il veut que tu lises son agenda et ses mails (ça se règle sur le "
        "serveur : propose de lui expliquer comment)",
        needs_missing=("calendar", "mail"),
    ),
)


class OnboardingSettings(ModuleSettings):
    enabled: bool = True
    days: int = Field(default=21, ge=1, le=90, description="How long after the first message")
    ask_after: time = Field(default=time(11, 0), description="No question before this time")
    ask_before: time = Field(default=time(20, 30), description="Nor after this one")
    quiet_minutes: int = Field(
        default=90, ge=0, description="Never within this long of the owner's last message"
    )


@dataclass
class OnboardingState:
    name: str = "onboarding"
    first_message: datetime | None = None
    last_message: datetime | None = None
    asked: dict[str, date] = field(default_factory=dict[str, date])
    stopped: bool = False
    _seq: int = 0

    async def checkpoint(self) -> int:
        return self._seq

    async def reset(self) -> None:
        self.first_message = self.last_message = None
        self.asked.clear()
        self.stopped = False
        self._seq = 0

    async def apply(self, event: Event) -> None:
        self._seq = event.seq
        if event.redacted:
            return
        payload: dict[str, Any] = event.payload
        if event.kind == OWNER_MESSAGE and event.trust >= Trust.OWNER:
            moment = datetime.fromtimestamp(Timestamp.decode(event.hlc).wall_ms / 1000, UTC)
            self.first_message = self.first_message or moment
            self.last_message = moment
        elif event.kind == DELIVERED and str(payload.get("key", "")).startswith(PREFIX):
            topic = str(payload["key"]).removeprefix(PREFIX).split(":", 1)[0]
            self.asked[topic] = date.fromisoformat(str(payload["day"]))
        elif event.kind == STOPPED:
            self.stopped = True


def known(topic: Topic, facts: list[Fact]) -> bool:
    for fact in facts:
        if fact.category in topic.known_if:
            return True
        text = fact.statement.casefold()
        if any(word in text for word in topic.known_if if " " in word or len(word) > 4):
            return True
    return False


class Onboarding:
    def __init__(
        self,
        journal: EventStore,
        settings: OnboardingSettings,
        timezone: str,
        facts: Callable[[], Any],
        enabled_modules: set[str],
    ) -> None:
        self._journal = journal
        self.settings = settings
        self._zone = ZoneInfo(timezone)
        self._facts = facts
        self._modules = enabled_modules
        self.state = OnboardingState()
        self._runner = ProjectionRunner(journal, [self.state])

    async def start(self) -> None:
        await self._runner.catch_up()

    def topics(self) -> list[Topic]:
        return [
            t
            for t in TOPICS
            if not t.needs_missing or any(m not in self._modules for m in t.needs_missing)
        ]

    async def next_topic(self, facts: list[Fact]) -> Topic | None:
        await self._runner.catch_up()
        for topic in self.topics():
            if topic.key not in self.state.asked and not known(topic, facts):
                return topic
        return None

    async def alerts(self, now: datetime) -> list[Alert]:
        await self._runner.catch_up()
        s, local = self.state, now.astimezone(self._zone)
        if s.stopped or s.first_message is None or s.last_message is None:
            return []
        if now - s.first_message > timedelta(days=self.settings.days):
            return []
        if not self.settings.ask_after <= local.time() < self.settings.ask_before:
            return []
        if local.date() in s.asked.values():
            return []  # one question a day
        if now - s.last_message < timedelta(minutes=self.settings.quiet_minutes):
            return []  # never in the middle of a conversation
        topic = await self.next_topic(await self._facts())
        if topic is None:
            return []
        end = datetime.combine(local.date(), self.settings.ask_before, self._zone)
        return [
            Alert(
                key=f"{PREFIX}{topic.key}:{local.date().isoformat()}",
                notice="Tu fais encore connaissance avec ton propriétaire. Pose-lui UNE "
                f"question, courte et naturelle (pas un questionnaire), pour savoir {topic.ask}."
                " S'il n'a pas envie d'en parler, c'est très bien : n'insiste pas.",
                why="on se connaît depuis peu et j'aimerais mieux te connaître",
                fallback=f"Petite question pour mieux te connaître : {topic.question}",
                expires_at=end,
            )
        ]

    async def stop(self, correlation_id: str | None = None) -> None:
        await self._journal.append(
            [
                NewEvent(
                    kind=STOPPED,
                    payload={},
                    provenance=Provenance(source="owner", actor="owner:main"),
                    trust=Trust.OWNER,
                    correlation_id=correlation_id,
                )
            ]
        )
        await self._runner.catch_up()
