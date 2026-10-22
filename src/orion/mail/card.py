"""A mail's card: what is known of it from its headers alone (D114).

There used to be a quarantine: a separate model read each mail and filled the card (category,
importance, a summary, a promise). The owner wanted no other mind between him and Orion: Orion
now reads his mail itself (`MailCard.text`, third-party content), and judges it. The card keeps
only what the headers say: a newsletter or a mailing list, or not. Cards made by the quarantine
before stay readable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any, Literal, cast


class Category(StrEnum):
    PERSONAL = "personal"
    WORK = "work"
    BILL = "bill"
    DELIVERY = "delivery"
    TRAVEL = "travel"
    NEWSLETTER = "newsletter"
    NOTIFICATION = "notification"
    SPAM = "spam"
    OTHER = "other"


class Importance(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


@dataclass(frozen=True)
class Promise:
    by: Literal["owner", "sender"]
    what: str
    due: date | None = None


@dataclass(frozen=True)
class Card:
    category: Category
    importance: Importance
    needs_reply: bool
    due: date | None
    amount: float | None
    currency: str | None
    summary: str
    suspicious: bool
    read: Literal["model", "bulk", "failed", "headers"]
    """How the card was made: from headers (since D114), or by the old quarantine model."""
    promise: Promise | None = None

    def payload(self) -> dict[str, object]:
        promise = self.promise
        return {
            **(
                {
                    "promise": {
                        "by": promise.by,
                        "what": promise.what,
                        "due": promise.due.isoformat() if promise.due else None,
                    }
                }
                if promise
                else {}
            ),
            "category": self.category.value,
            "importance": self.importance.value,
            "needs_reply": self.needs_reply,
            "due": self.due.isoformat() if self.due else None,
            "amount": self.amount,
            "currency": self.currency,
            "summary": self.summary,
            "suspicious": self.suspicious,
            "read": self.read,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> Card:
        due = payload.get("due")
        amount = payload.get("amount")
        found = payload.get("promise")
        promise: Promise | None = None
        if isinstance(found, dict):
            raw = cast(dict[str, Any], found)
            promise = Promise(
                "owner" if raw.get("by") == "owner" else "sender",
                str(raw["what"]),
                date.fromisoformat(str(raw["due"])) if raw.get("due") else None,
            )
        return cls(
            category=Category(str(payload["category"])),
            importance=Importance(str(payload["importance"])),
            needs_reply=bool(payload["needs_reply"]),
            due=date.fromisoformat(str(due)) if due else None,
            amount=float(str(amount)) if amount is not None else None,
            currency=str(payload["currency"]) if payload.get("currency") else None,
            summary=str(payload.get("summary", "")),
            suspicious=bool(payload.get("suspicious", False)),
            read=str(payload.get("read", "model")),  # type: ignore[arg-type]
            promise=promise,
        )


BULK_CARD = Card(
    Category.NEWSLETTER, Importance.LOW, False, None, None, None, "", False, read="bulk"
)
HEADERS_CARD = Card(
    Category.OTHER, Importance.NORMAL, False, None, None, None, "", False, read="headers"
)


def headers_card(bulk: bool) -> Card:
    """What the headers say: a newsletter or a list (`List-Unsubscribe`…), or a mail for him."""
    return BULK_CARD if bulk else HEADERS_CARD
