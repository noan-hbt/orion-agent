"""Which settings Orion may change by message, and who must say yes (D92).

Three kinds of keys in orion.toml:

- **Orion's own** (`agent`, `initiative`, `modules`…): changed with the owner's yes, or alone
  once he let it (`configure_change` is autonomable);
- **the owner's** (`guardian`, `channels`, `federation`, `budget`, who may write to Orion):
  what protects him and who Orion obeys. Changed only with his yes to the exact change, which
  the guardian process checks itself when Orion runs split, never alone;
- **never by message**: secrets and what runs on the server (keys, binaries, `allow_bash`,
  where data lives). Those are set on the server, by `orion setup`.

Kept free of heavy imports: the guardian process and the autonomy limits use it too.
"""

from __future__ import annotations

from typing import Any, Literal, cast

Tier = Literal["orion", "owner"]

ORION_SECTIONS = ("agent", "initiative", "adaptation", "memory", "modules", "voice", "delegation")
"""Top-level sections that are Orion's own."""
OWNER_SECTIONS = ("guardian", "channels", "federation", "budget", "logging", "providers")
"""Top-level sections only the owner's yes changes."""
OWNER_PATHS = ("modules.secretary.contacts", "modules.shell", "modules.mcp")
"""Inside Orion's sections, what is the owner's: who may write to Orion, what may run on the
server or act through others' tools."""
OWNER_KEYS = (
    "accounts",
    "env",
    "password",
    "secret",
    "token",
    "host",
    "url",
    "urls",
    "server",
    "servers",
)
"""Keys that are the owner's wherever they are, also as a suffix (`public_url`): where a secret
goes and to whom (a mail account's server and the variable holding its password, a calendar's
secret address), and where Orion sends him (the console's address)."""
FORBIDDEN_KEYS = (
    "allow_bash",
    "binary",
    "api_key",
    "base_url",
    "signing_key",
    "key",
    "socket",
    "data_dir",
    "path",
    "command",
)
"""Keys never changed by message, wherever they are: a secret, what runs on the server, where
data lives."""


class ConfigureError(ValueError):
    pass


def check_path(path: str) -> list[str]:
    """The path's parts. Raises ConfigureError for what is never changed by message."""
    parts = path.strip().split(".")
    if not all(parts) or parts[0] not in (*ORION_SECTIONS, *OWNER_SECTIONS):
        raise ConfigureError(
            f"{path!r}: not a setting that can be changed by message (sections: "
            f"{', '.join((*ORION_SECTIONS, *OWNER_SECTIONS))})"
        )
    if any(p in FORBIDDEN_KEYS or p.endswith("_dir") for p in parts):
        raise ConfigureError(
            f"{path!r}: never changed by message (a secret, or what runs on the server): it is "
            "set on the server, with `orion setup`"
        )
    return parts


def _keys(value: object) -> set[str]:
    """Every key inside a value (a table, a list of tables)."""
    if isinstance(value, dict):
        table = cast(dict[object, object], value)
        found = {str(k) for k in table}
        for item in table.values():
            found |= _keys(item)
        return found
    if isinstance(value, list):
        return set[str]().union(*(_keys(item) for item in cast(list[object], value)))
    return set()


def _owners_key(key: str) -> bool:
    return key in OWNER_KEYS or any(key.endswith(f"_{k}") for k in OWNER_KEYS)


def tier(path: str, value: object = None) -> Tier:
    parts = check_path(path)
    joined = ".".join(parts)
    inside = _keys(value)
    if any(k in FORBIDDEN_KEYS or k.endswith("_dir") for k in inside):
        raise ConfigureError(f"{path!r}: the value holds a key never changed by message")
    if (
        parts[0] in OWNER_SECTIONS
        or any(joined == p or joined.startswith(p + ".") for p in OWNER_PATHS)
        or any(_owners_key(k) for k in (*parts, *inside))
    ):
        return "owner"
    return "orion"


def owners(changes: list[Any]) -> bool:
    """Whether any of these changes (dicts with a `path` and a `value`) is the owner's to
    approve. A change that cannot be read counts as his."""
    for change in changes:
        if not isinstance(change, dict):
            return True
        found = cast(dict[str, Any], change)
        try:
            if tier(str(found.get("path", "")), found.get("value")) == "owner":
                return True
        except ConfigureError:
            return True
    return False
