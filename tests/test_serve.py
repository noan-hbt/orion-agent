from __future__ import annotations

from pathlib import Path

import pytest

from orion.app import build_app
from orion.channels.imessage import IMessageChannel
from orion.cli import main
from orion.config import ChannelsConfig, IMessageConfig, OrionConfig
from orion.runtime import is_silence
from orion.serve import ServeError, connect_imessage, owner_inbox
from tests.fake_inkbox import FakeInkbox
from tests.fakes import ScriptedProvider


def config(tmp_path: Path, **imessage: object) -> OrionConfig:
    settings: dict[str, object] = {
        "enabled": True,
        "identity": "orion-test",
        "owner_number": "+33611111111",
    }
    settings.update(imessage)
    return OrionConfig(
        data_dir=tmp_path,
        channels=ChannelsConfig(imessage=IMessageConfig.model_validate(settings)),
    )


async def test_serve_wires_the_channel_as_approver_and_shares_the_registry(
    tmp_path: Path,
) -> None:
    async with build_app(config(tmp_path), ScriptedProvider()) as app:
        channel = await connect_imessage(app, FakeInkbox().client(), owner_inbox(app))
        assert isinstance(channel, IMessageChannel)
        assert app.approver.target is channel
        tools = {spec.name for spec in app.orion._registry.specs()}
        assert {"remind_me", "remember", "current_time"} <= tools


@pytest.mark.parametrize(
    ("identity", "message"),
    [
        (None, "not found"),
        ({"id": "i", "agent_handle": "x", "imessage_enabled": False}, "not enabled"),
    ],
)
async def test_serve_refuses_a_half_configured_identity(
    tmp_path: Path, identity: dict[str, object] | None, message: str
) -> None:
    async with build_app(config(tmp_path), ScriptedProvider()) as app:
        with pytest.raises(ServeError, match=message):
            await connect_imessage(app, FakeInkbox(identity=identity).client(), owner_inbox(app))
    async with build_app(config(tmp_path, owner_number=""), ScriptedProvider()) as app:
        with pytest.raises(ServeError, match="owner_number"):
            await connect_imessage(app, FakeInkbox().client(), owner_inbox(app))


def test_serve_without_a_channel_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    toml = tmp_path / "orion.toml"
    toml.write_text('data_dir = "data"\n', encoding="utf-8")
    assert main(["--config", str(toml), "serve"]) == 2
    assert "Aucun canal" in capsys.readouterr().out


def test_silence_detection() -> None:
    assert is_silence("SILENCE") and is_silence(" silence. ")
    assert not is_silence("Silence radio de Léa depuis lundi.")
