"""Messages between the core and the guardian process (JSON over its Unix socket)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orion.mail.imap import Batch, RawMail


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=1, max_length=64)
    params: dict[str, Any]
    reason: str = Field(default="", max_length=400)
    conversation_id: str


class MailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uidvalidity: int
    last_uid: int
    lookback_days: int
    limit: int
    now: datetime


def mail_request(body: bytes) -> MailRequest:
    return MailRequest.model_validate_json(body)


def batch_to_json(batch: Batch) -> dict[str, Any]:
    return {
        "uidvalidity": batch.uidvalidity,
        "last_uid": batch.last_uid,
        "skipped": batch.skipped,
        "mails": [
            {
                "uid": m.uid,
                "message_id": m.message_id,
                "from_addr": m.from_addr,
                "from_name": m.from_name,
                "subject": m.subject,
                "received_at": m.received_at.isoformat(),
                "body": m.body,
                "bulk": m.bulk,
            }
            for m in batch.mails
        ],
    }


def batch_from_json(data: dict[str, Any]) -> Batch:
    return Batch(
        int(data["uidvalidity"]),
        int(data["last_uid"]),
        [
            RawMail(
                int(m["uid"]),
                str(m["message_id"]),
                str(m["from_addr"]),
                str(m["from_name"]),
                str(m["subject"]),
                datetime.fromisoformat(str(m["received_at"])),
                str(m["body"]),
                bool(m["bulk"]),
            )
            for m in data["mails"]
        ],
        skipped=int(data.get("skipped", 0)),
    )
