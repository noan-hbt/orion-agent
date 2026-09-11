from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_dependency_files_delegate_to_pyproject_source_of_truth():
    runtime = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    dev = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")

    assert runtime.splitlines()[-1] == "."
    assert dev.splitlines()[-1] == "-e .[dev]"

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert {"build>=1.2", "pytest>=8", "ruff>=0.6,<1"} <= set(
        project["optional-dependencies"]["dev"]
    )


def test_env_example_contains_current_env_contract_without_legacy_knobs():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in (
        "OPENROUTER_API_KEY",
        "TAVILY_API_KEY",
        "GITHUB_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        "DISCORD_WEBHOOK_URL",
        "EMAIL_PASSWORD",
        "ORION_WEBHOOK_TOKEN",
        "ORION_WEBHOOK_REPLY_URL",
        "ORION_GATEWAY_TOKEN",
        "ORION_GATEWAY_HMAC_SECRET",
        "ORION_LOG_JSON",
    ):
        assert f"{name}=" in text

    for legacy in (
        "OPENROUTER_MODEL=",
        "ORION_STATE_PATH=",
        "ORION_VECTOR_MEMORY=",
        "TELEGRAM_USER_ID=",
        "WEB_UI_PORT=",
    ):
        assert legacy not in text
