"""Reading vCards (2.1 to 4.0), as phones export them: an address book in one `.vcf` file.

Only what Orion needs is read: the name, nicknames, phone numbers, addresses, the birthday and
the organisation. Folded lines, escapes and quoted-printable (old Android exports) are handled;
anything else (photos, custom fields) is skipped.
"""

from __future__ import annotations

import hashlib
import quopri
import re
from dataclasses import dataclass

MAX_CARDS = 5000
_BDAY = re.compile(r"^(?:(\d{4})|--)-?(\d{2})-?(\d{2})")


@dataclass(frozen=True)
class Birthday:
    month: int
    day: int
    year: int | None = None


@dataclass(frozen=True)
class Card:
    uid: str
    name: str
    nicknames: tuple[str, ...] = ()
    phones: tuple[str, ...] = ()
    emails: tuple[str, ...] = ()
    birthday: Birthday | None = None
    org: str = ""

    def payload(self) -> dict[str, object]:
        birthday = self.birthday
        return {
            "uid": self.uid,
            "name": self.name,
            "nicknames": list(self.nicknames),
            "phones": list(self.phones),
            "emails": list(self.emails),
            "birthday": (
                {"month": birthday.month, "day": birthday.day, "year": birthday.year}
                if birthday
                else None
            ),
            "org": self.org,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> Card:
        found = payload.get("birthday")
        birthday: Birthday | None = None
        if isinstance(found, dict):
            year = found.get("year")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            birthday = Birthday(
                int(str(found["month"])),  # pyright: ignore[reportUnknownArgumentType]
                int(str(found["day"])),  # pyright: ignore[reportUnknownArgumentType]
                int(str(year)) if year else None,  # pyright: ignore[reportUnknownArgumentType]
            )
        return cls(
            uid=str(payload["uid"]),
            name=str(payload["name"]),
            nicknames=tuple(str(n) for n in _list(payload.get("nicknames"))),
            phones=tuple(str(p) for p in _list(payload.get("phones"))),
            emails=tuple(str(e) for e in _list(payload.get("emails"))),
            birthday=birthday,
            org=str(payload.get("org") or ""),
        )

    def line(self) -> str:
        parts = [self.name]
        if self.nicknames:
            parts.append(f"dit {', '.join(self.nicknames)}")
        if self.org:
            parts.append(self.org)
        if self.phones:
            parts.append("tél. " + ", ".join(self.phones))
        if self.emails:
            parts.append(", ".join(self.emails))
        if self.birthday:
            b = self.birthday
            parts.append(f"né(e) le {b.day:02d}/{b.month:02d}" + (f"/{b.year}" if b.year else ""))
        return " · ".join(parts)


def _list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []  # pyright: ignore[reportUnknownArgumentType]


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif lines and lines[-1].endswith("=") and "QUOTED-PRINTABLE" in lines[-1].upper():
            lines[-1] = lines[-1][:-1] + raw  # a soft line break, in quoted-printable
        else:
            lines.append(raw)
    return lines


def _value(params: str, value: str) -> str:
    upper = params.upper()
    if "QUOTED-PRINTABLE" in upper:
        charset = re.search(r"CHARSET=([\w-]+)", upper)
        value = quopri.decodestring(value.encode("latin-1", "replace")).decode(
            charset.group(1).lower() if charset else "utf-8", "replace"
        )
    return (
        value.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";")
    ).strip()


def birthday(value: str) -> Birthday | None:
    found = _BDAY.match(value.strip())
    if found is None:
        return None
    year, month, day = found.group(1), int(found.group(2)), int(found.group(3))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    year_known = int(year) if year and year not in ("1604", "0000") else None  # iOS: no year
    return Birthday(month, day, year_known)


def _phone(value: str) -> str:
    return re.sub(r"[^\d+]", "", value.removeprefix("tel:"))


def parse(text: str) -> list[Card]:
    """Every card with a name, in the order given (at most `MAX_CARDS`)."""
    cards: list[Card] = []
    fields: dict[str, list[str]] = {}
    inside = False
    for line in _unfold(text):
        head, sep, value = line.partition(":")
        if not sep:
            continue
        name, _, params = head.partition(";")
        name = name.split(".")[-1].upper()  # « item1.TEL »
        if name == "BEGIN" and value.strip().upper() == "VCARD":
            inside, fields = True, {}
        elif name == "END" and inside:
            inside = False
            card = _card(fields)
            if card is not None:
                cards.append(card)
                if len(cards) >= MAX_CARDS:
                    break
        elif inside:
            fields.setdefault(name, []).append(_value(params, value))
    return cards


def _card(fields: dict[str, list[str]]) -> Card | None:
    name = next((n for n in fields.get("FN", []) if n), "")
    if not name and fields.get("N"):
        last, first, *_ = [*fields["N"][0].split(";"), "", ""]
        name = " ".join(p for p in (first.strip(), last.strip()) if p)
    if not name:
        return None
    phones = tuple(dict.fromkeys(p for p in map(_phone, fields.get("TEL", [])) if len(p) >= 4))
    emails = tuple(dict.fromkeys(e.lower() for e in fields.get("EMAIL", []) if "@" in e))
    nicknames = tuple(
        n.strip() for v in fields.get("NICKNAME", []) for n in v.split(",") if n.strip()
    )
    uid = next((u for u in fields.get("UID", []) if u), "")
    if not uid:
        seed = "|".join([name.casefold(), *phones[:1], *emails[:1]])
        uid = "h_" + hashlib.sha256(seed.encode()).hexdigest()[:16]
    return Card(
        uid=uid[:200],
        name=name[:120],
        nicknames=nicknames[:5],
        phones=phones[:6],
        emails=emails[:6],
        birthday=next((b for b in map(birthday, fields.get("BDAY", [])) if b is not None), None),
        org=(fields.get("ORG") or [""])[0].replace(";", " ").strip()[:120],
    )
