"""Files only Orion's user may read (keys, logins, cookies, the owner's files): created 0600,
never readable by anyone else, not even for the moment between writing and `chmod`."""

from __future__ import annotations

import os
from pathlib import Path


def write_private(path: Path, data: bytes | str, *, exclusive: bool = False) -> None:
    """`data` at `path`, mode 0600, replaced whole (a reader never sees half a file).
    `exclusive`: only if `path` does not exist yet (FileExistsError otherwise), so two
    processes creating the same key at once never overwrite each other."""
    raw = data.encode() if isinstance(data, str) else data
    if exclusive:
        _write(path, raw, os.O_EXCL)
        return
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        _write(tmp, raw, os.O_TRUNC)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _write(path: Path, raw: bytes, flag: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | flag, 0o600)
    with os.fdopen(fd, "wb") as file:
        file.write(raw)
    path.chmod(0o600)  # an existing file opened with O_TRUNC keeps its old mode
