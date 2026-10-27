from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orion.cli import _reminders  # pyright: ignore[reportPrivateUsage]
from orion.config import OrionConfig
from orion.core.events import Provenance, Trust
from orion.core.hlc import HybridClock
from orion.core.journal import SqliteJournal
from orion.initiative.timers import TimerProjection, Timers


async def test_the_owner_lists_and_cancels_a_reminder_from_the_command_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = OrionConfig.model_validate({"data_dir": tmp_path})
    async with SqliteJournal(config.journal_path, HybridClock(config.instance)) as journal:
        timer_id = await Timers(journal, TimerProjection()).schedule(
            datetime.now(UTC) + timedelta(days=1),
            "appeler le garage",
            "t1",
            Provenance(source="thread:t1", actor="orion:core"),
            Trust.OWNER,
        )

    assert await _reminders(config, argparse.Namespace(reminders_command="list")) == 0
    out = capsys.readouterr().out
    assert timer_id in out and "appeler le garage" in out

    unknown = argparse.Namespace(reminders_command="cancel", reminder_id="r_x", reason="test")
    assert await _reminders(config, unknown) == 1
    assert "r_x" in capsys.readouterr().err

    cancel = argparse.Namespace(reminders_command="cancel", reminder_id=timer_id, reason="test")
    assert await _reminders(config, cancel) == 0
    assert await _reminders(config, argparse.Namespace(reminders_command="list")) == 0
    assert "Aucun rappel programmé." in capsys.readouterr().out
