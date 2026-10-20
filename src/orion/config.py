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
    sort: Literal["throughput", "latency", "price"] | None = Field(
        default="throughput",
        description="Which host serves the model: the fastest by default (a slow one made "
        "each step 3 to 5 times longer)",
    )


class ChatGPTConfig(BaseModel):
    """The owner's ChatGPT plan (D62): models named `chatgpt:<slug>` are served through it, after
    `orion chatgpt login`."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    reasoning_effort: Literal["low", "medium", "high"] | None = Field(
        default="low",
        description="How long the models think when a call asks nothing; none: the model's own",
    )


class ProvidersConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    openrouter: OpenRouterConfig = OpenRouterConfig()
    chatgpt: ChatGPTConfig = ChatGPTConfig()


STRONG_MODEL = "anthropic/claude-opus-5.5"
VISION_MODEL = "google/gemini-3.8-flash"
IMAGE_MODEL = "google/gemini-3.1-flash-image"


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = "deepseek/deepseek-v4.1-flash"
    timezone: str = "Europe/Paris"
    max_window_tokens: int = Field(default=120_000, ge=4_000)
    max_steps: int = Field(
        default=40, ge=1, description="Model calls per turn; browsing a site takes one per page"
    )
    max_tool_calls: int = Field(default=80, ge=0)
    max_cost_usd_per_turn: float = Field(default=0.50, gt=0)
    prune_idle_minutes: int = Field(
        default=10,
        ge=0,
        description="After this long without messages (the model cache has expired anyway), "
        "old bulky tool results are shown shortened; 0 never does",
    )
    untrusted_idle_minutes: int = Field(
        default=120,
        ge=0,
        description="After this long without messages, a window holding untrusted content "
        "starts afresh (memory carries what matters); 0 never does",
    )
    extractor_model: str = "deepseek/deepseek-v4.1-flash"
    fallback_models: list[str] = Field(
        default_factory=lambda: ["anthropic/claude-haiku-5.5"],
        description="Tried in order when the main or background model fails",
    )
    verifier_model: str = Field(
        default="anthropic/claude-haiku-5.5",
        description="Checks proactive messages; must differ from `model`",
    )
    strong_model: str = Field(
        default=STRONG_MODEL,
        description="The stronger model: consulted by Orion (expert), and used for the night's "
        "thinking and the weekly chronicle unless their section names another",
    )
    vision_model: str = Field(
        default=VISION_MODEL,
        description="Looks at the photos the owner sends (reads HEIC, the iPhone's format)",
    )
    image_model: str = Field(
        default=IMAGE_MODEL, description="Makes the pictures the owner asks for"
    )
    summary_model: str | None = Field(
        default=None, description="Summarizes what a rebase drops; default: extractor_model"
    )
    reflection_model: str | None = Field(
        default=None,
        description="Learns the owner's playbook and tidies memory at night; "
        "default: extractor_model",
    )

    def models(self) -> set[str]:
        """Every model the agent section names."""
        return {
            self.model,
            self.extractor_model,
            self.verifier_model,
            self.strong_model,
            self.vision_model,
            self.image_model,
            self.summary_model or self.extractor_model,
            self.reflection_model or self.extractor_model,
        }

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


class MemoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_names: list[str] = Field(
        default_factory=list[str],
        description="The owner's names and nicknames: kept out of the people in their life",
    )

    embedding_model: str | None = Field(
        default="openai/text-embedding-3-small",
        description="Embeddings for semantic search, through the main provider; None: words only",
    )


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
    poll_seconds: float = Field(default=1.0, ge=0.5)
    webhook_host: str = "127.0.0.1"
    webhook_port: int = 8787
    webhook_path: str = "/inkbox/webhook"
    signing_key: SecretRef = SecretRef(env="INKBOX_SIGNING_KEY")
    coalesce_seconds: float = Field(
        default=0.5,
        ge=0.0,
        description="Wait for a burst before starting; later texts still join the running turn",
    )
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
    live: bool = Field(
        default=False,
        description="Calls with the owner are led by Orion itself over Inkbox's media stream "
        "(live memory and tools); calls to others stay with the hosted voice",
    )
    live_url: str = Field(
        default="", description="Public wss:// address of the stream, e.g. wss://host/voice/live"
    )
    live_host: str = "127.0.0.1"
    live_port: int = 8791


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


class GuardianConfig(BaseModel):
    """The guardian process (D28): holds the secrets and checks every effect on the world."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description="Run split: `orion guardian` holds the secrets, `orion serve` asks it",
    )
    socket: Path = Field(
        default=Path("run/guardian.sock"), description="Unix socket shared by both processes"
    )
    data_dir: Path = Field(
        default=Path("guardian"), description="The guardian's own ledger; only it reads it"
    )
    approval_ttl_seconds: float = Field(default=1800.0, gt=0)
    max_sends_per_minute: int = Field(
        default=30, ge=1, description="Messages to the owner, at most (a runaway loop stops)"
    )
    core_silence_alert_seconds: float = Field(
        default=300.0,
        ge=0.0,
        description="The guardian texts the owner when the core has been silent this long "
        "(the core can't say it is down); 0: never",
    )
    heartbeat_seconds: float = Field(default=60.0, gt=0, description="The core's heartbeat")
    autonomable: list[str] = Field(
        default_factory=lambda: ["calendar_add", "configure_change"],
        description="Actions the owner may let Orion do alone, once earned (never calls or "
        "mail to strangers unless listed here)",
    )
    offer_autonomy_after: int = Field(
        default=5, ge=2, description="Approvals in a row before Orion offers to stop asking"
    )
    max_autonomous_per_day: int = Field(default=10, ge=1)


class FederationConfig(BaseModel):
    """Talking with other people's Orion (D29, M7). Needs the guardian process."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    owner_name: str = Field(default="", description="How other Orions name the owner")
    key: SecretRef = Field(
        default=SecretRef(env="ORION_FEDERATION_KEY"),
        description="This instance's Ed25519 private key (`orion federation keygen`)",
    )
    public_url: str = Field(
        default="", description="HTTPS address other Orions reach (a reverse proxy in front)"
    )
    listen_host: str = "127.0.0.1"
    listen_port: int = 8788
    invite_days: int = Field(default=7, ge=1, le=60)
    max_inbound_per_hour: int = Field(default=60, ge=1, description="Per peer")
    meeting_start: time = Field(default=time(9, 0), description="Earliest meeting start")
    meeting_end: time = Field(default=time(19, 0), description="Latest meeting end")
    weekends: bool = False


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
    memory: MemoryConfig = MemoryConfig()
    channels: ChannelsConfig = ChannelsConfig()
    initiative: InitiativeConfig = InitiativeConfig()
    adaptation: AdaptationConfig = AdaptationConfig()
    delegation: DelegationConfig = DelegationConfig()
    voice: VoiceConfig = VoiceConfig()
    guardian: GuardianConfig = GuardianConfig()
    federation: FederationConfig = FederationConfig()
    modules: dict[str, dict[str, Any]] = Field(
        default_factory=dict[str, dict[str, Any]],
        description="Third-party modules' [modules.<name>] sections, validated by each module",
    )

    source: Path | None = Field(default=None, exclude=True)
    """The orion.toml this was read from (None: defaults); Orion edits it to configure itself."""

    @property
    def journal_path(self) -> Path:
        return self.data_dir / "journal.sqlite3"

    @property
    def memory_path(self) -> Path:
        """Rebuildable projection: safe to delete, it is replayed from the journal."""
        return self.data_dir / "memory.sqlite3"

    @property
    def embeddings_path(self) -> Path:
        """A cache: safe to delete, recomputed on demand."""
        return self.data_dir / "embeddings.sqlite3"

    @property
    def chatgpt_path(self) -> Path:
        """Sign in with ChatGPT credentials (owner-only, rotating; never backed up)."""
        return self.data_dir / "chatgpt"

    @property
    def guardian_ledger_path(self) -> Path:
        return self.guardian.data_dir / "guardian.sqlite3"

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
    return config.model_copy(
        update={
            "source": target.resolve(),
            "data_dir": _beside(target, config.data_dir),
            "guardian": config.guardian.model_copy(
                update={
                    "socket": _beside(target, config.guardian.socket),
                    "data_dir": _beside(target, config.guardian.data_dir),
                }
            ),
        }
    )


def _beside(config_file: Path, path: Path) -> Path:
    """Relative paths in orion.toml are relative to the file itself."""
    return path if path.is_absolute() else config_file.parent / path
