import subprocess
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values

from context_os import ThreadStateStore
from orion_config import OrionConfig
from orion_install import install
from tool_manager import ToolManager


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
    assert 'bootstrap_pairing_secret_env = "TELEGRAM_PAIRING_SECRET"' in text
    assert 'provider = "auto"' in text
    parsed = OrionConfig.from_file(config)
    assert parsed.tools.enabled == []
    assert parsed.subagents.enabled is False
    assert parsed.subagents.default_tools == []
    assert parsed.scheduler.enabled is False
    assert parsed.tasks.enabled is False
    assert parsed.teams.enabled is False
    assert "TELEGRAM_PAIRING_SECRET=" in env.read_text(encoding="utf-8")
    assert "OPENROUTER_API_KEY=key" in env.read_text(encoding="utf-8")
    assert (tmp_path / "ORION_CORE.md").is_file()
    assert (tmp_path / "REFLECTION_CORE.md").is_file()


def test_web_auto_provider_resolves_without_starting_browser(monkeypatch):
    config = OrionConfig.from_mapping(
        {
            "tools": {
                "web": {
                    "provider": "auto",
                    "api_key_env": "ORION_TEST_TAVILY",
                }
            }
        }
    )

    monkeypatch.delenv("ORION_TEST_TAVILY", raising=False)
    assert config._effective_tool_settings()["web"]["provider"] == "public"

    monkeypatch.setenv("ORION_TEST_TAVILY", "test-key")
    assert config._effective_tool_settings()["web"]["provider"] == "tavily"


def test_explicit_web_browser_provider_is_not_rewritten(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    config = OrionConfig.from_mapping(
        {"tools": {"web": {"provider": "browser"}}}
    )

    assert config._effective_tool_settings()["web"]["provider"] == "browser"


def test_fresh_telegram_install_generates_pairing_secret_without_toml_leak(tmp_path, capsys):
    config, env = _install(
        tmp_path,
        channels=["telegram"],
        secrets={"TELEGRAM_BOT_TOKEN": "bot-token"},
    )
    values = dotenv_values(env)
    pairing_secret = values["TELEGRAM_PAIRING_SECRET"]
    assert pairing_secret
    assert len(pairing_secret) >= 32
    config_text = config.read_text(encoding="utf-8")
    assert pairing_secret not in config_text
    assert 'bootstrap_pairing_secret_env = "TELEGRAM_PAIRING_SECRET"' in config_text
    output = capsys.readouterr().out
    assert output.count(pairing_secret) == 1
    assert f"/pair {pairing_secret}" in output


def test_force_rerun_reuses_existing_pairing_secret_without_reprinting(tmp_path, capsys):
    config, env = _install(
        tmp_path,
        channels=["telegram"],
        secrets={"TELEGRAM_BOT_TOKEN": "bot-token"},
    )
    first = dotenv_values(env)["TELEGRAM_PAIRING_SECRET"]
    capsys.readouterr()

    _install(
        tmp_path,
        channels=["telegram"],
        secrets={"TELEGRAM_BOT_TOKEN": "bot-token"},
        force=True,
    )

    assert dotenv_values(env)["TELEGRAM_PAIRING_SECRET"] == first
    assert first not in config.read_text(encoding="utf-8")
    assert first not in capsys.readouterr().out


def test_force_rerun_does_not_overwrite_user_edited_prompt_resources(tmp_path):
    _install(tmp_path)
    core = tmp_path / "ORION_CORE.md"
    reflection = tmp_path / "REFLECTION_CORE.md"
    core.write_text("custom core\n", encoding="utf-8")
    reflection.write_text("custom reflection\n", encoding="utf-8")

    _install(tmp_path, force=True)

    assert core.read_text(encoding="utf-8") == "custom core\n"
    assert reflection.read_text(encoding="utf-8") == "custom reflection\n"


def test_rerun_requires_force_and_force_keeps_numbered_backups(tmp_path):
    config, _ = _install(tmp_path)
    with pytest.raises(FileExistsError):
        _install(tmp_path)
    _install(tmp_path, force=True, model="second/model")
    _install(tmp_path, force=True, model="third/model")
    assert (tmp_path / "orion.toml.backup").exists()
    assert (tmp_path / "orion.toml.backup.1").exists()
    assert 'model = "third/model"' in config.read_text(encoding="utf-8")


def test_force_rerun_preserves_explicit_tool_activation(tmp_path):
    config, _ = _install(tmp_path)
    ToolManager._update_toml_table(
        config,
        "tools",
        {"enabled": ["orion.web", "example.custom"], "disabled": []},
    )

    _install(tmp_path, force=True)

    parsed = OrionConfig.from_file(config)
    assert parsed.tools.enabled == ["orion.web", "example.custom"]


def test_force_rerun_preserves_explicit_tool_disable_over_builtin_default(tmp_path):
    config, _ = _install(tmp_path)
    ToolManager._update_toml_table(
        config,
        "tools",
        {"enabled": ["orion.web"], "disabled": ["orion.files"]},
    )

    _install(tmp_path, force=True)

    parsed = OrionConfig.from_file(config)
    assert parsed.tools.enabled == ["orion.web"]
    assert parsed.tools.disabled == ["orion.files"]


def test_force_rerun_migrates_legacy_enabled_services_to_bundled_modules(tmp_path):
    config, _ = _install(tmp_path)
    ToolManager._update_toml_table(config, "subagents", {"enabled": True})
    ToolManager._update_toml_table(config, "scheduler", {"enabled": True})
    ToolManager._update_toml_table(config, "teams", {"enabled": True})

    _install(tmp_path, force=True)

    parsed = OrionConfig.from_file(config)
    assert parsed.subagents.enabled is False
    assert parsed.scheduler.enabled is False
    assert parsed.teams.enabled is False
    assert parsed.tools.enabled == ["orion.subagents", "orion.tasks", "orion.team"]


def test_nested_env_path_and_complex_secrets_are_preserved(tmp_path):
    config = tmp_path / "nested" / "cfg" / "orion.toml"
    env = tmp_path / "nested" / "secrets" / ".env"
    secret = 'p@ss=word # "quoted" \\ slash; $HOME'
    install(config_path=config, env_path=env, model="m", api_key="a", channels=["discord"], memory=False, secrets={"DISCORD_WEBHOOK_URL": secret, "TAVILY_API_KEY": "x=y"})
    loaded = dotenv_values(env)
    assert loaded["DISCORD_WEBHOOK_URL"] == secret
    assert loaded["TAVILY_API_KEY"] == "x=y"


def test_generated_config_round_trips_through_config_loader(tmp_path, monkeypatch):
    config_path, _ = _install(tmp_path, channels=["email"], memory=True, compactor_model="compact/m", memory_model="memory/m", reflection_model="reflect/m")
    # Loader expands environment values; installation itself stores only names.
    monkeypatch.chdir(tmp_path)
    parsed = OrionConfig.from_file(config_path)
    assert parsed.llm.model == "test/model"
    assert parsed.memory.enabled is True
    assert parsed.context.compactor_model == "compact/m"
    assert parsed.events.durable_path == "data/events.sqlite3"
    assert parsed.runtime.durable_path == "data/events.sqlite3"
    assert parsed.path(parsed.events.durable_path) == str(tmp_path / "data" / "events.sqlite3")
    assert parsed.path(parsed.runtime.durable_path) == str(tmp_path / "data" / "events.sqlite3")


def test_legacy_config_without_durable_paths_remains_ram_only():
    parsed = OrionConfig.from_mapping({})

    assert parsed.events.durable_path is None
    assert parsed.runtime.durable_path is None


def test_runtime_durable_path_rejects_empty_config_value():
    with pytest.raises(ValueError, match="runtime.durable_path"):
        OrionConfig.from_mapping({"runtime": {"durable_path": ""}})


def test_fresh_email_install_is_fail_closed_without_explicit_allowlist(tmp_path, capsys):
    password = "mail-password-that-must-stay-out-of-toml"
    config, env = _install(
        tmp_path,
        channels=["email"],
        secrets={"EMAIL_PASSWORD": password},
    )

    text = config.read_text(encoding="utf-8")
    assert "allowed_senders = []" in text
    assert password not in text
    assert dotenv_values(env)["EMAIL_PASSWORD"] == password
    parsed = OrionConfig.from_file(config)
    assert parsed.channels.settings["email"]["allowed_senders"] == []
    output = capsys.readouterr().out
    assert "aucun email entrant ne sera accepte" in output
    assert "allowed_senders=[]" in output


def test_fresh_email_install_accepts_explicit_sender_allowlist(tmp_path, capsys):
    config, _ = _install(
        tmp_path,
        channels=["email"],
        secrets={"EMAIL_PASSWORD": "secret"},
        email_allowed_senders=["owner@example.com", "ops@example.org"],
    )

    text = config.read_text(encoding="utf-8")
    assert 'allowed_senders = ["owner@example.com", "ops@example.org"]' in text
    parsed = OrionConfig.from_file(config)
    assert parsed.channels.settings["email"]["allowed_senders"] == [
        "owner@example.com",
        "ops@example.org",
    ]
    assert "aucun email entrant ne sera accepte" not in capsys.readouterr().out


def test_legacy_email_config_without_allowed_senders_remains_parseable():
    parsed = OrionConfig.from_mapping(
        {
            "channels": {
                "enabled": ["email"],
                "email": {
                    "imap_host": "imap.example.com",
                    "smtp_host": "smtp.example.com",
                    "username": "orion@example.com",
                },
            }
        }
    )

    assert "allowed_senders" not in parsed.channels.settings["email"]


def test_config_loads_sibling_dotenv_before_expansion(tmp_path, monkeypatch):
    monkeypatch.delenv("ORION_TEST_MODEL", raising=False)
    config = tmp_path / "orion.toml"
    config.write_text(
        '[llm]\nmodel = "$ORION_TEST_MODEL"\n',
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("ORION_TEST_MODEL=dotenv/model\n", encoding="utf-8")

    parsed = OrionConfig.from_file(config)

    assert parsed.llm.model == "dotenv/model"


def test_process_environment_takes_precedence_over_sibling_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("ORION_TEST_MODEL", "process/model")
    config = tmp_path / "orion.toml"
    config.write_text(
        '[llm]\nmodel = "$ORION_TEST_MODEL"\n',
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("ORION_TEST_MODEL=dotenv/model\n", encoding="utf-8")

    parsed = OrionConfig.from_file(config)

    assert parsed.llm.model == "process/model"


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
    # ``orion_install.py`` is invoked as a script, so the repo root must be the
    # working directory; tests no longer inherit the ambient cwd.
    result = subprocess.run(
        [sys.executable, "orion_install.py", "--config", str(config), "--env", str(env), "--model", "m", "--api-key", "k", "--channels", "web", "--set-secret", "ORION_WEBHOOK_TOKEN=a=b=c"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert result.returncode == 0, result.stderr
    assert dotenv_values(env)["ORION_WEBHOOK_TOKEN"] == "a=b=c"


def test_build_wheel_and_sdist_contain_installer(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--sdist", "--outdir", str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    if result.returncode != 0 and "No module named build" in result.stderr:
        pytest.skip("build package is not installed")
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.glob("*.whl"))
    assert list(tmp_path.glob("*.tar.gz"))
