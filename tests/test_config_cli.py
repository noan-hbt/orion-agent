from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from orion.channels.inkbox import InkboxClient
from orion.cli import main
from orion.config import OrionConfig, SecretRef, load_config


def test_defaults_when_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert load_config() == OrionConfig()


def test_load_resolves_relative_data_dir(tmp_path: Path) -> None:
    path = tmp_path / "orion.toml"
    path.write_text('instance = "orion-test"\ndata_dir = "state"\n[logging]\nlevel = "DEBUG"\n')
    config = load_config(path)
    assert config.instance == "orion-test"
    assert config.data_dir == tmp_path / "state"
    assert config.logging.level == "DEBUG"


def test_unknown_keys_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "orion.toml"
    path.write_text('instnace = "typo"\n')
    with pytest.raises(ValidationError):
        load_config(path)


def test_explicit_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "absent.toml")


def test_secret_ref_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    ref = SecretRef(env="ORION_TEST_SECRET")
    monkeypatch.delenv("ORION_TEST_SECRET", raising=False)
    with pytest.raises(LookupError):
        ref.resolve()
    monkeypatch.setenv("ORION_TEST_SECRET", "s3cret")
    assert ref.resolve().get_secret_value() == "s3cret"
    assert "s3cret" not in repr(ref.resolve())


def test_cli_doctor_creates_journal(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "orion.toml"
    path.write_text('data_dir = "d"\n[logging]\njson_output = false\n')
    assert main(["--config", str(path), "doctor"]) == 0
    out = capsys.readouterr().out
    assert "0 events" in out
    assert "calls:     off" in out
    assert (tmp_path / "d" / "journal.sqlite3").exists()


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == "0.1.0"


def test_timezones_resolve_without_system_tzdata() -> None:
    # The `tzdata` package ships the IANA database, for hosts that lack it (Windows, slim images).
    import tzdata  # noqa: F401  # pyright: ignore[reportUnusedImport]

    OrionConfig.model_validate({"agent": {"timezone": "Europe/Paris"}})


def test_unknown_timezone_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown timezone"):
        OrionConfig.model_validate({"agent": {"timezone": "Europe/Pariss"}})


def test_cli_inkbox_calls_shows_why_calls_ended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from orion import cli
    from tests.fake_inkbox import FakeInkbox

    fake = FakeInkbox()
    fake.call(duration=0)["status"] = "failed"
    next(iter(fake.phone_calls.values()))["hangup_reason"] = "rejected"

    def client(_config: OrionConfig) -> InkboxClient:
        return fake.client()

    monkeypatch.setattr(cli, "_inkbox", client)
    path = tmp_path / "orion.toml"
    path.write_text('[channels.imessage]\nidentity = "orion-test"\n')
    assert main(["--config", str(path), "inkbox", "calls"]) == 0
    assert "failed (rejected)" in capsys.readouterr().out
