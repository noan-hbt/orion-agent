"""Packaged text resources (constitution, prompts)."""

from importlib.resources import files


def read_resource(name: str) -> str:
    return files(__package__).joinpath(name).read_text(encoding="utf-8")
