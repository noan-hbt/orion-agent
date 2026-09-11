"""Smoke checks for the source distribution and wheel contents."""

from pathlib import Path
import os
import shutil
import subprocess
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def test_build_artifacts_include_flat_modules_and_tool_resources(tmp_path):
    """Build both formats and ensure the installable tool bundles survive."""
    dist = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "--wheel", "--outdir", str(dist)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    wheel = next(dist.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        entry_points_name = next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
        entry_points = archive.read(entry_points_name).decode("utf-8")
    assert "orion_tools.py" in names
    assert "cli_v2.py" in names
    assert "durable_events.py" in names
    assert "tool_policy.py" in names
    assert "tool_packages/terminal/tool.py" in names
    assert "tool_packages/terminal/tool.toml" in names
    assert "tool_packages/web/tool.py" in names
    assert "tool_packages/web/tool.toml" in names
    assert "orion_resources/__init__.py" in names
    assert "orion_resources/ORION_CORE.md" in names
    assert "orion_resources/REFLECTION_CORE.md" in names
    assert not any(name.endswith("/.env") or name == ".env" for name in names)
    assert "orion = orion_run:main" in entry_points
    assert "orion-install = orion_install:main" in entry_points
    assert "orion-tools = orion_tools:main" in entry_points

    sdist = next(dist.glob("*.tar.gz"))
    with zipfile.ZipFile(sdist) if sdist.suffix == ".zip" else _tar(sdist) as archive:
        names = set(archive.getnames())
    prefix = next(name.split("/")[0] for name in names if name.endswith("/pyproject.toml"))
    assert f"{prefix}/tool_packages/terminal/tool.toml" in names
    assert f"{prefix}/tool_packages/web/tool.toml" in names
    assert f"{prefix}/durable_events.py" in names
    assert f"{prefix}/tool_policy.py" in names
    assert f"{prefix}/orion_resources/ORION_CORE.md" in names
    assert f"{prefix}/orion_resources/REFLECTION_CORE.md" in names
    assert not any(name.endswith("/.env") for name in names)


def test_build_uses_canonical_sources_not_repo_build_or_egg_info(tmp_path):
    """Generated repo artifacts must never become packaging source-of-truth."""
    checkout = tmp_path / "checkout"
    shutil.copytree(
        ROOT,
        checkout,
        ignore=shutil.ignore_patterns(
            ".git",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            "__pycache__",
            "dist",
        ),
    )

    # Poison generated copies while leaving canonical source files untouched.
    # A correct setuptools configuration regenerates metadata and packages from
    # the checkout root, so none of these sentinels can reach the artifacts.
    build_lib = checkout / "build" / "lib"
    build_lib.mkdir(parents=True, exist_ok=True)
    build_lib.joinpath("durable_events.py").write_text(
        "REPO_BUILD_SENTINEL = True\n", encoding="utf-8"
    )
    build_lib.joinpath("tool_policy.py").write_text(
        "REPO_BUILD_POLICY_SENTINEL = True\n", encoding="utf-8"
    )
    build_resources = build_lib / "orion_resources"
    build_resources.mkdir(parents=True, exist_ok=True)
    build_resources.joinpath("ORION_CORE.md").write_text(
        "REPO_BUILD_RESOURCE_SENTINEL = True\n", encoding="utf-8"
    )

    repo_egg_info = checkout / "openrouter_agent_client.egg-info"
    repo_egg_info.mkdir(parents=True, exist_ok=True)
    poison_source = checkout / "repo_egginfo_poison.py"
    poison_source.write_text("REPO_EGG_INFO_SENTINEL = True\n", encoding="utf-8")
    repo_egg_info.joinpath("SOURCES.txt").write_text(
        "repo_egginfo_poison.py\nbuild/lib/durable_events.py\n",
        encoding="utf-8",
    )
    repo_egg_info.joinpath("PKG-INFO").write_text(
        "Metadata-Version: 2.1\nName: poisoned-repo-metadata\nVersion: 999\n",
        encoding="utf-8",
    )

    dist = tmp_path / "dist-canonical"
    subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "--wheel", "--outdir", str(dist)],
        cwd=checkout,
        check=True,
        capture_output=True,
        text=True,
    )

    canonical_modules = {
        "durable_events.py": (checkout / "durable_events.py").read_bytes(),
        "tool_policy.py": (checkout / "tool_policy.py").read_bytes(),
    }
    canonical_resources = {
        f"orion_resources/{name}": (checkout / "orion_resources" / name).read_bytes()
        for name in ("ORION_CORE.md", "REFLECTION_CORE.md")
    }
    canonical = {**canonical_modules, **canonical_resources}

    wheel = next(dist.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        wheel_names = set(archive.namelist())
        wheel_payloads = {
            name: archive.read(name) if name in wheel_names else None
            for name in canonical
        }

    sdist = next(dist.glob("*.tar.gz"))
    with _tar(sdist) as archive:
        sdist_names = set(archive.getnames())
        prefix = next(name.split("/")[0] for name in sdist_names if name.endswith("/pyproject.toml"))
        sdist_payloads = {}
        for name in canonical:
            member = f"{prefix}/{name}"
            extracted = archive.extractfile(member) if member in sdist_names else None
            sdist_payloads[name] = extracted.read() if extracted is not None else None

        # setuptools may include freshly generated .egg-info metadata in an
        # sdist. That is valid; what must never survive is the stale repo
        # SOURCES manifest pointing at generated build/ copies.
        sources_name = next(
            (name for name in sdist_names if name.endswith(".egg-info/SOURCES.txt")),
            None,
        )
        sources = archive.extractfile(sources_name) if sources_name is not None else None
        sources_text = sources.read().decode("utf-8") if sources is not None else ""

    violations = []
    if any(name.startswith("build/") or "/build/" in name for name in wheel_names):
        violations.append("wheel contains a build/ path")
    if any(".egg-info/" in name for name in wheel_names):
        violations.append("wheel contains repo-style .egg-info metadata")
    if "repo_egginfo_poison.py" in wheel_names:
        violations.append("wheel trusted stale repo .egg-info/SOURCES.txt")
    for name, expected in canonical.items():
        if wheel_payloads[name] != expected:
            violations.append(f"wheel used a generated/stale copy for {name}")

    if any("/build/" in name or name.endswith("/build") for name in sdist_names):
        violations.append("sdist contains a build/ path")
    if f"{prefix}/repo_egginfo_poison.py" in sdist_names:
        violations.append("sdist trusted stale repo .egg-info/SOURCES.txt")
    for name, expected in canonical.items():
        if sdist_payloads[name] != expected:
            violations.append(f"sdist used a generated/stale copy for {name}")
    if sources_name is None:
        violations.append("sdist did not generate expected .egg-info/SOURCES.txt metadata")
    if "build/lib/" in sources_text.replace("\\", "/"):
        violations.append("generated sdist SOURCES.txt references repo build/lib")
    if "repo_egginfo_poison.py" in sources_text:
        violations.append("generated sdist SOURCES.txt retained stale repo entry")

    assert not violations, "\n" + "\n".join(f"- {item}" for item in violations)


def test_packaged_prompt_sources_match_checkout_canonical_files():
    for name in ("ORION_CORE.md", "REFLECTION_CORE.md"):
        assert (ROOT / "orion_resources" / name).read_text(encoding="utf-8") == (
            ROOT / name
        ).read_text(encoding="utf-8")


def test_installed_wheel_provisions_prompt_resources_without_overwriting(tmp_path):
    dist = tmp_path / "dist"
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(dist.glob("*.whl"))
    target = tmp_path / "site"
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--target", str(target), str(wheel)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    run_dir = tmp_path / "installed-run"
    run_dir.mkdir()
    script = r'''
from pathlib import Path
import sys

site = Path(sys.argv[1])
run_dir = Path(sys.argv[2])
sys.path.insert(0, str(site))

import orion_install as installed_orion_install

assert Path(installed_orion_install.__file__).resolve().is_relative_to(site.resolve())
install = installed_orion_install.install

config = run_dir / "orion.toml"
env = run_dir / ".env"
kwargs = dict(
    config_path=config,
    env_path=env,
    model="test/model",
    compactor_model="test/compact",
    memory_model="test/memory",
    reflection_model="test/reflection",
    api_key="",
    channels=["cli"],
    memory=False,
    secrets={},
)
install(**kwargs)
core = run_dir / "ORION_CORE.md"
reflection = run_dir / "REFLECTION_CORE.md"
assert core.is_file()
assert reflection.is_file()
text = config.read_text(encoding="utf-8")
assert 'core_path = "ORION_CORE.md"' in text
assert 'prompt_path = "REFLECTION_CORE.md"' in text

core.write_text("# user-edited core\n", encoding="utf-8")
install(**kwargs, force=True)
assert core.read_text(encoding="utf-8") == "# user-edited core\n"
assert reflection.is_file()
'''
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-c", script, str(target), str(run_dir)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def _tar(path):
    import tarfile

    return tarfile.open(path)
