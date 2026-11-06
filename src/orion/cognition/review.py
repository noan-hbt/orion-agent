"""The proof, after the words: his replies are read again once the conversation settles (D253).

D252 had a second model read every reply before it left: one more call and 1 to 3 s on each of
his messages (~130 a day, most on « bonne nuit »). The free part stays in the turn
(`grounding.ground_check`: a state claimed with no tool run is sent back at once). The model
now reads in the background, several replies in one call, once the conversation has been quiet
for `QUIET` or `EVERY` replies have piled up. What nothing the tools showed supports comes back
to Orion as an alert: he goes back to it, checks with the tool that shows it, and corrects
himself in his own words, through the initiative engine and the outbox like anything he says
first (SILENCE if the check bears his reply out).

Where the reading stands is in the journal: each one leaves `grounding.reviewed` (up to which
message, what was wrong, when), so after a restart it goes on after the last one and still
passes on what it found.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

import structlog

from orion.cognition.grounding import evidence
from orion.cognition.verifier import Verifier
from orion.core.events import NewEvent, Provenance, Trust
from orion.core.journal import EventStore
from orion.initiative.engine import Alert
from orion.providers.base import Message
from orion.runtime import OWNER_THREAD, Orion

log = structlog.get_logger(__name__)

REVIEWED = "grounding.reviewed"
QUIET = timedelta(seconds=30)
"""No new message in his conversation for this long: the exchange has settled, read it."""
EVERY = 5
"""Or this many replies not read yet, even while it goes on."""
TICK_SECONDS = 10.0
EXPIRES = timedelta(hours=2)
"""Past this, a correction comes too late to be worth a message."""

NOTICE = (
    "Une relecture de tes dernières réponses y a vu ce qu'aucun résultat d'outil ne "
    "montrait : {issues}. Reviens-y : vérifie avec l'outil qui le montre (le journal, le mail, "
    "la page, le coffre, l'agenda…). Si tu t'es avancé, dis-lui franchement ce qu'il en est "
    "vraiment, en une ou deux phrases. Si la vérification te donne raison, réponds SILENCE."
)

_ACTOR = Provenance(source="orion:grounding", actor="orion:verifier")


def _text(message: Message) -> str:
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _is_note(message: Message) -> bool:
    return message.get("role") == "user" and _text(message).startswith("[Orion")


def replies(messages: Sequence[Message]) -> list[int]:
    """Indexes of the replies that reached him in his own turns: an initiative is read before
    it goes (`InitiativeEngine`), and a draft sent back by a note never left."""
    found: list[int] = []
    his = False
    for i, message in enumerate(messages):
        role = message.get("role")
        if role == "user" and not _is_note(message):
            his = not _text(message).startswith("<initiative")
        elif (
            his
            and role == "assistant"
            and not message.get("tool_calls")
            and _text(message).strip()
            and not (i + 1 < len(messages) and _is_note(messages[i + 1]))
        ):
            found.append(i)
    return found


class GroundingReview:
    """An alert source (`InitiativeEngine`) fed by a background reading of his replies."""

    def __init__(
        self,
        orion: Orion,
        verifier: Verifier,
        journal: EventStore,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._orion = orion
        self._verifier = verifier
        self._journal = journal
        self._clock = clock
        self._through: int | None = None
        """Journal position of the last reply read; None until loaded from the journal."""
        self._found: list[tuple[str, list[str], datetime]] = []
        """(key, issues, when) of readings that found something."""
        self._seen = 0
        self._seen_at = clock()

    async def run(self) -> None:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            await self.tick()

    async def tick(self) -> bool:
        """Reads what is due; True if it read."""
        thread = self._orion.threads.get(OWNER_THREAD)
        if self._through is None:
            await self._load(thread.after_seq(thread.base), thread.last_seq)
        assert self._through is not None
        now = self._clock()
        if thread.last_seq != self._seen:
            self._seen, self._seen_at = thread.last_seq, now
        if self._orion.lock(OWNER_THREAD).locked():
            return False  # a turn is running: its reply is not final yet
        window = thread.messages[thread.base :]
        seqs = thread.seqs[thread.base :]
        due = [i for i in replies(window) if seqs[i] > self._through]
        if not due or (len(due) < EVERY and now - self._seen_at < QUIET):
            return False
        last = due[-1]
        facts, words = evidence(window[: last + 1])
        drafts = "\n\n---\n\n".join(_text(window[i]).strip() for i in due)
        verdict = await self._verifier.check(drafts, facts or "(aucun résultat d'outil)", words)
        failed = not verdict.ok and (
            not verdict.issues or verdict.issues[0].startswith("verifier ")
        )
        issues = [] if verdict.ok or failed else verdict.issues
        if failed:
            # Not read, and said so: the next replies are read; these stay as they went.
            log.warning("grounding.review_failed", issues=verdict.issues, replies=len(due))
        elif issues:
            log.info("grounding.unproven", issues=issues, replies=len(due))
        await self._journal.append(
            [
                NewEvent(
                    kind=REVIEWED,
                    payload={
                        "thread_id": OWNER_THREAD,
                        "through": seqs[last],
                        "replies": len(due),
                        "issues": issues,
                        "failed": failed,
                        "at": now.isoformat(),
                    },
                    provenance=_ACTOR,
                    trust=Trust.SYSTEM,
                )
            ]
        )
        self._through = seqs[last]
        if issues:
            self._found.append((f"grounding:{seqs[last]}", issues, now))
        return True

    async def _load(self, after: int, last: int) -> None:
        """Where the last reading stopped, and what it found, from the journal (from the
        start of his conversation's window; none yet: from now on)."""
        through: int | None = None
        while True:
            events = await self._journal.read(after_seq=after)
            if not events:
                break
            for event in events:
                if event.kind == REVIEWED and event.payload.get("thread_id") == OWNER_THREAD:
                    through = int(event.payload["through"])
                    if event.payload.get("issues"):
                        self._found.append(
                            (
                                f"grounding:{through}",
                                [str(i) for i in event.payload["issues"]],
                                datetime.fromisoformat(str(event.payload["at"])),
                            )
                        )
            after = events[-1].seq
        self._through = through if through is not None else last

    async def alerts(self, now: datetime) -> list[Alert]:
        self._found = [f for f in self._found if now - f[2] < EXPIRES]
        return [
            Alert(
                key=key,
                notice=NOTICE.format(issues=" ; ".join(issues)[:800]),
                why="je relis ce que je t'ai dit",
                fallback="",
                expires_at=at + EXPIRES,
                importance=0.8,
                # His own answer set right, not an idea of Orion's: never held by the budget.
                spoken_to=True,
            )
            for key, issues, at in self._found
        ]
