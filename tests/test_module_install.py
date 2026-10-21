"""Native modules installed and removed from the catalog, with the owner's yes (D105)."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from orion.config import load_config
from orion.configure.module import Configurator
from orion.core.events import Trust
from orion.core.journal import SqliteJournal
from orion.guardian.policy import ActionProposal, Guardian, Verdict
from orion.modules import installer as installing
from orion.modules.base import ModuleError
from orion.modules.installer import REPO, Installer, InstallError, installed, read_catalog
from orion.modules.registry import discover, load
from orion.modules.tools import module_tools
from orion.tools.base import ToolContext

EXAMPLE = REPO / "examples" / "modules" / "orion-des"
CTX = ToolContext("owner", "c")


def catalog(tmp_path: Path) -> Path:
    folder = tmp_path / "catalog"
    folder.mkdir()
    (folder / "des.toml").write_text(
        'name = "des"\ndescription = "Des dés"\nsource = "path:examples/modules/orion-des"\n'
    )
    (folder / "loose.toml").write_text(  # no exact version: never offered
        'name = "loose"\ndescription = "x"\nsource = "orion-loose>=1"\n'
    )
    return folder


def fake_uv(declares: str = "des") -> Any:
    """`uv pip install --target`, without the network: the example, as a wheel would lay it."""

    def run(command: list[str], env: dict[str, str]) -> tuple[int, str]:
        if command[1:3] != ["pip", "install"]:
            return installing.run_process(command, env)  # the check runs for real
        target = Path(command[command.index("--target") + 1])
        target.mkdir(parents=True)
        shutil.copytree(EXAMPLE / "orion_des", target / "orion_des")
        info = target / "orion_des-0.1.0.dist-info"
        info.mkdir()
        (info / "METADATA").write_text("Metadata-Version: 2.1\nName: orion-des\nVersion: 0.1.0\n")
        (info / "entry_points.txt").write_text(
            f"[orion.modules]\n{declares} = orion_des:DesModule\n"
        )
        return 0, ""

    return run


@pytest.fixture
def owner_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(sys, "path", list(sys.path))  # what a test activates goes with it
    source = tmp_path / "orion.toml"
    source.write_text('data_dir = "data"\n\n[modules.des]\nfaces = 20\n', "utf-8")
    return source


async def test_a_module_is_installed_turned_on_and_removed(
    journal: SqliteJournal, owner_file: Path, tmp_path: Path
) -> None:
    config = load_config(owner_file)
    assert list(read_catalog(catalog(tmp_path))) == ["des"]  # the unpinned one is left out
    installer = Installer(config, tmp_path / "catalog", fake_uv())
    configurator = Configurator(journal, owner_file)
    listing, install, remove = module_tools(installer, configurator)
    done = await install.handler(install.parse('{"name": "des"}'), CTX)
    assert not done.is_error, done.content
    assert "installé et allumé" in done.content
    assert list(installed(config.data_dir / "modules")) == ["des"]
    reloaded = load_config(owner_file)
    assert reloaded.modules["des"] == {"faces": 20, "enabled": True}  # its settings kept
    [des] = [m for m in load(reloaded) if m.name == "des"]
    assert des.settings.faces == 20
    assert "des : installé, allumé" in (await listing.handler(listing.parse("{}"), CTX)).content

    again = await install.handler(install.parse('{"name": "des"}'), CTX)
    assert again.is_error and "déjà installé" in again.content

    gone = await remove.handler(remove.parse('{"name": "des"}'), CTX)
    assert not gone.is_error, gone.content
    assert installed(config.data_dir / "modules") == {}
    after = load_config(owner_file)
    assert after.modules["des"]["enabled"] is False
    load(after)  # its section stays behind, off: Orion still starts
    with pytest.raises(ModuleError, match="no such module is installed"):
        load(load_config(owner_file).model_copy(update={"modules": {"des": {"enabled": True}}}))


def test_only_the_catalog_and_only_what_loads(owner_file: Path, tmp_path: Path) -> None:
    config = load_config(owner_file)
    folder = catalog(tmp_path)
    with pytest.raises(InstallError, match="pas dans le catalogue"):
        Installer(config, folder, fake_uv()).install("loose")
    (folder / "shell.toml").write_text(
        'name = "shell"\ndescription = "x"\nsource = "path:examples/modules/orion-des"\n'
    )
    with pytest.raises(InstallError, match="fait déjà partie d'Orion"):
        Installer(config, folder, fake_uv()).install("shell")
    # A package that is not the module it claims to be is left out, nothing kept.
    with pytest.raises(InstallError, match="ne se charge pas"):
        Installer(config, folder, fake_uv(declares="autre")).install("des")
    assert installed(config.data_dir / "modules") == {}
    assert not any((config.data_dir / "modules" / "site").iterdir())


def test_the_guardian_knows_installed_modules_without_running_them(
    owner_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config(owner_file)
    monkeypatch.setattr(installing._Process, "imports_modules", False)  # pyright: ignore[reportPrivateUsage]
    Installer(config, catalog(tmp_path), fake_uv()).install("des")  # by the core, in truth
    on = config.model_copy(update={"modules": {"des": {"enabled": True}}})
    assert "des" not in [k.name for k in discover(on)]  # never imported here
    assert "des" not in [m.name for m in load(on)]  # yet the config is valid


async def test_installing_code_is_always_put_to_him_and_never_from_outside_content(
    journal: SqliteJournal, owner_file: Path, tmp_path: Path
) -> None:
    config = load_config(owner_file)
    _, install, _ = module_tools(
        Installer(config, catalog(tmp_path), fake_uv()), Configurator(journal, owner_file)
    )
    args = install.parse('{"name": "des"}')

    async def always(_action: str, _params: dict[str, Any]) -> bool:
        return True  # even if the config listed it as something Orion may do alone

    alone = Guardian(autonomous=always, approvals="equilibre")
    decided = await alone.decide(ActionProposal(install, args, Trust.OWNER, "owner"))
    assert decided.verdict is Verdict.DENY  # asked (and the default approver says no)
    from_mail = await alone.decide(ActionProposal(install, args, Trust.THIRD_PARTY, "owner"))
    assert from_mail.verdict is Verdict.DENY and "cannot run" in from_mail.reason


async def test_split_the_guardian_turns_it_on_under_the_yes_to_install_it(
    journal: SqliteJournal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orion.configure.module import Change
    from orion.configure.paths import ConfigureError
    from orion.guardian.client import GuardianUnavailableError
    from tests.fake_guardian import running_guardian
    from tests.fake_inkbox import CONVERSATION
    from tests.fake_inkbox import OWNER as OWNER_NUMBER
    from tests.test_configure import OWNER, SPLIT_TOML

    monkeypatch.setattr(sys, "path", list(sys.path))
    source = tmp_path / "orion.toml"
    source.write_text(SPLIT_TOML.format(owner=OWNER_NUMBER), "utf-8")
    config = load_config(source)
    Installer(config, catalog(tmp_path), fake_uv()).install("des")
    switch = [Change(path="modules.des.enabled", value=True)]
    async with running_guardian(config) as g:
        g.fake.inbound("salut")
        grant, message_id = await g.client.request_approval(
            "module_install", {"name": "des"}, "", CONVERSATION
        )
        [question] = g.fake.texts()  # the guardian read the source from the catalog itself
        assert "installer le module « des »" in question
        assert "Source : path:examples/modules/orion-des" in question
        assert "ta mémoire comprise" in question
        configurator = Configurator(journal, source, guardian=g.client)
        with pytest.raises(ConfigureError, match="has not said yes"):
            await configurator.change(switch, "installer des", OWNER, grant, "des")
        g.fake.react(message_id, "like")
        sneaky = [Change(path="agent.model", value="m")]
        with pytest.raises(ConfigureError, match="covers only that module"):
            await configurator.change(sneaky, "installer des", OWNER, grant, "des")
        await configurator.change(switch, "installer des", OWNER, grant, "des")
        assert load_config(source).modules["des"]["enabled"] is True
        with pytest.raises(GuardianUnavailableError, match="already used"):
            await g.client.configure([switch[0].described()], "x", grant, "des")
