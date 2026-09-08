"""Tests for tool guidance declared in manifests."""

from pathlib import Path

import pytest

from tool_manager import ToolGuidance, ToolManager, ToolManifest, ToolPackageError


def test_guidance_from_mapping_normalizes_and_serializes():
    guidance = ToolGuidance.from_mapping(
        {
            "summary": "  Cherche des sources.  ",
            "instructions": "  Vérifie les informations avant de répondre.\n",
            "constraints": ["  Cite les sources. ", "Ne fabrique pas de résultats."],
        }
    )

    assert guidance == ToolGuidance(
        summary="Cherche des sources.",
        instructions="Vérifie les informations avant de répondre.",
        constraints=("Cite les sources.", "Ne fabrique pas de résultats."),
    )
    assert guidance.to_dict() == {
        "summary": "Cherche des sources.",
        "instructions": "Vérifie les informations avant de répondre.",
        "constraints": ["Cite les sources.", "Ne fabrique pas de résultats."],
    }


def test_manifest_parses_guidance_from_toml_and_serializes_it(tmp_path: Path):
    manifest_path = tmp_path / "tool.toml"
    manifest_path.write_text(
        """
id = "example.guided"
name = "Guided example"
version = "1.2.3"

[guidance]
summary = "Une aide courte"
instructions = "Suit la procédure déclarée."
constraints = ["Reste dans le périmètre."]
""".strip(),
        encoding="utf-8",
    )

    manifest = ToolManifest.from_file(manifest_path)

    assert manifest.guidance.summary == "Une aide courte"
    assert manifest.guidance.instructions == "Suit la procédure déclarée."
    assert manifest.guidance.constraints == ("Reste dans le périmètre.",)
    assert manifest.to_dict()["guidance"] == {
        "summary": "Une aide courte",
        "instructions": "Suit la procédure déclarée.",
        "constraints": ["Reste dans le périmètre."],
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("summary", "x" * 501, "summary"),
        ("instructions", "x" * 4001, "instructions"),
        ("constraints", ["x"] * 21, "constraints"),
        ("constraints", ["x" * 501], "constraint"),
    ],
)
def test_guidance_limits_are_rejected(field: str, value: object, message: str):
    guidance = ToolGuidance(
        summary=value if field == "summary" else "",
        instructions=value if field == "instructions" else "",
        constraints=tuple(value) if field == "constraints" else (),
    )

    with pytest.raises(ToolPackageError, match=message):
        guidance.validate()


@pytest.mark.parametrize(
    "value",
    [
        {"summary": 123},
        {"instructions": None},
        {"constraints": "not-a-list"},
        {"constraints": ["ok", 123]},
    ],
)
def test_guidance_types_are_rejected(value: object):
    with pytest.raises(TypeError):
        ToolGuidance.from_mapping(value)  # type: ignore[arg-type]


class _DummyClient:
    def register_tool(self, *args, **kwargs):
        return None


def _write_tool_package(install_dir: Path, tool_id: str, summary: str) -> None:
    package_dir = install_dir / tool_id
    package_dir.mkdir(parents=True)
    (package_dir / "tool.py").write_text(
        "def register(client):\n    return None\n", encoding="utf-8"
    )
    (package_dir / "tool.toml").write_text(
        f'id = "{tool_id}"\nname = "{tool_id}"\nversion = "1.0.0"\n'
        f'[guidance]\nsummary = "{summary}"\n',
        encoding="utf-8",
    )


def test_loaded_guidance_contains_only_loaded_tools(tmp_path: Path):
    install_dir = tmp_path / "tools"
    _write_tool_package(install_dir, "example.alpha", "Alpha guidance")
    _write_tool_package(install_dir, "example.beta", "Beta guidance")
    manager = ToolManager(
        install_dir=install_dir,
        root_dir=tmp_path,
        config={"enabled": ["example.alpha"]},
    )

    loaded = manager.load_all(_DummyClient())
    guidance = manager.loaded_guidance()

    assert [manifest.id for manifest in loaded] == ["example.alpha"]
    assert set(guidance) == {"example.alpha"}
    assert guidance["example.alpha"].summary == "Alpha guidance"
    assert set(manager.loaded_guidance({"example.alpha", "example.beta"})) == {"example.alpha"}

