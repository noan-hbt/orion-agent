"""A guardian process served in-process: the real service, reached over ASGI instead of its
Unix socket, in front of the in-memory Inkbox."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from orion.config import OrionConfig
from orion.core.hlc import HybridClock
from orion.core.journal import SqliteJournal
from orion.guardian.client import GuardianClient
from orion.guardian.ledger import Ledger
from orion.guardian.service import GuardianService
from tests.fake_inkbox import OWNER, FakeInkbox


def guardian_config(tmp_path: Path, **extra: Any) -> OrionConfig:
    data: dict[str, Any] = {
        "data_dir": tmp_path / "core",
        "channels": {
            "imessage": {"enabled": True, "identity": "orion-test", "owner_number": OWNER}
        },
        "guardian": {"enabled": True, "data_dir": tmp_path / "guardian"},
    }
    data.update(extra)
    return OrionConfig.model_validate(data)


@dataclass
class Running:
    fake: FakeInkbox
    service: GuardianService
    client: GuardianClient
    journal: SqliteJournal


@asynccontextmanager
async def running_guardian(
    config: OrionConfig, fake: FakeInkbox | None = None, **kwargs: Any
) -> AsyncGenerator[Running]:
    fake = fake or FakeInkbox()
    async with SqliteJournal(
        config.guardian_ledger_path, HybridClock(f"{config.instance}-guardian")
    ) as journal:
        service = GuardianService(config, Ledger(journal), fake.client(), **kwargs)
        await service.start()
        app = service.app()
        client = GuardianClient(lambda: httpx.ASGITransport(app=app))
        try:
            yield Running(fake, service, client, journal)
        finally:
            await client.aclose()
            await service.aclose()
