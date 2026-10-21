"""The fixed part of every request has a budget (D84).

The constitution and the tool schemas open every model request. They are cached, but every turn
still pays to carry them, and the longer they are the more one instruction drowns another. Each
module now adds a section and a tool or two: these tests make growth a decision. Going over
means trimming (what a tool does belongs in its description) or raising a number here, on
purpose, in the same commit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orion.app import MAX_SECTION_CHARS, build_app, constitution
from orion.config import OrionConfig
from orion.modules.base import Module, ModuleError
from orion.modules.registry import builtin_modules
from tests.fakes import ScriptedProvider

CONSTITUTION_ALL_MODULES = 38_500
"""Characters of the system message with every built-in module on (34.7k on 20/10/2026; 38.4k
on 21/10 with `awaiting`, D109)."""
TOOLS_DEFAULT = 41_500
"""Characters of the tool schemas with the default modules (35.2k on 20/10/2026; 41.0k on
21/10 with `awaiting`, D109)."""
ONE_TOOL = 2_600
"""Characters of one tool's schema, actions included (the largest, `mission`: 2.3k)."""


def test_the_system_message_with_every_module_fits(tmp_path: Path) -> None:
    config = OrionConfig.model_validate({"data_dir": tmp_path})
    every = [kind(config) for kind in builtin_modules()]
    assert all(len(m.instructions_text()) <= MAX_SECTION_CHARS for m in every)
    assert len(constitution(every)) <= CONSTITUTION_ALL_MODULES


async def test_the_tool_schemas_fit(tmp_path: Path) -> None:
    config = OrionConfig.model_validate({"data_dir": tmp_path})
    async with build_app(config, ScriptedProvider()) as app:
        specs = [s.to_openai() for s in app.orion.registry.specs()]
    sizes = {s["function"]["name"]: len(json.dumps(s, ensure_ascii=False)) for s in specs}
    assert max(sizes.values()) <= ONE_TOOL, max(sizes, key=lambda n: sizes[n])
    assert sum(sizes.values()) <= TOOLS_DEFAULT


def test_a_module_with_a_long_section_does_not_load(tmp_path: Path) -> None:
    class Talkative(Module):
        name = "talkative"

        def instructions_text(self) -> str:
            return "x" * (MAX_SECTION_CHARS + 1)

    config = OrionConfig.model_validate({"data_dir": tmp_path})
    with pytest.raises(ModuleError, match="'talkative': its instructions are 2501 characters"):
        constitution([Talkative(config)])
