"""Orion changes its own settings by message, checked whole first, then restarts (D72)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orion.config import load_config
from orion.configure.module import (
    CHANGED,
    Change,
    Configurator,
    ConfigureError,
    apply_changes,
    configure_tools,
)
from orion.core.events import Provenance
from orion.core.journal import SqliteJournal
from orion.tools.base import Effect, ToolContext, ToolInputError

TOML = """# Orion, chez Noan
instance = "orion-main"
data_dir = "data"

[initiative]
quiet_start = "22:30:00"  # pas après

[modules.web]
enabled = false
"""
OWNER = Provenance(source="thread:owner", actor="orion:core")


def _set(path: str, value: object) -> Change:
    return Change(path=path, value=value)


def test_a_change_keeps_comments_and_refuses_secrets_and_paths() -> None:
    after = apply_changes(
        TOML, [_set("modules.web.enabled", True), _set("modules.mail.refresh_minutes", 10)]
    )
    assert "# Orion, chez Noan" in after and "# pas après" in after
    assert "[modules.web]\nenabled = true" in after
    assert "[modules.mail]" in after
    for path in ("providers.openrouter.api_key", "delegation.allow_bash", "data_dir",
                 "guardian.socket", "guardian.data_dir", "instance",
                 "channels.inkbox.signing_key"):  # fmt: skip
        with pytest.raises(ConfigureError):
            apply_changes(TOML, [_set(path, "x")])
    with pytest.raises(ConfigureError):  # nor hidden inside a value
        apply_changes(TOML, [_set("modules.mail", {"enabled": True, "data_dir": "/"})])


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
    assert change.name == "configure_change"  # its own tool: approvals and autonomy by name
    web_on = {"changes": [{"path": "modules.web.enabled", "value": True}], "why": "active le web"}
    # The owner approves the exact change, unless he let Orion do it alone.
    assert change.effect_for(change.parse(json.dumps(web_on))) is Effect.EXTERNAL

    # A change Orion could not start with is refused before the owner is asked.
    bad = {"changes": [{"path": "initiative.daily_budget", "value": "beaucoup"}], "why": "plus"}
    with pytest.raises(ToolInputError, match="would not start"):
        change.parse(json.dumps(bad))
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

    same = await configurator.change([_set("modules.web.enabled", True)], "encore", OWNER)
    assert same.startswith("Rien ne change")


async def test_a_module_that_could_not_start_is_refused(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    source = tmp_path / "orion.toml"
    source.write_text(TOML, "utf-8")
    configurator = Configurator(journal, load_config(source).source)
    with pytest.raises(ConfigureError, match="would not start"):
        await configurator.change([_set("voice.enabled", True)], "x", OWNER)


def test_show_lists_every_module_even_absent_from_the_file(tmp_path: Path) -> None:
    from orion.configure.module import editable_view

    source = tmp_path / "orion.toml"
    source.write_text(TOML, "utf-8")
    view = editable_view(load_config(source))
    assert "- console : éteint" in view and "- web : éteint" in view
    assert '"guardian"' in view and '"telegram"' in view  # his, with his yes
    assert "socket" not in view and "api_key" not in view and "data_dir" not in view


ANGELE = {"name": "Angèle", "telegram_id": 42, "about": "ma copine", "share": ["availability"]}


def test_someone_is_added_and_removed_from_a_list_by_name(tmp_path: Path) -> None:
    add = Change(path="modules.secretary.contacts", value=ANGELE, op="add")
    after = apply_changes(TOML, [add])
    assert "# Orion, chez Noan" in after
    assert "[[modules.secretary.contacts]]" in after and 'name = "Angèle"' in after
    source = tmp_path / "orion.toml"
    source.write_text(after, "utf-8")
    assert load_config(source).modules["secretary"]["contacts"][0]["telegram_id"] == 42
    paul = {"name": "Paul", "number": "+33611111111"}
    both = apply_changes(after, [Change(path="modules.secretary.contacts", value=paul, op="add")])
    gone = apply_changes(
        both, [Change(path="modules.secretary.contacts", value="angèle", op="remove")]
    )
    assert "Angèle" not in gone and 'name = "Paul"' in gone
    with pytest.raises(ConfigureError, match="no 'Zoé' to remove"):
        apply_changes(gone, [Change(path="modules.secretary.contacts", value="Zoé", op="remove")])


def test_the_owners_settings_are_never_changed_alone() -> None:
    from orion.configure.paths import tier
    from orion.guardian.limits import within

    for path, value in (
        ("modules.secretary.contacts", ANGELE),
        ("guardian.autonomable", ["mail_send"]),
        ("channels.telegram.owner_id", 1),
        ("budget.daily_usd", 100),
        ("modules.mail.accounts", [{"name": "x", "host": "evil.example"}]),
        ("modules.web", {"enabled": True, "password": {"env": "TELEGRAM_BOT_TOKEN"}}),
        ("modules.console.public_url", "https://evil.example"),  # where his button points
    ):
        assert tier(path, value) == "owner", path
        assert not within("configure_change", None, {"changes": [{"path": path, "value": value}]})
    assert tier("modules.web.enabled", True) == "orion"
    assert within("configure_change", None, {"changes": [{"path": "agent.model", "value": "m"}]})


def test_a_parent_path_does_not_hide_the_owners_setting() -> None:
    from orion.configure.paths import tier
    from orion.guardian.limits import within

    for path, value in (
        ("modules.secretary", {"contacts": [ANGELE]}),
        ("modules", {"secretary": {"contacts": [ANGELE]}}),
        ("modules", {"shell": {"enabled": True}}),
    ):
        assert tier(path, value) == "owner", (path, value)
    asked = {"changes": [{"path": "agent", "value": {"approvals": {"mail_send": "never"}}}]}
    assert not within("configure_change", None, asked)


SPLIT_TOML = """# chez Noan
data_dir = "core"

[channels.imessage]
enabled = true
identity = "orion-test"
owner_number = "{owner}"

[guardian]
enabled = true
data_dir = "guardian"
"""


async def test_split_the_guardian_writes_the_file_after_the_owners_yes(
    journal: SqliteJournal, tmp_path: Path
) -> None:
    from orion.guardian.client import GuardianUnavailableError
    from orion.guardian.ledger import CONFIG_CHANGED
    from tests.fake_guardian import running_guardian
    from tests.fake_inkbox import CONVERSATION
    from tests.fake_inkbox import OWNER as OWNER_NUMBER

    source = tmp_path / "orion.toml"
    source.write_text(SPLIT_TOML.format(owner=OWNER_NUMBER), "utf-8")
    source.chmod(0o640)
    restarted: list[bool] = []

    async def restart() -> None:
        restarted.append(True)

    async with running_guardian(load_config(source)) as g:
        g.service.restart = restart
        g.fake.inbound("salut")
        add = [{"path": "modules.secretary.contacts", "value": ANGELE, "op": "add"}]
        with pytest.raises(GuardianUnavailableError, match="needs the owner's approval"):
            await g.client.configure(add, "ajoute Angèle", None)
        # Even Orion's own settings: it was not let do them alone.
        with pytest.raises(GuardianUnavailableError, match="needs the owner's approval"):
            await g.client.configure([{"path": "agent.model", "value": "m"}], "x", None)
        # The core asks with the tool's arguments as it parsed them (`op` included).
        asked = {"changes": [{**add[0]}], "why": "ajoute Angèle"}
        grant, message_id = await g.client.request_approval(
            "configure_change", asked, "", CONVERSATION
        )
        [question] = g.fake.texts()
        assert "modules.secretary.contacts : ajouter" in question and "Angèle" in question
        with pytest.raises(GuardianUnavailableError, match="has not said yes"):
            await g.client.configure(add, "ajoute Angèle", grant)
        g.fake.react(message_id, "like")
        other = [{"path": "modules.secretary.contacts", "value": {**ANGELE, "share": []}}]
        with pytest.raises(GuardianUnavailableError, match="not what the owner approved"):
            await g.client.configure(other, "ajoute Angèle", grant)

        configurator = Configurator(journal, source, guardian=g.client)
        said = await configurator.change(
            [Change.model_validate(add[0])], "ajoute Angèle", OWNER, grant
        )
        assert "Réglage écrit" in said
        assert "# chez Noan" in source.read_text("utf-8")
        assert load_config(source).modules["secretary"]["contacts"][0]["name"] == "Angèle"
        assert source.stat().st_mode & 0o777 == 0o640  # the core still reads it
        assert restarted == [True]
        assert CONFIG_CHANGED in [e.kind for e in await g.journal.read()]
        assert [e.kind for e in await journal.read()].count(CHANGED) == 1
        with pytest.raises(GuardianUnavailableError, match="already used"):
            await g.client.configure(add, "ajoute Angèle", grant)


def test_a_change_never_overwrites_one_made_meanwhile(tmp_path: Path) -> None:
    from orion.configure.module import write

    source = tmp_path / "orion.toml"
    source.write_text('[agent]\nmodel = "a"\n', "utf-8")
    before = source.read_text("utf-8")
    source.write_text('[agent]\nmodel = "b"\n', "utf-8")  # another change landed first
    with pytest.raises(ConfigureError, match="changed meanwhile"):
        write(source, before, '[agent]\nmodel = "c"\n')
    assert 'model = "b"' in source.read_text("utf-8")
