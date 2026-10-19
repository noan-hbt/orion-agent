"""Typed configuration, loaded from a TOML file.

Secrets never live in the config file: the config only names the environment variable that
holds each secret (`SecretRef`), and the value is read when it is needed.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr

DEFAULT_CONFIG_PATH = Path("orion.toml")


class SecretRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    env: str = Field(min_length=1, description="Environment variable holding the secret")

    def resolve(self) -> SecretStr:
        value = os.environ.get(self.env)
        if not value:
            raise LookupError(f"secret environment variable {self.env!r} is not set")
        return SecretStr(value)


class LoggingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: str = "INFO"
    json_output: bool = True


class OpenRouterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: SecretRef = SecretRef(env="OPENROUTER_API_KEY")
    base_url: str = "https://openrouter.ai/api/v1"


class ProvidersConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    openrouter: OpenRouterConfig = OpenRouterConfig()


class OrionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance: str = Field(
        default="orion-main",
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        description="Stable id of this Orion instance; used as the HLC node id",
    )
    data_dir: Path = Path("data")
    logging: LoggingConfig = LoggingConfig()
    providers: ProvidersConfig = ProvidersConfig()

    @property
    def journal_path(self) -> Path:
        return self.data_dir / "journal.sqlite3"


def load_config(path: Path | None = None) -> OrionConfig:
    """Load config from `path`, or `orion.toml` if it exists, or fall back to defaults."""
    target = path or DEFAULT_CONFIG_PATH
    if not target.exists():
        if path is not None:
            raise FileNotFoundError(f"config file not found: {target}")
        return OrionConfig()
    with target.open("rb") as f:
        data = tomllib.load(f)
    config = OrionConfig.model_validate(data)
    if not config.data_dir.is_absolute():
        config = config.model_copy(update={"data_dir": target.parent / config.data_dir})
    return config
