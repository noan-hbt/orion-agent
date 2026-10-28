from __future__ import annotations

from pathlib import Path

import pytest

from orion.app import build_app
from orion.channels.imessage import IMessageChannel
from orion.channels.inkbox import WebhookEvent
from orion.channels.module import IMessageModule
from orion.cli import main
from orion.config import ChannelsConfig, IMessageConfig, OrionConfig
from orion.core.supervisor import Supervisor
from orion.modules.base import ModuleError
from orion.runtime import is_silence
from orion.serve import owner_inbox, start
from orion.voice.module import VoiceModule
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


async def test_serve_asks_through_the_inbox_and_shares_the_registry(
    tmp_path: Path,
) -> None:
    """D88: the inbox puts an action to the owner on the channel he last wrote from."""
    cfg = config(tmp_path)
    module = IMessageModule(cfg, FakeInkbox().client())
    async with build_app(cfg, ScriptedProvider(), [module]) as app:
        inbox = owner_inbox(app)
        channel = await module.connect(app.plate, inbox)
        assert isinstance(channel, IMessageChannel)
        assert app.approver.target is inbox
        tools = {spec.name for spec in app.orion._registry.specs()}
        assert {"later", "remember", "plan"} <= tools


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
    cfg = config(tmp_path)
    module = IMessageModule(cfg, FakeInkbox(identity=identity).client())
    async with build_app(cfg, ScriptedProvider(), [module]) as app:
        with pytest.raises(ModuleError, match=message):
            await module.connect(app.plate, owner_inbox(app))
    cfg = config(tmp_path, owner_number="")
    module = IMessageModule(cfg, FakeInkbox().client())
    async with build_app(cfg, ScriptedProvider(), [module]) as app:
        with pytest.raises(ModuleError, match="owner_number"):
            await module.connect(app.plate, owner_inbox(app))


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


async def test_imessage_and_voice_modules_plug_into_the_daemon(tmp_path: Path) -> None:
    cfg = OrionConfig.model_validate(
        {
            "data_dir": tmp_path,
            "channels": {
                "imessage": {
                    "enabled": True,
                    "identity": "orion-test",
                    "owner_number": "+33611111111",
                }
            },
            "voice": {"enabled": True},
            "adaptation": {"enabled": False},
        }
    )
    fake = FakeInkbox()
    line = IMessageModule(cfg, fake.client())
    voice = VoiceModule(cfg)
    async with build_app(cfg, ScriptedProvider(), [line, voice]) as app:
        assert "call_me" in {s.name for s in app.orion._registry.specs()}  # pyright: ignore[reportPrivateUsage]
        assert "# Ta voix" in app.orion._compiler.system_message()["content"]  # pyright: ignore[reportPrivateUsage]
        supervisor = Supervisor()
        daemon = await start(app, owner_inbox(app), supervisor)
        assert supervisor.services == [
            "inbox",
            "memory.batch",
            "imessage.poll",
            "voice.brief",
            "voice.poll",
            "autonomy.ritual",
            "initiative",
        ]
        assert voice.voice is not None and voice.link.target is voice.voice
        assert daemon.caller is voice.voice and daemon.reports == [voice.voice]
        # A finished call on the shared Inkbox line reaches the voice module.
        call = fake.call(turns=[("user", "salut")])
        await line.dispatch(
            WebhookEvent.model_validate(
                {
                    "id": "evt-1",
                    "event_type": "call.ended",
                    "timestamp": "2026-10-19T21:00:00Z",
                    "data": {"call": call, "transcript": {"entries": []}},
                }
            )
        )
        assert call["id"] in voice.voice.state.ended
