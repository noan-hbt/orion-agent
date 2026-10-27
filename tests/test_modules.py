from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from orion.app import build_app
from orion.channels.inbox import Inbound
from orion.cli import main
from orion.config import OrionConfig
from orion.core.events import Trust
from orion.core.supervisor import Supervisor
from orion.initiative.engine import Report
from orion.modules.base import Daemon, Module, ModuleError, ModuleSettings, Plate
from orion.modules.registry import builtin_modules, load
from orion.serve import owner_inbox, start
from orion.tools.base import Effect, Reversibility, Tool, ToolContext, ToolResult
from tests.fakes import ScriptedProvider


class WeatherSettings(ModuleSettings):
    city: str = "Paris"


class _NoArgs(BaseModel):
    pass


class Weather(Module):
    """A third-party module: a tool, a constitution section, a service, a report source."""

    name = "weather"
    settings_model = WeatherSettings

    def __init__(self, config: OrionConfig) -> None:
        super().__init__(config)
        self.closed = False

    def instructions_text(self) -> str:
        return f"# La météo\n\nTu connais la météo de {self.settings.city} avec `weather`."

    async def assemble(self, plate: Plate) -> list[Tool]:
        async def weather(_args: Any, _ctx: ToolContext) -> ToolResult:
            return ToolResult(f"Il pleut à {self.settings.city}.", trust=Trust.THIRD_PARTY)

        return [
            Tool(
                name="weather",
                description="Today's weather.",
                input_model=_NoArgs,
                handler=weather,
                effect=Effect.NONE,
                reversibility=Reversibility.REVERSIBLE,
                accepts_untrusted_context=True,
            )
        ]

    async def serve(self, daemon: Daemon) -> None:
        daemon.add_service("weather.watch", self.watch)
        daemon.reports.append(self)

    async def watch(self) -> None:  # pragma: no cover - never run in these tests
        return None

    async def reports(self) -> list[Report]:
        return []

    async def close(self) -> None:
        self.closed = True


class Desk(Module):
    """A third-party owner channel that needs the weather module."""

    name = "desk"
    channel = True
    requires = ("weather",)

    async def serve(self, daemon: Daemon) -> None:
        daemon.inbox.register(self)

    async def available(self) -> bool:
        return True

    async def deliver(self, text: str, correlation_id: str | None) -> None:
        return None

    @asynccontextmanager
    async def working(self, item: Inbound) -> AsyncGenerator[None]:
        yield


THIRD_PARTY: list[type[Module]] = [*builtin_modules(), Weather, Desk]


def config(tmp_path: Path, **modules: dict[str, Any]) -> OrionConfig:
    return OrionConfig.model_validate({"data_dir": tmp_path, "modules": modules})


def test_load_orders_dependencies_first_and_fails_closed(tmp_path: Path) -> None:
    both = config(tmp_path, desk={"enabled": True}, weather={"enabled": True, "city": "Lyon"})
    assert [m.name for m in load(both, THIRD_PARTY)] == [
        "adaptation",
        "consolidation",
        "doubt",
        "feeds",
        "files",
        "onboarding",
        "focus",
        "expert",
        "missions",
        "tasks",
        "library",
        "photos",
        "papers",
        "thinking",
        "watch",
        "awaiting",
        "chronicle",
        "intentions",
        "relations",
        "contacts",
        "departure",
        "advice",
        "style",
        "configure",
        "vocal",
        "recap",
        "skills",
        "weather",
        "desk",
    ]

    with pytest.raises(ModuleError, match="'desk' needs 'weather'"):
        load(config(tmp_path, desk={"enabled": True}), THIRD_PARTY)
    with pytest.raises(ModuleError, match=r"\[modules.radio\] is configured but"):
        load(config(tmp_path, radio={"enabled": True}), THIRD_PARTY)
    with pytest.raises(ValueError, match="citi"):
        load(config(tmp_path, weather={"enabled": True, "citi": "Lyon"}), THIRD_PARTY)
    voice_alone = OrionConfig.model_validate({"data_dir": tmp_path, "voice": {"enabled": True}})
    with pytest.raises(ModuleError, match="'voice' needs 'imessage'"):
        load(voice_alone, THIRD_PARTY)


async def test_a_module_snaps_on_tools_instructions_services_and_a_channel(
    tmp_path: Path,
) -> None:
    cfg = config(tmp_path, desk={"enabled": True}, weather={"enabled": True, "city": "Lyon"})
    modules = load(cfg, THIRD_PARTY)
    async with build_app(cfg, ScriptedProvider(), modules) as app:
        tools = [spec.name for spec in app.orion._registry.specs()]  # pyright: ignore[reportPrivateUsage]
        assert "weather" in tools and tools == sorted(tools)
        system = app.orion._compiler.system_message()["content"]  # pyright: ignore[reportPrivateUsage]
        weather = system.index("Tu connais la météo de Lyon avec `weather`.")
        assert weather < system.index("# Ce que tu pourrais faire de plus")  # offers close it
        assert "# Ta voix" not in system  # the voice module is off: its section is too
        assert "Lui parler au téléphone" in system  # …and it is offered instead

        inbox = owner_inbox(app)
        supervisor = Supervisor()
        daemon = await start(app, inbox, supervisor)
        assert supervisor.services == [
            "inbox",
            "memory.batch",
            "adaptation",
            "consolidation",
            "feeds",
            "missions.work",
            "tasks.work",
            "thinking.night",
            "watch",
            "chronicle.week",
            "configure.watch",
            "weather.watch",
            "autonomy.ritual",
            "initiative",
        ]
        assert daemon.reports == [app.module(Weather)]
        assert await inbox.available()
    weather = next(m for m in modules if isinstance(m, Weather))
    assert weather.closed


async def test_serve_needs_an_owner_channel(tmp_path: Path) -> None:
    cfg = config(tmp_path, weather={"enabled": True})
    async with build_app(cfg, ScriptedProvider(), load(cfg, THIRD_PARTY)) as app:
        with pytest.raises(ModuleError, match="no owner channel"):
            await start(app, owner_inbox(app), Supervisor())


def test_doctor_lists_every_module(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    toml = tmp_path / "orion.toml"
    toml.write_text('data_dir = "d"\n[delegation]\nenabled = true\nbinary = "no-such-bin"\n')
    assert main(["--config", str(toml), "doctor"]) == 0
    out = capsys.readouterr().out
    assert "delegation:    on (no-such-bin not found on PATH)" in out
    assert "imessage:      off" in out
