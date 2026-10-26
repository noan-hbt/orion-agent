"""The app as a module (D125): an https domain, not yet beside a separate guardian."""

from __future__ import annotations

from pathlib import Path

import pytest

from orion.cli import main
from orion.config import OrionConfig
from orion.core.hlc import HybridClock
from orion.core.journal import SqliteJournal
from orion.mobile.devices import Devices, session_key
from orion.mobile.module import MobileModule, check_url
from orion.modules.base import ModuleError
from orion.modules.registry import builtin_modules


def test_a_passkey_needs_a_domain_in_https() -> None:
    assert check_url("https://orion.example/") == "https://orion.example"
    assert check_url("http://localhost:8795") == "http://localhost:8795"
    for wrong in ("http://orion.example", "http://51.15.12.3:8795", "orion.example"):
        with pytest.raises(ModuleError, match="https"):
            check_url(wrong)


async def test_not_beside_a_separate_guardian_yet(tmp_path: Path) -> None:
    config = OrionConfig.model_validate(
        {
            "data_dir": tmp_path,
            "guardian": {"enabled": True},
            "modules": {"mobile": {"enabled": True, "public_url": "https://orion.example"}},
        }
    )
    with pytest.raises(ModuleError, match="guardian"):
        await MobileModule(config).assemble(None)  # type: ignore[arg-type]
    assert MobileModule in builtin_modules()


async def test_orion_app_pair_prints_a_code_that_pairs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    toml = tmp_path / "orion.toml"
    toml.write_text(
        '[modules.mobile]\nenabled = true\npublic_url = "https://orion.example"\n', "utf-8"
    )
    assert await _run(["--config", str(toml), "app", "pair", "iphone"]) == 0
    out = capsys.readouterr().out
    assert "https://orion.example/app/" in out
    code = out.rsplit(": ", 1)[1].strip()
    config = OrionConfig.model_validate({"data_dir": tmp_path / "data"})
    async with SqliteJournal(config.journal_path, HybridClock("t")) as journal:
        devices = Devices(
            journal, "https://orion.example", session_key(tmp_path / "data" / "mobile")
        )
        await devices.pairing_options(code)  # a valid code
    assert await _run(["--config", str(toml), "app", "devices"]) == 0
    assert "Aucun appareil" in capsys.readouterr().out


async def _run(argv: list[str]) -> int:
    import asyncio

    return await asyncio.to_thread(main, argv)
