import subprocess
import sys
from pathlib import Path

import pytest

from context_os import ThreadStateStore
from orion_config import OrionConfig
from orion_install import install


def _install(tmp_path: Path, **kwargs):
    config = tmp_path / "orion.toml"
    env = tmp_path / ".env"
    install(
        config_path=config,
        env_path=env,
        model=kwargs.pop("model", "test/model"),
        api_key=kwargs.pop("api_key", "key"),
        channels=kwargs.pop("channels", ["cli"]),
        memory=kwargs.pop("memory", False),
        secrets=kwargs.pop("secrets", {}),
        **kwargs,
    )
    return config, env


def test_fresh_install_generates_config_and_env(tmp_path):
    config, env = _install(tmp_path, channels=["telegram", "web"] , secrets={"TELEGRAM_BOT_TOKEN": "abc", "ORION_WEBHOOK_TOKEN": "xyz"})
    text = config.read_text(encoding="utf-8")
    assert "api_key =" not in text
    assert 'model = "test/model"' in text
    assert 'enabled = ["telegram", "web"]' in text
    assert 'token_env = "TELEGRAM_BOT_TOKEN"' in text
    assert "OPENROUTER_API_KEY=key" in env.read_text(encoding="utf-8")


def test_rerun_requires_force_and_force_keeps_numbered_backups(tmp_path):
    config, _ = _install(tmp_path)
    with pytest.raises(FileExistsError):
        _install(tmp_path)
    _install(tmp_path, force=True, model="second/model")
    _install(tmp_path, force=True, model="third/model")
    assert (tmp_path / "orion.toml.backup").exists()
    assert (tmp_path / "orion.toml.backup.1").exists()
    assert 'model = "third/model"' in config.read_text(encoding="utf-8")


def test_nested_env_path_and_complex_secrets_are_preserved(tmp_path):
    config = tmp_path / "nested" / "cfg" / "orion.toml"
    env = tmp_path / "nested" / "secrets" / ".env"
    secret = 'p@ss=word # "quoted" \\ slash; $HOME'
    install(config_path=config, env_path=env, model="m", api_key="a", channels=["discord"], memory=False, secrets={"DISCORD_WEBHOOK_URL": secret, "TAVILY_API_KEY": "x=y"})
    lines = env.read_text(encoding="utf-8").splitlines()
    assert f"DISCORD_WEBHOOK_URL={secret}" in lines
    assert "TAVILY_API_KEY=x=y" in lines


def test_generated_config_round_trips_through_config_loader(tmp_path, monkeypatch):
    config_path, _ = _install(tmp_path, channels=["email"], memory=True, compactor_model="compact/m", memory_model="memory/m", reflection_model="reflect/m")
    # Loader expands environment values; installation itself stores only names.
    monkeypatch.chdir(tmp_path)
    parsed = OrionConfig.from_file(config_path)
    assert parsed.llm.model == "test/model"
    assert parsed.memory.enabled is True
    assert parsed.context.compactor_model == "compact/m"


def test_context_os_state_is_versioned_and_persistent(tmp_path):
    path = tmp_path / "state" / "thread.json"
    store = ThreadStateStore(path, thread_id="t-1")
    first = store.update(user="alice", nested={"ok": True})
    assert first.version == 1
    restored = ThreadStateStore(path, thread_id="ignored")
    assert restored.get().to_dict()["user"] == "alice"
    assert restored.get().thread_id == "t-1"
    with pytest.raises(ValueError):
        restored.update(expected_version=0, x=1)


def test_cli_set_secret_accepts_equals_and_writes_nested_paths(tmp_path):
    config = tmp_path / "a" / "orion.toml"
    env = tmp_path / "b" / ".env"
    result = subprocess.run([sys.executable, "orion_install.py", "--config", str(config), "--env", str(env), "--model", "m", "--api-key", "k", "--channels", "web", "--set-secret", "ORION_WEBHOOK_TOKEN=a=b=c"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "ORION_WEBHOOK_TOKEN=a=b=c" in env.read_text(encoding="utf-8")


def test_build_wheel_and_sdist_contain_installer(tmp_path):
    result = subprocess.run([sys.executable, "-m", "build", "--wheel", "--sdist", "--outdir", str(tmp_path)], capture_output=True, text=True)
    if result.returncode != 0 and "No module named build" in result.stderr:
        pytest.skip("build package is not installed")
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.glob("*.whl"))
    assert list(tmp_path.glob("*.tar.gz"))
