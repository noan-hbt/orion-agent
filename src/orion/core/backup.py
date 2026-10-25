"""Backups of a journal: everything else is rebuilt from it.

The journal (or the guardian's ledger) is the only thing worth saving: memory, people,
reminders, mail cards and federation state are projections replayed from it. A backup is an
online SQLite copy (consistent while Orion writes), checked before it counts, in a folder named
after the time it was taken. Old copies beyond `keep` are removed, oldest first.

« Checked » means restorable (D81): the copy passes SQLite's integrity check, and every event
in it is read back the way Orion would read it at start (today's shape, `orion.core.upcast`),
with no gap in the sequence. A backup that cannot be replayed is not a backup.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from orion.core.journal import event_from_row

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
    src = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    except sqlite3.Error as exc:
        dst.close()
        src.close()
        raise BackupError(f"backup of {source} failed: {exc}") from exc
    dst.close()
    src.close()
    return verify(target)


def verify(path: Path) -> int:
    """Check that a journal (or a copy of one) can be restored: intact, and every event
    readable by this Orion, in order with no gap. Returns the number of events."""
    if not path.exists():
        raise BackupError(f"no journal at {path}")
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        (check,) = db.execute("PRAGMA integrity_check").fetchone()
        if check != "ok":
            raise BackupError(f"{path} is damaged: {check}")
        columns = {row["name"] for row in db.execute("PRAGMA table_info(events)")}
        version = "version" if "version" in columns else "1 AS version"
        rows = db.execute(
            "SELECT seq, id, hlc, kind, payload, source, actor, derived_from, trust, "
            f"causation_id, correlation_id, redacted, {version} FROM events ORDER BY seq"
        )
        count = 0
        for row in rows:
            count += 1
            if row["seq"] != count:
                raise BackupError(f"{path}: event {count} is missing (next is {row['seq']})")
            try:
                event_from_row(row)
            except (ValueError, ValidationError) as exc:
                raise BackupError(f"{path}: event {row['seq']} cannot be read: {exc}") from exc
    except sqlite3.Error as exc:
        raise BackupError(f"{path} cannot be read: {exc}") from exc
    finally:
        db.close()
    return count


def backup(source: Path, destination: Path, keep: int, now: datetime | None = None) -> BackupResult:
    """Copy `source` into `destination/<stamp>/`, then keep only the `keep` newest copies."""
    if keep < 1:
        raise BackupError("keep at least one copy")
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime(STAMP)
    folder = destination / stamp
    try:
        folder.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise BackupError(f"a backup is already being made at {folder}") from exc
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
