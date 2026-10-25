"""Event versions: the journal is never rewritten, so old events are read in today's shape.

An event's payload is stored as it was written, with the version of its kind at that time. When
a kind's payload changes shape (a field renamed, a value split in two), the change does not
touch the journal: a function that turns version N into N+1 is added to `UPCASTERS`, and every
read applies the ones the event has not been through. A kind renamed as a whole goes in
`RENAMED`; its events are read under the new name.

Rules for an upcaster: pure (same input, same output, no clock, no I/O), total (it must accept
every payload of its version, including ones missing optional fields), and never deleted once
shipped: a journal written years ago still needs it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

Payload = dict[str, Any]
Upcaster = Callable[[Payload], Payload]

UPCASTERS: dict[str, list[Upcaster]] = {}
"""kind → [v1 → v2, v2 → v3, …]. The current version of a kind is 1 + len(its list)."""

RENAMED: dict[str, str] = {}
"""old kind → new kind, read as the new kind (then upcast as the new kind, from version 1)."""


def version_of(kind: str) -> int:
    """The version a payload of `kind` is written at today."""
    return 1 + len(UPCASTERS.get(kind, ()))


def upcast(kind: str, version: int, payload: Payload) -> tuple[str, Payload]:
    """An event as written (`kind`, `version`, `payload`), in today's shape."""
    if version < 1:
        raise ValueError(f"event of kind {kind!r} has no valid version ({version})")
    if kind in RENAMED:
        kind, version = RENAMED[kind], 1  # its old versions are the new kind's first shape
    steps = UPCASTERS.get(kind, [])
    if version > len(steps) + 1:
        raise ValueError(
            f"event of kind {kind!r} is version {version}, newer than this Orion knows "
            f"({len(steps) + 1}): run the newer Orion, or roll the journal back from a backup"
        )
    for step in steps[version - 1 :]:
        payload = step(dict(payload))
    return kind, payload
