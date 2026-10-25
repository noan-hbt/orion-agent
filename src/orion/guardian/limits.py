"""Bounded delegations: what an autonomy covers, checked in code on every action.

An autonomy (`guardian.autonomy`) can carry limits the owner sets when granting it, and some
actions can only be granted with them: Orion never mails a stranger alone, and never sends a
form alone on a site the owner did not name.

- `recipients`: every recipient must be one of these addresses, or end with one of these
  `@domains` (for `contact_message`: the contacts' names);
- `domains`: every site the action may send to must be one of these domains (or under one).

The guardian process checks them (it holds what the action needs: the mail password, the
site's session), and the core checks them too, before even asking.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from orion.configure.paths import owners, reached

REQUIRED = {
    "mail_send": "recipients",
    "browser": "domains",
    "contact_message": "recipients",
}
"""Actions that can only be granted with this limit set."""
ASKED_ALWAYS = ("agent.approvals",)
"""Settings Orion never changes alone, whatever it was granted: what it asks him (D85)."""


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipients: list[str] = Field(
        default_factory=list[str], description="Addresses, or '@domain' for a whole domain"
    )
    domains: list[str] = Field(
        default_factory=list[str], description="Sites where sending is allowed (e.g. doctolib.fr)"
    )

    @property
    def empty(self) -> bool:
        return not self.recipients and not self.domains

    def describe(self) -> str:
        parts: list[str] = []
        if self.recipients:
            parts.append("seulement à " + ", ".join(self.recipients))
        if self.domains:
            parts.append("seulement sur " + ", ".join(self.domains))
        return " ; ".join(parts)


def missing(action: str, limits: Limits) -> str | None:
    """Why `action` cannot be granted with these limits, if it cannot."""
    needed = REQUIRED.get(action)
    if needed is not None and not getattr(limits, needed):
        return f"{action} can only be done alone with `{needed}` limits"
    return None


def host_in(host: str, domains: list[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d.lower() or host.endswith("." + d.lower()) for d in domains)


def _recipient_in(address: str, allowed: list[str]) -> bool:
    address = address.strip().lower()
    if not address or any(c in address for c in ',;<>"') or any(c.isspace() for c in address):
        return False  # one bare address only: `x@evil.com, y@ok.com` is two recipients
    return any(
        address == a.lower() or (a.startswith("@") and address.endswith(a.lower())) for a in allowed
    )


def within(action: str, limits: Limits | None, params: dict[str, Any]) -> bool:
    """Whether an action with these parameters stays inside the autonomy's limits."""
    if action == "configure_change" and (
        owners(_items(params.get("changes")))
        or any(
            each in ASKED_ALWAYS or any(each.startswith(a + ".") for a in ASKED_ALWAYS)
            for change in _items(params.get("changes"))
            for each in reached(_path(change), _value(change))
        )
    ):
        return False  # the owner's settings, and what Orion asks him, never alone (D92)
    if limits is None:
        return action not in REQUIRED
    if missing(action, limits):
        return False
    if limits.recipients:
        recipients = [
            r for key in ("to", "cc", "bcc", "contact") for r in _strings(params.get(key)) if r
        ]
        if not recipients or not all(_recipient_in(r, limits.recipients) for r in recipients):
            return False
    if limits.domains:
        if action == "browser":
            # Sending a page's form: that page's site. Signing in: the guardian checks the
            # account's own sites when it lends it.
            if params.get("action") == "login":
                return True
            sites = [urlsplit(str(params.get("page") or "")).hostname or ""]
        else:
            sites = _strings(params.get("sites"))
        if not all(sites) or not sites or not all(host_in(s, limits.domains) for s in sites):
            return False
    return True


def _items(value: object) -> list[object]:
    if isinstance(value, list | tuple):
        return list(value)  # pyright: ignore[reportUnknownArgumentType]
    return []


def _path(change: object) -> str:
    if isinstance(change, dict):
        return str(change.get("path", "")).strip()  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    return ""


def _value(change: object) -> object:
    if isinstance(change, dict):
        return change.get("value")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return None


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple):
        items: list[object] = list(value)  # pyright: ignore[reportUnknownArgumentType]
        return [str(item) for item in items]
    return []
