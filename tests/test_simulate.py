"""A week of initiative played ahead on a copy: what Orion would send, nothing sent (D82)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from orion.cli import main
from orion.config import OrionConfig
from orion.core.events import Provenance, Trust
from orion.core.hlc import HybridClock
from orion.core.journal import SqliteJournal
from orion.initiative.simulate import simulate
from orion.initiative.timers import TimerProjection, Timers

PARIS = ZoneInfo("Europe/Paris")
MONDAY = datetime(2026, 10, 26, 7, 0, tzinfo=PARIS)


async def a_journal(path: Path) -> int:
    async with SqliteJournal(path, HybridClock("t")) as journal:
        timers = Timers(journal, TimerProjection())
        await timers.schedule(
            MONDAY.replace(hour=23, minute=30),
            "sortir les poubelles",
            "owner",
            Provenance(source="test", actor="owner:main"),
            Trust.OWNER,
        )
        return await journal.last_seq()


async def test_a_week_is_played_on_a_copy_and_nothing_is_sent(tmp_path: Path) -> None:
    config = OrionConfig.model_validate({"data_dir": tmp_path})
    before = await a_journal(config.journal_path)
    week = await simulate(config, days=2, start=MONDAY)
    shown = week.render()
    assert "Rien n'a été envoyé." in shown
    assert "23:30  timer" in shown  # a reminder he asked for goes, quiet hours or not
    assert "Lundi 26/10 : 1 envoyé(s)" in shown
    async with SqliteJournal(config.journal_path, HybridClock("t")) as journal:
        assert await journal.last_seq() == before  # the real journal is untouched
    assert not any(p.name.startswith("orion-simulate-") for p in tmp_path.iterdir())


def test_the_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "orion.toml").write_text(f'data_dir = "{tmp_path}"\n')
    asyncio.run(a_journal(tmp_path / "journal.sqlite3"))
    assert main(["--config", str(tmp_path / "orion.toml"), "simulate", "--days", "1"]) == 0
    assert "Simulation du" in capsys.readouterr().out
    assert main(["--config", str(tmp_path / "orion.toml"), "simulate", "--days", "0"]) == 1
