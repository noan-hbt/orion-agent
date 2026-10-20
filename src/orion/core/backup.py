"""Backups of a journal: everything else is rebuilt from it.

The journal (or the guardian's ledger) is the only thing worth saving: memory, people,
reminders, mail cards and federation state are projections replayed from it. A backup is an
online SQLite copy (consistent while Orion writes), checked before it counts, in a folder named
after the time it was taken. Old copies beyond `keep` are removed, oldest first.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

STAMP = "%Y%m%dT%H%M%SZ"


class BackupError(RuntimeError):
    """The copy could not be made or does not check out."""


@dataclass(frozen=True)
class BackupResult:
    path: Path
    events: int
    removed: list[Path]


def _copy(source: Path, target: Path) -> int:
    if not source.exists():
        raise BackupError(f"nothing to back up: {source} does not exist")
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        (check,) = dst.execute("PRAGMA integrity_check").fetchone()
        if check != "ok":
            raise BackupError(f"the copy of {source} is damaged: {check}")
        (events,) = dst.execute("SELECT count(*) FROM events").fetchone()
        return int(events)
    except sqlite3.Error as exc:
        raise BackupError(f"backup of {source} failed: {exc}") from exc
    finally:
        dst.close()
        src.close()


def backup(source: Path, destination: Path, keep: int, now: datetime | None = None) -> BackupResult:
    """Copy `source` into `destination/<stamp>/`, then keep only the `keep` newest copies."""
    if keep < 1:
        raise BackupError("keep at least one copy")
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime(STAMP)
    folder = destination / stamp
    folder.mkdir(parents=True, exist_ok=False)
    folder.chmod(0o700)
    try:
        events = _copy(source, folder / source.name)
    except BackupError:
        shutil.rmtree(folder)
        raise
    copies = sorted(p for p in destination.iterdir() if p.is_dir() and _is_stamp(p.name))
    removed = copies[:-keep]
    for old in removed:
        shutil.rmtree(old)
    return BackupResult(folder, events, removed)


def _is_stamp(name: str) -> bool:
    try:
        datetime.strptime(name, STAMP)
    except ValueError:
        return False
    return True
