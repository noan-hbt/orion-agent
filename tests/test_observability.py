import json

from observability import doctor, readiness


def _write_config(tmp_path, *, core="ORION_CORE.md", reflection_enabled=False):
    config = tmp_path / "orion.toml"
    config.write_text(
        "\n".join([
            "[llm]",
            'api_key_env = "ORION_TEST_API_KEY"',
            'model = "test/model"',
            "",
            "[prompt]",
            f'core_path = "{core}"',
            "",
            "[reflection]",
            f"enabled = {'true' if reflection_enabled else 'false'}",
            'prompt_path = "REFLECTION_CORE.md"',
            "",
        ]),
        encoding="utf-8",
    )
    return config


def test_readiness_reports_missing_llm_api_key_without_exposing_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("ORION_TEST_API_KEY", raising=False)
    (tmp_path / "ORION_CORE.md").write_text("core", encoding="utf-8")
    config = _write_config(tmp_path)

    assert doctor(config)["ok"] is True
    result = readiness(config)

    assert result["ready"] is False
    assert result["failures"] == [{
        "code": "missing_env",
        "name": "ORION_TEST_API_KEY",
        "component": "llm",
    }]
    assert "secret-value" not in json.dumps(result)


def test_readiness_reports_missing_core_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ORION_TEST_API_KEY", "secret-value")
    config = _write_config(tmp_path, core="missing-core.md")

    result = readiness(config)

    assert result["ready"] is False
    assert result["failures"] == [{
        "code": "missing_file",
        "path": str(tmp_path / "missing-core.md"),
        "component": "prompt_core",
    }]
    assert "secret-value" not in json.dumps(result)


def test_readiness_reports_missing_reflection_file_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("ORION_TEST_API_KEY", "secret-value")
    (tmp_path / "ORION_CORE.md").write_text("core", encoding="utf-8")
    config = _write_config(tmp_path, reflection_enabled=True)

    result = readiness(config)

    assert result["ready"] is False
    assert result["failures"] == [{
        "code": "missing_file",
        "path": str(tmp_path / "REFLECTION_CORE.md"),
        "component": "reflection",
    }]
    assert "secret-value" not in json.dumps(result)
