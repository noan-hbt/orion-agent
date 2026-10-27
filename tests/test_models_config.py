"""Every model Orion uses can be changed in orion.toml: none is written in the code."""

from __future__ import annotations

import re
from pathlib import Path

from orion.config import OrionConfig

SOURCE = Path(__file__).parent.parent / "src" / "orion"
MODEL = re.compile(
    r"[\"'](?:anthropic|openai|google|deepseek|qwen|mistralai|meta-llama|x-ai|moonshotai|z-ai)"
    r"/[\w.:-]+[\"']"
)


def test_no_model_is_written_outside_the_config() -> None:
    found = [
        f"{path.relative_to(SOURCE)}: {match}"
        for path in SOURCE.rglob("*.py")
        if path.name != "config.py"
        for match in MODEL.findall(path.read_text(encoding="utf-8"))
    ]
    assert found == []


def test_the_agent_section_names_every_shared_model() -> None:
    config = OrionConfig.model_validate(
        {"agent": {"strong_model": "a/strong", "vision_model": "a/eyes", "reflection_model": "a/r"}}
    )
    assert {"a/strong", "a/eyes", "a/r"} <= config.agent.models()
    assert config.agent.summary_model is None  # falls back to extractor_model


async def test_modules_take_their_model_from_the_config(tmp_path: Path) -> None:
    from orion.app import build_app
    from orion.chronicle.module import ChronicleModule
    from orion.photos.module import PhotosModule
    from orion.thinking.module import ThinkingModule
    from tests.fakes import ScriptedProvider

    config = OrionConfig.model_validate(
        {
            "data_dir": tmp_path,
            "agent": {"strong_model": "a/strong", "vision_model": "a/eyes"},
            "modules": {"chronicle": {"model": "a/writer"}},
        }
    )
    async with build_app(config, ScriptedProvider()) as app:
        thinking = next(m for m in app.modules if isinstance(m, ThinkingModule)).thinking
        chronicle = next(m for m in app.modules if isinstance(m, ChronicleModule)).chronicle
        photos = next(m for m in app.modules if isinstance(m, PhotosModule)).photos
        assert thinking is not None and thinking.model == "a/strong"
        assert chronicle is not None and chronicle.model == "a/writer"  # its own section wins
        assert photos is not None and photos.model == "a/eyes"


def test_on_a_plan_the_background_follows_the_main_model() -> None:
    from orion.config import STRONG_MODEL, AgentConfig

    agent = AgentConfig(model="chatgpt:gpt-6-luna")
    assert agent.extractor_model == agent.strong_model == agent.vision_model == agent.model
    assert agent.verifier_model == "anthropic/claude-haiku-5.5"  # another family
    named = AgentConfig(model="chatgpt:gpt-6-luna", strong_model="chatgpt:gpt-6")
    assert named.strong_model == "chatgpt:gpt-6"
    claude = AgentConfig(model="claude-code:sonnet")
    assert claude.extractor_model == "claude-code:sonnet" != claude.vision_model
    assert AgentConfig(model="deepseek/deepseek-v4.1-flash").strong_model == STRONG_MODEL
