"""Getting to know the owner: one question a day, at a calm moment, never twice."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from orion.config import OrionConfig
from orion.core.events import NewEvent, Provenance, Trust
from orion.core.journal import SqliteJournal
from orion.initiative.engine import DELIVERED
from orion.memory.store import Fact
from orion.modules.registry import load
from orion.onboarding.module import OnboardingModule
from orion.onboarding.service import Onboarding, OnboardingSettings

PARIS = ZoneInfo("Europe/Paris")


def fact(statement: str, category: str = "profile") -> Fact:
    return Fact("f", statement, [], category, None, None, 1, None, None, Trust.OWNER, "x")


class Memory:
    def __init__(self) -> None:
        self.facts: list[Fact] = []

    async def __call__(self) -> list[Fact]:
        return self.facts


async def owner_says(journal: SqliteJournal, text: str = "salut") -> None:
    await journal.append(
        [
            NewEvent(
                kind="message.received",
                payload={"text": text},
                provenance=Provenance(source="imessage:owner", actor="owner:main"),
                trust=Trust.OWNER,
            )
        ]
    )


async def delivered(journal: SqliteJournal, key: str, day: str) -> None:
    await journal.append(
        [
            NewEvent(
                kind=DELIVERED,
                payload={"key": key, "day": day},
                provenance=Provenance(source="initiative", actor="orion:initiative"),
                trust=Trust.SYSTEM,
            )
        ]
    )


NOON_TOMORROW = datetime.combine(
    datetime.now(PARIS).date() + timedelta(days=1), datetime.min.time(), PARIS
) + timedelta(hours=12)


def at(hours: float) -> datetime:
    """Hours after tomorrow noon (the owner's messages carry the real clock, so earlier)."""
    return NOON_TOMORROW + timedelta(hours=hours)


def make(journal: SqliteJournal, memory: Memory, modules: set[str] | None = None) -> Onboarding:
    settings = OnboardingSettings(ask_after=datetime.min.time(), ask_before=datetime.max.time())
    return Onboarding(journal, settings, "Europe/Paris", memory, modules or set())


async def test_nothing_is_asked_before_the_owner_ever_wrote(journal: SqliteJournal) -> None:
    onboarding = make(journal, Memory())
    await onboarding.start()
    assert await onboarding.alerts(at(5)) == []


async def test_one_question_a_day_never_mid_conversation(journal: SqliteJournal) -> None:
    onboarding = make(journal, Memory())
    await owner_says(journal)
    soon = datetime.now(UTC) + timedelta(minutes=30)
    assert await onboarding.alerts(soon) == []  # they wrote half an hour ago
    [first] = await onboarding.alerts(at(2))
    assert first.key.startswith("onboarding:style:")
    assert "UNE question" in first.notice and first.fallback.startswith("Petite question")
    today = at(2).astimezone(PARIS).date()
    await delivered(journal, first.key, today.isoformat())
    assert await onboarding.alerts(at(3)) == []  # already one today
    [second] = await onboarding.alerts(at(24))
    assert second.key.startswith("onboarding:work:")


async def test_what_memory_holds_is_not_asked(journal: SqliteJournal) -> None:
    memory = Memory()
    memory.facts = [
        fact("Préfère des messages courts", "preference"),
        fact("Est étudiant en informatique à Lyon"),
    ]
    onboarding = make(journal, memory, {"calendar", "mail"})
    await owner_says(journal)
    [alert] = await onboarding.alerts(at(2))
    assert alert.key.startswith("onboarding:home:")
    assert "sources" not in [t.key for t in onboarding.topics()]  # already connected


async def test_the_owner_can_stop_it_and_it_ends_by_itself(journal: SqliteJournal) -> None:
    onboarding = make(journal, Memory())
    await owner_says(journal)
    assert await onboarding.alerts(at(24 * 22)) == []  # three weeks on: over
    await onboarding.stop()
    assert await onboarding.alerts(at(2)) == []


def test_on_by_default(tmp_path: Path) -> None:
    config: Any = OrionConfig.model_validate({"data_dir": tmp_path})
    assert any(isinstance(m, OnboardingModule) for m in load(config))
    off = OrionConfig.model_validate(
        {"data_dir": tmp_path, "modules": {"onboarding": {"enabled": False}}}
    )
    assert not any(isinstance(m, OnboardingModule) for m in load(off))
