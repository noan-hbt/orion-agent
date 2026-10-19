"""Typed configuration, loaded from a TOML file.

Secrets never live in the config file: the config only names the environment variable that
holds each secret (`SecretRef`), and the value is read when it is needed.
"""

from __future__ import annotations

import os
import tomllib
from datetime import time
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

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


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = "deepseek/deepseek-v4.1-flash"
    timezone: str = "Europe/Paris"
    max_window_tokens: int = Field(default=120_000, ge=4_000)
    max_steps: int = Field(default=12, ge=1)
    max_tool_calls: int = Field(default=40, ge=0)
    max_cost_usd_per_turn: float = Field(default=0.50, gt=0)
    extractor_model: str = "deepseek/deepseek-v4.1-flash"
    fallback_models: list[str] = Field(
        default_factory=lambda: ["anthropic/claude-haiku-5.5"],
        description="Tried in order when the main or background model fails",
    )
    verifier_model: str = Field(
        default="anthropic/claude-haiku-5.5",
        description="Checks proactive messages; must differ from `model`",
    )

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r} (IANA name, e.g. Europe/Paris)") from exc
        return value

    @model_validator(mode="after")
    def _verifier_differs(self) -> AgentConfig:
        if self.verifier_model == self.model:
            raise ValueError(
                "verifier_model must differ from model (a model misses its own errors)"
            )
        return self


class IMessageConfig(BaseModel):
    """iMessage through an Inkbox managed line."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    api_key: SecretRef = SecretRef(env="INKBOX_API_KEY")
    base_url: str = "https://inkbox.ai"
    identity: str = Field(default="", description="Orion's Inkbox agent handle")
    owner_number: str = Field(
        default="", description="Owner's phone number, E.164 (e.g. +33612345678)"
    )
    mode: Literal["poll", "webhook"] = "poll"
    poll_seconds: float = Field(default=2.0, ge=1.0)
    webhook_host: str = "127.0.0.1"
    webhook_port: int = 8787
    webhook_path: str = "/inkbox/webhook"
    signing_key: SecretRef = SecretRef(env="INKBOX_SIGNING_KEY")
    coalesce_seconds: float = Field(default=1.5, ge=0.0)
    approval_timeout_seconds: float = Field(default=900.0, gt=0)


class ChannelsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    imessage: IMessageConfig = IMessageConfig()


class InitiativeConfig(BaseModel):
    """When Orion may speak first."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    digests: list[time] = Field(default_factory=lambda: [time(8, 30), time(13, 0), time(19, 0)])
    quiet_start: time = time(22, 30)
    quiet_end: time = time(8, 0)
    daily_budget: int = Field(default=6, ge=0, description="Unrequested messages per day, max")
    tick_seconds: float = Field(default=30.0, ge=1.0)


class AdaptationConfig(BaseModel):
    """How Orion learns to work with its owner."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    reflect_after_messages: int = Field(default=12, ge=1)
    reflect_daily_after_messages: int = Field(default=3, ge=1)
    tick_seconds: float = Field(default=600.0, ge=10.0)


class VoiceConfig(BaseModel):
    """Phone calls through Inkbox Voice AI, on the same line as iMessage."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    voice: str | None = Field(default=None, description="Inkbox voice name; None: default")
    origination: Literal[
        "shared_imessage_number", "dedicated_imessage_number", "dedicated_number"
    ] = "shared_imessage_number"
    brief_seconds: float = Field(default=300.0, ge=30.0)
    calls_poll_seconds: float = Field(default=30.0, ge=5.0)


class DelegationConfig(BaseModel):
    """Long tasks handed to the official `claude` CLI (logged in with `claude auth login`)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    binary: str = "claude"
    model: str | None = None
    timeout_seconds: float = Field(default=1800.0, gt=0)
    max_budget_usd: float = Field(default=2.0, gt=0)
    allow_bash: bool = Field(
        default=False, description="Let the agent run commands; each job then needs approval"
    )


class OrionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance: str = Field(
        default="orion-main",
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        description="Stable id of this Orion instance; used as the HLC node id",
    )
    data_dir: Path = Path("data")
    logging: LoggingConfig = LoggingConfig()
    agent: AgentConfig = AgentConfig()
    providers: ProvidersConfig = ProvidersConfig()
    channels: ChannelsConfig = ChannelsConfig()
    initiative: InitiativeConfig = InitiativeConfig()
    adaptation: AdaptationConfig = AdaptationConfig()
    delegation: DelegationConfig = DelegationConfig()
    voice: VoiceConfig = VoiceConfig()
    modules: dict[str, dict[str, Any]] = Field(
        default_factory=dict[str, dict[str, Any]],
        description="Third-party modules' [modules.<name>] sections, validated by each module",
    )

    @property
    def journal_path(self) -> Path:
        return self.data_dir / "journal.sqlite3"

    @property
    def memory_path(self) -> Path:
        """Rebuildable projection: safe to delete, it is replayed from the journal."""
        return self.data_dir / "memory.sqlite3"

    @property
    def workspaces_path(self) -> Path:
        """One folder per delegated job."""
        return self.data_dir / "workspaces"


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
