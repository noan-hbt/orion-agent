"""Orion changes its own settings by message, checked whole first, then restarts (D72)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orion.config import load_config
from orion.configure.module import (
    CHANGED,
    Configurator,
    ConfigureError,
    apply_changes,
    configure_tools,
)
from orion.core.events import Provenance
from orion.core.journal import SqliteJournal
from orion.tools.base import Effect, ToolContext

TOML = """# Orion, chez Noan
instance = "orion-main"
data_dir = "data"

[initiative]
quiet_start = "22:30:00"  # pas après

[modules.web]
enabled = false
"""
OWNER = Provenance(source="thread:owner", actor="orion:core")


def test_a_change_keeps_comments_and_refuses_what_is_not_its_own() -> None:
    after = apply_changes(
        TOML, [("modules.web.enabled", True), ("modules.mail.refresh_minutes", 10)]
    )
    assert "# Orion, chez Noan" in after and "# pas après" in after
    assert "[modules.web]\nenabled = true" in after
    assert "[modules.mail]" in after
    for path in ("guardian.autonomable", "channels.imessage.owner_number",
                 "providers.openrouter.api_key", "delegation.allow_bash",
                 "modules.secretary.contacts", "data_dir"):  # fmt: skip
        with pytest.raises(ConfigureError):
            apply_changes(TOML, [(path, "x")])


async def test_the_file_is_checked_written_journaled_then_orion_restarts(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    source = tmp_path / "orion.toml"
    source.write_text(TOML, "utf-8")
    config = load_config(source)
    assert config.source == source.resolve()
    configurator = Configurator(journal, config.source)
    restarted: list[bool] = []

    async def restart() -> None:
        restarted.append(True)

    configurator.restart = restart
    _show, change = configure_tools(configurator, config)
    web_on = {"changes": [{"path": "modules.web.enabled", "value": True}], "why": "active le web"}
    # The owner approves the exact change, unless he let Orion do it alone.
    assert change.effect_for(change.parse(json.dumps(web_on))) is Effect.EXTERNAL

    bad = await change.handler(
        change.parse(
            json.dumps(
                {
                    "changes": [{"path": "initiative.daily_budget", "value": "beaucoup"}],
                    "why": "plus de messages",
                }
            )
        ),
        ToolContext("owner", "c"),
    )
    assert bad.is_error and "would not start" in bad.content
    assert source.read_text("utf-8") == TOML  # nothing written

    ok = await change.handler(
        change.parse(
            json.dumps(
                {
                    "changes": [{"path": "modules.web.enabled", "value": True}],
                    "why": "active le web",
                }
            )
        ),
        ToolContext("owner", "c"),
    )
    assert "redémarre" in ok.content
    import asyncio

    await asyncio.sleep(0)
    assert restarted == [True]
    assert load_config(source).modules["web"]["enabled"] is True
    assert (tmp_path / "orion.toml.previous").read_text("utf-8") == TOML
    (event,) = [e for e in await journal.read() if e.kind == CHANGED]
    assert event.payload["changes"] == [{"path": "modules.web.enabled", "value": True}]
    assert event.payload["why"] == "active le web"

    same = await configurator.change([("modules.web.enabled", True)], "encore", OWNER)
    assert same.startswith("Rien ne change")


async def test_a_module_that_could_not_start_is_refused(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    source = tmp_path / "orion.toml"
    source.write_text(TOML, "utf-8")
    configurator = Configurator(journal, load_config(source).source)
    with pytest.raises(ConfigureError, match="would not start"):
        await configurator.change([("voice.enabled", True)], "x", OWNER)


def test_show_lists_every_module_even_absent_from_the_file(tmp_path: Path) -> None:
    from orion.configure.module import editable_view

    source = tmp_path / "orion.toml"
    source.write_text(TOML, "utf-8")
    view = editable_view(load_config(source))
    assert "- console : éteint" in view and "- web : éteint" in view
    assert "guardian" not in view and "channels" not in view
