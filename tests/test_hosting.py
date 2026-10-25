"""Running for real: backups of the journal, and the guardian watching the core."""

from __future__ import annotations

import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orion.cli import main
from orion.core.backup import BackupError, backup, verify
from orion.core.events import NewEvent, Provenance, Trust
from orion.core.hlc import HybridClock
from orion.core.journal import SqliteJournal
from orion.guardian.ledger import CORE_BACK, CORE_SILENT
from tests.fake_guardian import guardian_config, running_guardian


async def journal_with(path: Path, count: int) -> None:
    async with SqliteJournal(path, HybridClock("t")) as journal:
        await journal.append(
            [
                NewEvent(
                    kind="test.event",
                    payload={"n": n},
                    provenance=Provenance(source="test", actor="test"),
                    trust=Trust.OWNER,
                )
                for n in range(count)
            ]
        )


async def test_a_backup_is_a_checked_copy_and_old_ones_go(tmp_path: Path) -> None:
    source = tmp_path / "journal.sqlite3"
    await journal_with(source, 3)
    start = datetime(2026, 10, 1, tzinfo=UTC)
    results = [
        backup(source, tmp_path / "copies", keep=2, now=start + timedelta(days=d)) for d in range(3)
    ]
    assert [r.events for r in results] == [3, 3, 3]
    assert results[2].removed == [results[0].path]
    kept = sorted(p.name for p in (tmp_path / "copies").iterdir())
    assert kept == [results[1].path.name, results[2].path.name]
    copy = sqlite3.connect(results[2].path / "journal.sqlite3")
    assert copy.execute("SELECT count(*) FROM events").fetchone() == (3,)
    copy.close()


async def test_a_journal_under_an_odd_folder_name_is_backed_up(tmp_path: Path) -> None:
    folder = tmp_path / "Mes données #1 ?"
    folder.mkdir()
    source = folder / "journal.sqlite3"
    await journal_with(source, 2)
    now = datetime(2026, 10, 1, tzinfo=UTC)
    assert backup(source, tmp_path / "copies", keep=2, now=now).events == 2
    with pytest.raises(BackupError, match="already being made"):  # the same second, twice
        backup(source, tmp_path / "copies", keep=2, now=now)


def test_nothing_to_back_up_fails_and_leaves_nothing(tmp_path: Path) -> None:
    with pytest.raises(BackupError):
        backup(tmp_path / "missing.sqlite3", tmp_path / "copies", keep=3)
    assert list((tmp_path / "copies").iterdir()) == []


async def test_backup_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "data").mkdir()
    await journal_with(tmp_path / "data" / "journal.sqlite3", 2)
    (tmp_path / "orion.toml").write_text('data_dir = "data"\n')
    code = main(["--config", str(tmp_path / "orion.toml"), "backup", str(tmp_path / "b")])
    assert code == 0
    assert "2 événements copiés" in capsys.readouterr().out


async def test_the_guardian_tells_the_owner_when_the_core_goes_silent(tmp_path: Path) -> None:
    async with running_guardian(guardian_config(tmp_path)) as g:
        g.fake.inbound("salut")
        core = g.client.inkbox(max_attempts=1)
        await core.send("conv-owner", "Coucou", "o_1", "ident-1")
        await core.aclose()
        clock = [1000.0]
        g.service.monotonic = lambda: clock[0]
        await g.client.heartbeat()
        clock[0] += 200
        await g.service.check_core()
        assert len(g.fake.sent) == 1  # not yet
        clock[0] += 200
        await g.service.check_core()
        await g.service.check_core()  # once only
        assert "ne réponds plus" in g.fake.texts()[-1]
        assert len(g.fake.sent) == 2
        await g.client.heartbeat()
        assert "de nouveau là" in g.fake.texts()[-1]
        kinds = [e.kind for e in await g.journal.read()]
        assert kinds.count(CORE_SILENT) == 1 and kinds.count(CORE_BACK) == 1


async def test_the_watch_can_be_turned_off(tmp_path: Path) -> None:
    config = guardian_config(
        tmp_path,
        guardian={"enabled": True, "data_dir": tmp_path / "g", "core_silence_alert_seconds": 0},
    )
    async with running_guardian(config) as g:
        g.service.monotonic = lambda: 10**9
        await g.service.check_core()
        assert g.fake.sent == []


async def test_a_copy_is_checked_by_reading_every_event(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "journal.sqlite3"
    await journal_with(source, 3)
    result = backup(source, tmp_path / "copies", keep=2)
    assert main(["check-backup", str(result.path)]) == 0
    assert "3 événements, tous relisibles" in capsys.readouterr().out

    broken = tmp_path / "broken.sqlite3"
    shutil.copy(source, broken)
    db = sqlite3.connect(broken)
    db.execute("UPDATE events SET payload = 'pas du json' WHERE seq = 2")
    db.commit()
    db.close()
    with pytest.raises(BackupError, match="event 2 cannot be read"):
        verify(broken)
    assert main(["check-backup", str(broken)]) == 1
