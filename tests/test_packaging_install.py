"""Smoke checks for the source distribution and wheel contents."""

from pathlib import Path
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
    assert "orion_tools.py" in names
    assert "tool_packages/terminal/tool.py" in names
    assert "tool_packages/terminal/tool.toml" in names
    assert "tool_packages/web/tool.py" in names
    assert "tool_packages/web/tool.toml" in names

    sdist = next(dist.glob("*.tar.gz"))
    with zipfile.ZipFile(sdist) if sdist.suffix == ".zip" else _tar(sdist) as archive:
        names = set(archive.getnames())
    prefix = next(name.split("/")[0] for name in names if name.endswith("/pyproject.toml"))
    assert f"{prefix}/tool_packages/terminal/tool.toml" in names
    assert f"{prefix}/tool_packages/web/tool.toml" in names


def _tar(path):
    import tarfile

    return tarfile.open(path)
