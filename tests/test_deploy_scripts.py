"""Starting over and removing Orion on a server (deploy/reset.sh, deploy/uninstall.sh), tried on
a pretend installation: systemctl is a stand-in that only notes what it is asked."""

from __future__ import annotations

import os
import subprocess
import tarfile
import tomllib
from pathlib import Path

from orion.config import parse_config
from orion.mobile.module import MobileSettings

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"

SYSTEMCTL = """#!/bin/sh
echo "$*" >> "$(dirname "$0")/calls"
case "$1" in is-active) [ "$3" = orion-update.timer ] && exit 0; exit 3;; esac
exit 0
"""


def installed(tmp_path: Path) -> tuple[Path, Path]:
    root, etc = tmp_path / "srv" / "orion", tmp_path / "etc"
    for name in (
        "data/journal.sqlite3",
        "data/memory.sqlite3",
        "data/.hidden",
        "data/browser/Cookies",
        "data/files/f1.pdf",
        "data/modules/meteo/module.py",
        "data/chatgpt/auth.json",
        "data/console.key",
        "guardian/guardian.sqlite3",
        "guardian/logins/doctolib.json",
        "guardian/secrets.env",
        "guardian/admin.token",
        "backups/core/journal-1.sqlite3",
        "config/orion.toml",
        "app/pyproject.toml",
    ):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(name)
    (etc / "orion").mkdir(parents=True)
    (etc / "orion" / "core.env").write_text("OPENROUTER_API_KEY=k")
    (etc / "systemd" / "system").mkdir(parents=True)
    for unit in ("orion-core.service", "orion-guardian.service", "orion-update.timer"):
        (etc / "systemd" / "system" / unit).write_text("[Unit]")
    (etc / "caddy").mkdir()
    (etc / "caddy" / "Caddyfile").write_text("# Orion's public face: …\no.fr {\n}\n")
    return root, etc


def run(tmp_path: Path, script: str, *args: str, stdin: str | None = None) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "systemctl").write_text(SYSTEMCTL)
    (bin_dir / "systemctl").chmod(0o755)
    (tmp_path / "archives").mkdir(exist_ok=True)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "ORION_ROOT": str(tmp_path / "srv" / "orion"),
        "ORION_ETC": str(tmp_path / "etc"),
        "ORION_ARCHIVE_DIR": str(tmp_path / "archives"),
    }
    done = subprocess.run(
        ["bash", str(DEPLOY / script), *args],
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return f"{done.returncode}\n{done.stdout}{done.stderr}"


def files(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def calls(tmp_path: Path) -> list[str]:
    return (tmp_path / "bin" / "calls").read_text().splitlines()


def test_a_reset_forgets_and_keeps_the_settings(tmp_path: Path) -> None:
    root, _ = installed(tmp_path)
    out = run(tmp_path, "reset.sh", "--yes")
    assert out.startswith("0\n")
    assert files(root) == {
        "data/modules/meteo/module.py",
        "data/chatgpt/auth.json",
        "data/console.key",
        "guardian/guardian.sqlite3",
        "guardian/logins/doctolib.json",
        "guardian/secrets.env",
        "guardian/admin.token",
        "backups/core/journal-1.sqlite3",
        "config/orion.toml",
        "app/pyproject.toml",
    }
    # Stopped while it forgets, started again; the update timer it found on is on again.
    assert calls(tmp_path).index("stop orion-core orion-guardian") < calls(tmp_path).index(
        "start orion-guardian orion-core"
    )
    assert calls(tmp_path)[-1] == "start orion-update.timer"
    [archive] = (tmp_path / "archives").iterdir()
    assert archive.stat().st_mode & 0o777 == 0o600
    with tarfile.open(archive) as kept:
        names = kept.getnames()
    assert any(n.endswith("data/journal.sqlite3") for n in names)
    assert not any(n.endswith("console.key") for n in names)


def test_a_full_reset_forgets_the_guardian_and_the_backups_too(tmp_path: Path) -> None:
    root, _ = installed(tmp_path)
    out = run(tmp_path, "reset.sh", "--guardian", "--backups", "--no-archive", "--yes")
    assert out.startswith("0\n")
    assert files(root) == {
        "data/modules/meteo/module.py",
        "data/chatgpt/auth.json",
        "data/console.key",
        "guardian/secrets.env",
        "guardian/admin.token",
        "config/orion.toml",
        "app/pyproject.toml",
    }
    assert not list((tmp_path / "archives").iterdir())


def test_nothing_is_done_without_a_yes(tmp_path: Path) -> None:
    root, _ = installed(tmp_path)
    before = files(root)
    assert "add --yes" in run(tmp_path, "reset.sh")  # no terminal to ask
    assert "add --yes" in run(tmp_path, "uninstall.sh")
    assert "unknown option" in run(tmp_path, "reset.sh", "--everything")
    assert files(root) == before


def test_uninstalling_removes_orion_and_keeps_an_archive(tmp_path: Path) -> None:
    root, etc = installed(tmp_path)
    out = run(tmp_path, "uninstall.sh", "--yes")
    assert out.startswith("0\n") and "Orion is uninstalled" in out
    assert not root.exists() and not (etc / "orion").exists()
    assert not list((etc / "systemd" / "system").iterdir())
    assert "disable --now -q orion-core.service" in calls(tmp_path)
    assert "file_server" in (etc / "caddy" / "Caddyfile").read_text()  # Caddy's own page again
    [archive] = (tmp_path / "archives").iterdir()
    assert archive.stat().st_mode & 0o777 == 0o600
    with tarfile.open(archive) as kept:
        names = kept.getnames()
    assert any(n.endswith("data/journal.sqlite3") for n in names)
    assert any(n.endswith("orion/core.env") for n in names)
    assert not any("/app/" in n for n in names)  # the code is not data
    assert "is not installed" in run(tmp_path, "uninstall.sh", "--yes")


def test_a_purge_keeps_nothing(tmp_path: Path) -> None:
    root, _ = installed(tmp_path)
    assert run(tmp_path, "uninstall.sh", "--purge", "--yes").startswith("0\n")
    assert not root.exists() and not list((tmp_path / "archives").iterdir())


def test_an_older_install_gets_the_app_and_keeps_the_rest(tmp_path: Path) -> None:
    config = tmp_path / "orion.toml"
    template = (DEPLOY / "orion.production.toml").read_text()
    before = template[: template.index("[modules.mobile]")].replace("@DOMAIN@", "1-2-3-4.sslip.io")
    config.write_text(before)

    def add() -> str:
        return subprocess.run(
            [DEPLOY / "add-app", config, "1-2-3-4.sslip.io"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    assert "added" in add()
    after = config.read_text()
    assert after.startswith(before)
    loaded = parse_config(tomllib.loads(after), config)
    mobile = MobileSettings.model_validate(loaded.modules["mobile"])
    assert mobile.enabled
    assert mobile.public_url == "https://1-2-3-4.sslip.io"

    assert add() == ""  # already there: nothing changes
    assert config.read_text() == after
