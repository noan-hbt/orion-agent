"""Configuration centralisee et bootstrap de l'application Orion.

Le fichier de configuration est TOML. Les secrets restent dans `.env` ; le
champ ``api_key_env`` indique uniquement le nom de la variable a utiliser.
"""

from __future__ import annotations

import os
import math
import inspect
import threading
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from datetime import time as day_time
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dependency is optional at import time
    load_dotenv = None  # type: ignore[assignment]


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"La section [{name}] doit etre un objet TOML.")
    return value


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item) for item in value]
    return value


@dataclass
class LLMConfig:
    api_key_env: str = "OPENROUTER_API_KEY"
    model: str = "openai/gpt-4o-mini"
    base_url: str = "https://openrouter.ai/api/v1"
    timeout: float = 60.0
    max_retries: int = 2
    retry_backoff: float = 0.5
    # Upper bound for any single retry wait, including a provider-supplied
    # ``Retry-After`` header.  Without it a ``Retry-After: 3600`` blocked a
    # worker thread for an hour with no cancellation path.
    retry_max_delay: float = 60.0
    # Hard wall-clock ceiling for one HTTP attempt (all phases, DNS included).
    # Derived from ``timeout`` when left at zero so existing configs behave.
    request_deadline: float = 150.0
    site_url: str | None = None
    site_name: str | None = None
    default_params: dict[str, Any] = field(default_factory=dict)


@dataclass
class EventConfig:
    workers: int = 1
    queue_size: int = 0
    default_max_attempts: int = 3
    retry_delay: float = 1.0
    retry_backoff: float = 2.0
    dedupe_ttl: float = 86400.0
    dedupe_max_entries: int = 10000
    durable_path: str | None = None


@dataclass
class RuntimeConfig:
    max_turns: int = 12
    wake_queue_size: int = 0
    dedupe_window: float = 86400.0
    parallel_tool_calls: bool = False
    queue_events_during_run: bool = True
    wake_on_subagent_progress: bool = False
    max_deferred_events: int = 10000
    durable_path: str | None = None


@dataclass
class SubAgentConfig:
    """Workers IA persistants, indépendants du runtime principal."""

    enabled: bool = False
    state_path: str = "data/subagents.json"
    workers: int = 3
    default_model: str | None = "deepseek/deepseek-v4-flash-0731"
    default_tools: list[str] = field(default_factory=list)
    default_max_turns: int = 8
    max_context_chars: int = 16000
    max_result_chars: int = 24000
    max_tool_output_chars: int = 12000
    max_session_messages: int = 100
    history_limit: int = 200
    emit_progress_events: bool = False


@dataclass
class TeamConfig:
    """Bus local optionnel pour plusieurs instances Orion."""

    enabled: bool = False
    path: str = "data/teams.sqlite3"
    instance_id: str = "orion"
    team: str = "default"
    poll_interval: float = 1.0
    max_message_chars: int = 12000


@dataclass
class ResponseConfig:
    """Style et garde-fous des réponses envoyées aux utilisateurs."""

    concise: bool = True
    max_chars: int = 1800
    max_sentences: int = 5


@dataclass
class ReflectionConfig:
    """Pre-etape interne avant le premier appel decisionnel du run."""

    enabled: bool = True
    model: str | None = "openai/gpt-4o-mini"
    prompt_path: str = "REFLECTION_CORE.md"
    max_input_chars: int = 12000
    max_output_chars: int = 5000
    temperature: float = 0.7


@dataclass
class SchedulerConfig:
    enabled: bool = False
    poll_interval: float = 1.0
    schedules_path: str = "data/schedules.json"


@dataclass
class PromptConfig:
    core_path: str = "ORION_CORE.md"
    context_path: str = "data/prompt_context.json"
    journal_path: str = "data/conversations.jsonl"
    history_enabled: bool = True
    history_limit: int = 2000
    history_max_chars: int = 140000
    personality: str | None = None
    methodology: str | None = None
    additional: str = ""


@dataclass
class ContextConfig:
    # Versioned context contract.  The legacy fields below remain accepted so
    # existing configurations can be rolled back without a schema migration.
    context_contract_version: str = "v1"
    context_mode: str = "contract"
    compaction_enabled: bool = True
    llm_compaction_enabled: bool = False
    compactor_model: str = "openai/gpt-4o-mini"
    total_max_chars: int = 200000
    total_max_tokens: int = 48000
    output_reserve_tokens: int = 3000
    compactor_input_chars: int = 30000
    cache_size: int = 64
    task_max_chars: int = 12000
    task_max_tokens: int = 3000
    event_max_chars: int = 10000
    event_max_tokens: int = 3000
    history_max_chars: int = 140000
    policy_max_chars: int = 12000
    policy_max_tokens: int = 3000
    request_max_chars: int = 8000
    request_max_tokens: int = 2000
    profile_memory_max_chars: int = 4000
    profile_memory_max_tokens: int = 1000
    history_max_tokens: int = 30000
    notifications_tools_max_chars: int = 8000
    notifications_tools_max_tokens: int = 2000
    reflection_max_chars: int = 2000
    reflection_max_tokens: int = 500
    history_turn_limit: int = 2000
    reflection_enabled: bool = True
    reflection_format: str = "advisory_json"
    redaction_enabled: bool = False
    token_counter: str = "fallback"


@dataclass
class MemoryConfig:
    enabled: bool = False
    model: str = "openai/gpt-4o-mini"
    run_at: str = "23:00"
    batch_size: int = 20
    min_entries: int = 20
    # Keep these defaults aligned with prompt_context.MemoryMaintenance.  A
    # bounded tail age prevents sub-threshold entries from being stranded and
    # multiple batches let one maintenance pass catch up after a backlog.
    max_batches_per_run: int = 8
    tail_max_age: float = 3600.0
    poll_interval: float = 30.0
    max_input_chars: int = 30000


@dataclass
class ContextOSConfig:
    """Durable Context OS backends (opt-in, preserving legacy defaults)."""
    enabled: bool = False
    registry_path: str = "data/context_registry.sqlite3"
    journal_enabled: bool = False
    journal_path: str = "data/context_journal.sqlite3"
    memory_path: str = "data/memory.sqlite3"
    memory_namespace: str = "default"
    state_path: str = "data/thread_state.json"


@dataclass
class LedgerConfig:
    path: str = "data/action_ledger.sqlite3"


@dataclass
class ApprovalConfig:
    """Per-call human approval policy for privileged tools."""

    enabled: bool = False
    path: str = "data/approvals.sqlite3"


@dataclass
class TaskConfig:
    enabled: bool = False
    path: str = "data/tasks.json"


@dataclass
class ToolsConfig:
    """Extensions Python chargées au démarrage."""

    directory: str = "tools"
    state_path: str = "data/installed_tools.json"
    enabled: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    settings: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class ChannelConfig:
    enabled: list[str] = field(default_factory=list)
    default: str | None = None
    ledger_path: str = "data/communication_ledger.sqlite3"
    settings: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class GatewayConfig:
    """Limites et authentification du gateway HTTP.

    Les valeurs sensibles sont toujours lues depuis une variable
    d'environnement dont le nom est configure ici; aucun secret n'est
    representable dans ce dataclass.
    """

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8080
    path: str = "/webhook"
    max_body_bytes: int = 256 * 1024
    queue_size: int = 1000
    request_timeout: float = 20.0
    replay_window: float = 300.0
    auth_mode: str = "token"
    auth_token_env: str = "ORION_GATEWAY_TOKEN"
    hmac_secret_env: str | None = None
    allowlist: list[str] = field(default_factory=list)
    reply_allowlist: list[str] = field(default_factory=list)
    allow_redirects: bool = False
    ledger_path: str = "data/communication_ledger.sqlite3"


CONFIG_VERSION = 2
TASKS_MODULE_ID = "orion.tasks"
SUBAGENTS_MODULE_ID = "orion.subagents"
TEAM_MODULE_ID = "orion.team"


@dataclass
class OrionConfig:
    """Configuration complete, chargeable depuis ``orion.toml``."""

    name: str = "Orion"
    config_version: int = CONFIG_VERSION
    config_path: Path | None = None
    llm: LLMConfig = field(default_factory=LLMConfig)
    events: EventConfig = field(default_factory=EventConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    subagents: SubAgentConfig = field(default_factory=SubAgentConfig)
    teams: TeamConfig = field(default_factory=TeamConfig)
    response: ResponseConfig = field(default_factory=ResponseConfig)
    reflection: ReflectionConfig = field(default_factory=ReflectionConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    prompt: PromptConfig = field(default_factory=PromptConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    context_os: ContextOSConfig = field(default_factory=ContextOSConfig)
    ledger: LedgerConfig = field(default_factory=LedgerConfig)
    approvals: ApprovalConfig = field(default_factory=ApprovalConfig)
    tasks: TaskConfig = field(default_factory=TaskConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    channels: ChannelConfig = field(default_factory=ChannelConfig)
    gateway: GatewayConfig = field(default_factory=GatewayConfig)

    @classmethod
    def from_file(cls, path: str | Path = "orion.toml") -> OrionConfig:
        source = Path(path)
        if load_dotenv is not None:
            load_dotenv(source.parent / ".env", override=False)
        with source.open("rb") as handle:
            data = _expand(tomllib.load(handle))
        if not isinstance(data, dict):
            raise ValueError("La configuration Orion doit etre un objet TOML.")
        config = cls.from_mapping(data)
        config.config_path = source.resolve()
        return config

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> OrionConfig:
        if not isinstance(data, dict):
            raise ValueError("La configuration Orion doit etre un objet TOML.")
        orion = _section(data, "orion")
        llm = _section(data, "llm")
        events = _section(data, "events")
        runtime = _section(data, "runtime")
        subagents = _section(data, "subagents")
        teams = _section(data, "teams")
        response = _section(data, "response")
        reflection = _section(data, "reflection")
        scheduler = _section(data, "scheduler")
        prompt = _section(data, "prompt")
        context = _section(data, "context")
        memory = _section(data, "memory")
        context_os = _section(data, "context_os")
        ledger = _section(data, "ledger")
        approvals = _section(data, "approvals")
        tasks = _section(data, "tasks")
        tools = _section(data, "tools")
        channels = _section(data, "channels")
        gateway = _section(data, "gateway")

        # Public web/files callables were consolidated in config vNext.  Keep
        # old operator configs useful without re-registering the legacy model
        # tool names: migrate the known read-only families to their new
        # capability names and preserve ordering for any unrelated custom
        # tools.
        legacy_subagent_tool_names = {
            "web_search": "web",
            "web_fetch": "web",
            "fetch_url": "web",
            "fetch_json_api": "web",
            "list_files": "files",
            "read_file": "files",
            "search_files": "files",
        }
        configured_subagent_tools = subagents.get("default_tools")
        if isinstance(configured_subagent_tools, list):
            migrated_tools: list[Any] = []
            for raw_name in configured_subagent_tools:
                name = legacy_subagent_tool_names.get(raw_name, raw_name)
                if name not in migrated_tools:
                    migrated_tools.append(name)
            subagents["default_tools"] = migrated_tools

        for key in ("auth_token", "hmac_secret", "token", "password", "api_key", "secret"):
            if key in gateway and gateway[key] not in (None, ""):
                raise ValueError(f"gateway.{key} est interdit; utilisez *_env sans secret en clair.")
        enabled_channels = channels.get("enabled", [])
        if isinstance(enabled_channels, str):
            enabled_channels = [enabled_channels]
        if not isinstance(enabled_channels, list):
            raise ValueError("channels.enabled doit etre une liste de noms.")
        for key in ("enabled", "disabled"):
            if isinstance(tools.get(key), str):
                tools[key] = [tools[key]]
            if key in tools and not isinstance(tools[key], list):
                raise ValueError(f"tools.{key} doit etre une liste d'identifiants.")
        tool_settings = {
            str(key): dict(value)
            for key, value in tools.items()
            if isinstance(value, dict) and key not in {"settings"}
        }
        channel_settings = {
            str(key): dict(value)
            for key, value in channels.items()
            if isinstance(value, dict)
        }
        config = cls(
            name=str(orion.get("name", "Orion")),
            config_version=int(data.get("config_version", orion.get("config_version", CONFIG_VERSION))),
            llm=LLMConfig(**{key: value for key, value in llm.items() if key in LLMConfig.__dataclass_fields__}),
            events=EventConfig(**{key: value for key, value in events.items() if key in EventConfig.__dataclass_fields__}),
            runtime=RuntimeConfig(**{key: value for key, value in runtime.items() if key in RuntimeConfig.__dataclass_fields__}),
            subagents=SubAgentConfig(**{key: value for key, value in subagents.items() if key in SubAgentConfig.__dataclass_fields__}),
            teams=TeamConfig(**{key: value for key, value in teams.items() if key in TeamConfig.__dataclass_fields__}),
            response=ResponseConfig(**{key: value for key, value in response.items() if key in ResponseConfig.__dataclass_fields__}),
            reflection=ReflectionConfig(**{key: value for key, value in reflection.items() if key in ReflectionConfig.__dataclass_fields__}),
            scheduler=SchedulerConfig(**{key: value for key, value in scheduler.items() if key in SchedulerConfig.__dataclass_fields__}),
            prompt=PromptConfig(**{key: value for key, value in prompt.items() if key in PromptConfig.__dataclass_fields__}),
            context=ContextConfig(**{key: value for key, value in context.items() if key in ContextConfig.__dataclass_fields__}),
            memory=MemoryConfig(**{key: value for key, value in memory.items() if key in MemoryConfig.__dataclass_fields__}),
            context_os=ContextOSConfig(**{key: value for key, value in context_os.items() if key in ContextOSConfig.__dataclass_fields__}),
            ledger=LedgerConfig(**{key: value for key, value in ledger.items() if key in LedgerConfig.__dataclass_fields__}),
            approvals=ApprovalConfig(**{key: value for key, value in approvals.items() if key in ApprovalConfig.__dataclass_fields__}),
            tasks=TaskConfig(**{key: value for key, value in tasks.items() if key in TaskConfig.__dataclass_fields__}),
            tools=ToolsConfig(
                **{
                    key: value
                    for key, value in tools.items()
                    if key in {"directory", "state_path", "enabled", "disabled"}
                },
                settings=tool_settings,
            ),
            channels=ChannelConfig(
                enabled=[str(item) for item in enabled_channels],
                default=str(channels["default"]) if channels.get("default") else None,
                # ``gateway.ledger_path`` was the historical home of the
                # communication DB.  Honor it as a compatibility fallback,
                # while making the shared channel outbox configurable even
                # when the top-level gateway itself is disabled.
                ledger_path=str(
                    channels.get(
                        "ledger_path",
                        gateway.get("ledger_path", "data/communication_ledger.sqlite3"),
                    )
                ),
                settings=channel_settings,
            ),
            gateway=GatewayConfig(
                **{
                    key: value
                    for key, value in gateway.items()
                    if key in GatewayConfig.__dataclass_fields__
                }
            ),
        )
        config.validate()
        return config

    def validate(self) -> OrionConfig:
        """Valide les invariants opérationnels avant d'allouer des ressources."""

        def text(value: Any, label: str) -> str:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} doit etre une chaine non vide.")
            return value.strip()

        def integer(value: Any, label: str, minimum: int = 0) -> None:
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{label} doit etre un entier >= {minimum}.")

        def number(value: Any, label: str, minimum: float = 0.0, *, strict: bool = False) -> None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{label} doit etre un nombre.")
            value = float(value)
            if not math.isfinite(value) or value < minimum or (strict and value <= minimum):
                operator = ">" if strict else ">="
                raise ValueError(f"{label} doit etre {operator} {minimum}.")

        integer(self.config_version, "config_version", 1)
        if self.config_version > CONFIG_VERSION:
            raise ValueError(f"config_version {self.config_version} est trop recente (maximum {CONFIG_VERSION}).")
        text(self.name, "orion.name")
        text(self.llm.api_key_env, "llm.api_key_env")
        text(self.llm.model, "llm.model")
        text(self.llm.base_url, "llm.base_url")
        number(self.llm.timeout, "llm.timeout", strict=True)
        integer(self.llm.max_retries, "llm.max_retries")
        number(self.llm.retry_backoff, "llm.retry_backoff")
        number(self.llm.retry_max_delay, "llm.retry_max_delay", 0.0)
        number(self.llm.request_deadline, "llm.request_deadline", 0.0)
        if not isinstance(self.llm.default_params, dict):
            raise ValueError("llm.default_params doit etre un objet.")

        integer(self.events.workers, "events.workers")
        integer(self.events.queue_size, "events.queue_size")
        integer(self.events.default_max_attempts, "events.default_max_attempts", 1)
        number(self.events.retry_delay, "events.retry_delay")
        number(self.events.retry_backoff, "events.retry_backoff", 1.0)
        number(self.events.dedupe_ttl, "events.dedupe_ttl")
        integer(self.events.dedupe_max_entries, "events.dedupe_max_entries", 1)
        if self.events.durable_path is not None:
            text(self.events.durable_path, "events.durable_path")
        integer(self.runtime.max_turns, "runtime.max_turns", 1)
        integer(self.runtime.wake_queue_size, "runtime.wake_queue_size")
        number(self.runtime.dedupe_window, "runtime.dedupe_window")
        if self.runtime.durable_path is not None:
            text(self.runtime.durable_path, "runtime.durable_path")

        text(self.teams.path, "teams.path")
        if not isinstance(self.teams.enabled, bool):
            raise ValueError("teams.enabled doit etre un booléen.")
        instance_id = text(self.teams.instance_id, "teams.instance_id")
        team = text(self.teams.team, "teams.team")
        if "*" in instance_id or "*" in team:
            raise ValueError("teams.instance_id et teams.team ne peuvent pas contenir '*'.")
        number(self.teams.poll_interval, "teams.poll_interval", strict=True)
        integer(self.teams.max_message_chars, "teams.max_message_chars", 1)

        integer(self.subagents.workers, "subagents.workers", 1)
        if not isinstance(self.subagents.enabled, bool):
            raise ValueError("subagents.enabled doit etre un booléen.")
        integer(self.subagents.default_max_turns, "subagents.default_max_turns", 1)
        for label, value in (
            ("subagents.max_context_chars", self.subagents.max_context_chars),
            ("subagents.max_result_chars", self.subagents.max_result_chars),
            ("subagents.max_tool_output_chars", self.subagents.max_tool_output_chars),
            ("subagents.max_session_messages", self.subagents.max_session_messages),
            ("subagents.history_limit", self.subagents.history_limit),
        ):
            integer(value, label, 1)
        if not isinstance(self.subagents.default_tools, list) or any(
            not isinstance(item, str) or not item.strip() for item in self.subagents.default_tools
        ):
            raise ValueError("subagents.default_tools doit etre une liste de noms non vides.")

        integer(self.response.max_chars, "response.max_chars", 500)
        integer(self.response.max_sentences, "response.max_sentences", 1)
        integer(self.reflection.max_input_chars, "reflection.max_input_chars", 1)
        integer(self.reflection.max_output_chars, "reflection.max_output_chars", 1)
        number(self.reflection.temperature, "reflection.temperature")
        text(self.reflection.prompt_path, "reflection.prompt_path")
        number(self.scheduler.poll_interval, "scheduler.poll_interval", strict=True)
        if not isinstance(self.scheduler.enabled, bool):
            raise ValueError("scheduler.enabled doit etre un booléen.")
        text(self.scheduler.schedules_path, "scheduler.schedules_path")

        for label, value in (
            ("prompt.core_path", self.prompt.core_path),
            ("prompt.context_path", self.prompt.context_path),
            ("prompt.journal_path", self.prompt.journal_path),
        ):
            text(value, label)
        integer(self.prompt.history_limit, "prompt.history_limit", 1)
        integer(self.prompt.history_max_chars, "prompt.history_max_chars", 1)
        for label, value in (
            ("context.total_max_chars", self.context.total_max_chars),
            ("context.total_max_tokens", self.context.total_max_tokens),
            ("context.output_reserve_tokens", self.context.output_reserve_tokens),
            ("context.compactor_input_chars", self.context.compactor_input_chars),
            ("context.cache_size", self.context.cache_size),
            ("context.task_max_chars", self.context.task_max_chars),
            ("context.event_max_chars", self.context.event_max_chars),
            ("context.policy_max_chars", self.context.policy_max_chars),
            ("context.policy_max_tokens", self.context.policy_max_tokens),
            ("context.request_max_chars", self.context.request_max_chars),
            ("context.request_max_tokens", self.context.request_max_tokens),
            ("context.profile_memory_max_chars", self.context.profile_memory_max_chars),
            ("context.profile_memory_max_tokens", self.context.profile_memory_max_tokens),
            ("context.task_max_tokens", self.context.task_max_tokens),
            ("context.event_max_tokens", self.context.event_max_tokens),
            ("context.history_max_tokens", self.context.history_max_tokens),
            ("context.notifications_tools_max_chars", self.context.notifications_tools_max_chars),
            ("context.notifications_tools_max_tokens", self.context.notifications_tools_max_tokens),
            ("context.reflection_max_chars", self.context.reflection_max_chars),
            ("context.reflection_max_tokens", self.context.reflection_max_tokens),
            ("context.history_turn_limit", self.context.history_turn_limit),
        ):
            integer(value, label, 1)
        if self.context.context_contract_version != "v1":
            raise ValueError("context.context_contract_version doit etre 'v1'.")
        if self.context.context_mode not in {"contract", "legacy"}:
            raise ValueError("context.context_mode doit etre 'contract' ou 'legacy'.")
        if self.context.reflection_format not in {"advisory_json", "legacy_text"}:
            raise ValueError("context.reflection_format est invalide.")
        text(self.context.token_counter, "context.token_counter")
        for label, value in (
            ("policy_max_chars", self.context.policy_max_chars),
            ("request_max_chars", self.context.request_max_chars),
            ("profile_memory_max_chars", self.context.profile_memory_max_chars),
            ("task_max_chars", self.context.task_max_chars),
            ("event_max_chars", self.context.event_max_chars),
            ("history_max_chars", self.context.history_max_chars),
            ("notifications_tools_max_chars", self.context.notifications_tools_max_chars),
            ("reflection_max_chars", self.context.reflection_max_chars),
        ):
            if value > self.context.total_max_chars:
                raise ValueError(f"context.{label} ne peut pas depasser context.total_max_chars.")
        for label, value in (
            ("policy_max_tokens", self.context.policy_max_tokens),
            ("request_max_tokens", self.context.request_max_tokens),
            ("profile_memory_max_tokens", self.context.profile_memory_max_tokens),
            ("task_max_tokens", self.context.task_max_tokens),
            ("event_max_tokens", self.context.event_max_tokens),
            ("history_max_tokens", self.context.history_max_tokens),
            ("notifications_tools_max_tokens", self.context.notifications_tools_max_tokens),
            ("reflection_max_tokens", self.context.reflection_max_tokens),
        ):
            if value > self.context.total_max_tokens:
                raise ValueError(f"context.{label} ne peut pas depasser context.total_max_tokens.")
        number(self.memory.poll_interval, "memory.poll_interval", strict=True)
        integer(self.memory.batch_size, "memory.batch_size", 1)
        integer(self.memory.min_entries, "memory.min_entries", 1)
        if self.memory.min_entries > self.memory.batch_size:
            raise ValueError("memory.min_entries ne peut pas depasser memory.batch_size.")
        integer(self.memory.max_batches_per_run, "memory.max_batches_per_run", 1)
        number(self.memory.tail_max_age, "memory.tail_max_age")
        integer(self.memory.max_input_chars, "memory.max_input_chars", 1)
        integer(self.runtime.max_deferred_events, "runtime.max_deferred_events", 1)
        self._run_time()

        text(self.ledger.path, "ledger.path")
        if not isinstance(self.approvals.enabled, bool):
            raise ValueError("approvals.enabled doit etre un bool?en.")
        text(self.approvals.path, "approvals.path")
        text(self.context_os.registry_path, "context_os.registry_path")
        text(self.context_os.journal_path, "context_os.journal_path")
        text(self.context_os.memory_path, "context_os.memory_path")
        text(self.context_os.memory_namespace, "context_os.memory_namespace")
        text(self.context_os.state_path, "context_os.state_path")
        if not isinstance(self.tasks.enabled, bool):
            raise ValueError("tasks.enabled doit etre un booléen.")
        text(self.tasks.path, "tasks.path")
        text(self.tools.directory, "tools.directory")
        text(self.tools.state_path, "tools.state_path")
        for label, values in (("tools.enabled", self.tools.enabled), ("tools.disabled", self.tools.disabled)):
            if not isinstance(values, list) or any(not isinstance(item, str) or not item.strip() for item in values):
                raise ValueError(f"{label} doit etre une liste de noms non vides.")
        if set(self.tools.enabled) & set(self.tools.disabled):
            raise ValueError("tools.enabled et tools.disabled ne peuvent pas partager un identifiant.")
        if not isinstance(self.channels.enabled, list) or any(
            not isinstance(item, str) or not item.strip() for item in self.channels.enabled
        ):
            raise ValueError("channels.enabled doit etre une liste de noms non vides.")
        text(self.channels.ledger_path, "channels.ledger_path")
        if self.channels.default is not None:
            default = text(self.channels.default, "channels.default")
            if default not in self.channels.enabled and not (
                default == "gateway" and self.gateway.enabled
            ):
                raise ValueError("channels.default doit figurer dans channels.enabled.")
        if len(set(self.channels.enabled)) != len(self.channels.enabled):
            raise ValueError("channels.enabled ne doit pas contenir de doublons.")
        cli_settings = self.channels.settings.get("cli", {})
        if not isinstance(cli_settings, dict):
            raise ValueError("channels.cli doit etre une table TOML.")
        for key in ("style", "banner", "markdown", "timestamps", "enabled"):
            if key in cli_settings and not isinstance(cli_settings[key], bool):
                raise ValueError(f"channels.cli.{key} doit etre un booléen.")
        if "prompt" in cli_settings:
            text(cli_settings["prompt"], "channels.cli.prompt")
        if "history_path" in cli_settings and cli_settings["history_path"] is not None:
            text(cli_settings["history_path"], "channels.cli.history_path")
        if "slow_request_seconds" in cli_settings:
            number(
                cli_settings["slow_request_seconds"],
                "channels.cli.slow_request_seconds",
            )
        telegram_settings = self.channels.settings.get("telegram")
        if telegram_settings is None:
            telegram_settings = {}
        if not isinstance(telegram_settings, dict):
            raise ValueError("channels.telegram doit etre une table TOML.")
        if "telegram" in self.channels.enabled or telegram_settings:
            text(telegram_settings.get("token_env", "TELEGRAM_BOT_TOKEN"), "channels.telegram.token_env")
            for key in ("allowed_chat_ids", "allowed_user_ids", "outbound_allowed_chat_ids"):
                values = telegram_settings.get(key, [])
                if not isinstance(values, list):
                    raise ValueError(f"channels.telegram.{key} doit etre une liste d'entiers.")
                for item in values:
                    if isinstance(item, bool):
                        raise ValueError(f"channels.telegram.{key} doit etre une liste d'entiers.")
                    try:
                        int(item)
                    except (TypeError, ValueError) as exc:
                        raise ValueError(f"channels.telegram.{key} doit etre une liste d'entiers.") from exc
            if (
                not bool(telegram_settings.get("allow_all_chats", False))
                and not bool(telegram_settings.get("bootstrap_owner", True))
                and not telegram_settings.get("allowed_chat_ids")
                and not telegram_settings.get("allowed_user_ids")
            ):
                raise ValueError(
                    "channels.telegram doit declarer allowed_chat_ids, allowed_user_ids "
                    "ou allow_all_chats=true."
                )
            if "allow_all_chats" in telegram_settings and not isinstance(telegram_settings["allow_all_chats"], bool):
                raise ValueError("channels.telegram.allow_all_chats doit etre un booléen.")
            if "accept_edited" in telegram_settings and not isinstance(telegram_settings["accept_edited"], bool):
                raise ValueError("channels.telegram.accept_edited doit etre un booléen.")
            if "bootstrap_owner" in telegram_settings and not isinstance(telegram_settings["bootstrap_owner"], bool):
                raise ValueError("channels.telegram.bootstrap_owner doit etre un booléen.")
            if "bootstrap_pairing_secret_env" in telegram_settings:
                text(
                    telegram_settings["bootstrap_pairing_secret_env"],
                    "channels.telegram.bootstrap_pairing_secret_env",
                )
            if "poll_timeout" in telegram_settings:
                integer(telegram_settings["poll_timeout"], "channels.telegram.poll_timeout")
            if "api_timeout" in telegram_settings:
                number(telegram_settings["api_timeout"], "channels.telegram.api_timeout", strict=True)
            if "max_retries" in telegram_settings:
                integer(telegram_settings["max_retries"], "channels.telegram.max_retries")
            if "retry_backoff" in telegram_settings:
                number(telegram_settings["retry_backoff"], "channels.telegram.retry_backoff")
            if "retry_max_delay" in telegram_settings:
                number(telegram_settings["retry_max_delay"], "channels.telegram.retry_max_delay")
            if "queue_size" in telegram_settings:
                integer(telegram_settings["queue_size"], "channels.telegram.queue_size", 1)
                if telegram_settings["queue_size"] > 1000:
                    raise ValueError("channels.telegram.queue_size ne peut pas depasser 1000.")
            if "offset_path" in telegram_settings and telegram_settings["offset_path"] is not None:
                text(telegram_settings["offset_path"], "channels.telegram.offset_path")
            if "owner_path" in telegram_settings and telegram_settings["owner_path"] is not None:
                text(telegram_settings["owner_path"], "channels.telegram.owner_path")
            parse_mode = telegram_settings.get("parse_mode", "HTML")
            if parse_mode not in {None, "HTML", "MarkdownV2"}:
                raise ValueError("channels.telegram.parse_mode est invalide.")
            max_chars = telegram_settings.get("max_message_chars", 3500)
            integer(max_chars, "channels.telegram.max_message_chars", 500)
            if max_chars > 4096:
                raise ValueError("channels.telegram.max_message_chars ne peut pas depasser 4096.")
        email_settings = self.channels.settings.get("email")
        if email_settings is not None:
            if not isinstance(email_settings, dict):
                raise ValueError("channels.email doit etre une table TOML.")
            for key in ("allowed_senders", "allowed_recipient_domains"):
                if key not in email_settings:
                    continue
                values = email_settings[key]
                if not isinstance(values, list) or any(
                    not isinstance(item, str) or not item.strip() for item in values
                ):
                    raise ValueError(
                        f"channels.email.{key} doit etre une liste de valeurs non vides."
                    )
        text(self.gateway.host, "gateway.host")
        integer(self.gateway.port, "gateway.port", 1)
        if self.gateway.port > 65535:
            raise ValueError("gateway.port doit etre compris entre 1 et 65535.")
        gateway_path = text(self.gateway.path, "gateway.path")
        if not gateway_path.startswith("/"):
            raise ValueError("gateway.path doit commencer par '/'.")
        integer(self.gateway.max_body_bytes, "gateway.max_body_bytes", 1)
        if self.gateway.max_body_bytes > 256 * 1024:
            raise ValueError("gateway.max_body_bytes ne peut pas depasser 256 KiB.")
        integer(self.gateway.queue_size, "gateway.queue_size", 1)
        if self.gateway.queue_size > 1000:
            raise ValueError("gateway.queue_size ne peut pas depasser 1000.")
        number(self.gateway.request_timeout, "gateway.request_timeout", strict=True)
        number(self.gateway.replay_window, "gateway.replay_window", strict=True)
        if self.gateway.replay_window > 86400:
            raise ValueError("gateway.replay_window ne peut pas depasser 86400 secondes.")
        text(self.gateway.ledger_path, "gateway.ledger_path")
        auth_mode = text(self.gateway.auth_mode, "gateway.auth_mode").lower()
        if auth_mode not in {"token", "hmac"}:
            raise ValueError("gateway.auth_mode doit etre 'token' ou 'hmac'.")
        env_name = self.gateway.auth_token_env if auth_mode == "token" else self.gateway.hmac_secret_env
        if self.gateway.enabled and (not isinstance(env_name, str) or not env_name.strip()):
            raise ValueError("Le gateway active doit declarer une variable *_env d'authentification.")
        for label, values in (("gateway.allowlist", self.gateway.allowlist), ("gateway.reply_allowlist", self.gateway.reply_allowlist)):
            if not isinstance(values, list) or any(not isinstance(item, str) or not item.strip() for item in values):
                raise ValueError(f"{label} doit etre une liste de destinations non vides.")
            for item in values:
                candidate = item.strip()
                if "@" in candidate or "*" in candidate:
                    raise ValueError(f"{label} contient une destination interdite.")
                if "://" in candidate:
                    parsed = urlsplit(candidate)
                    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                        raise ValueError(f"{label} contient une URL invalide.")
        for channel_name, settings in self.channels.settings.items():
            if channel_name not in {"web", "api", "webhook"} or settings.get("enabled", True) is False:
                continue
            source_allowlist = settings.get("allowlist", [])
            if not isinstance(source_allowlist, list) or any(
                not isinstance(item, str) or not item.strip()
                for item in source_allowlist
            ):
                raise ValueError(
                    f"channels.{channel_name}.allowlist doit etre une liste de sources non vides."
                )
            if not settings.get("auth_token_env") and not settings.get("allowlist"):
                raise ValueError("Un webhook active doit declarer auth_token_env ou allowlist.")
        return self

    @property
    def base_dir(self) -> Path:
        return self.config_path.parent if self.config_path is not None else Path.cwd()

    def path(self, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Un chemin de configuration ne peut pas etre vide.")
        candidate = Path(value)
        return str(candidate if candidate.is_absolute() else self.base_dir / candidate)

    def _run_time(self) -> day_time:
        try:
            if not isinstance(self.memory.run_at, str):
                raise ValueError
            hour, minute = (int(item) for item in self.memory.run_at.split(":", 1))
            return day_time(hour, minute)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("memory.run_at doit etre au format HH:MM.") from exc

    def _configure_channels(
        self,
        router: Any,
        *,
        usage_ledger: Any = None,
        communication_ledger: Any = None,
    ) -> None:
        from channel_adapters import (
            CLIAdapter,
            DiscordWebhookAdapter,
            EmailAdapter,
            HttpWebhookAdapter,
            TelegramAdapter,
            secret_from_env,
        )

        for name in self.channels.enabled:
            settings = self.channels.settings.get(name, {})
            if settings.get("enabled", True) is False:
                continue
            if name == "cli":
                history_value = settings.get("history_path", "data/cli_history.txt")
                router.register(
                    CLIAdapter(
                        prompt=str(settings.get("prompt", "❯ ")),
                        style=settings.get("style"),
                        banner=bool(settings.get("banner", True)),
                        name=self.name,
                        model=self.llm.model,
                        history_path=self.path(str(history_value)) if history_value else None,
                        markdown=bool(settings.get("markdown", True)),
                        timestamps=bool(settings.get("timestamps", True)),
                        slow_request_seconds=float(settings.get("slow_request_seconds", 15.0)),
                        usage_ledger=usage_ledger,
                    )
                )
            elif name == "telegram":
                token = secret_from_env(str(settings.get("token_env", "TELEGRAM_BOT_TOKEN")))
                pairing_secret_env = settings.get("bootstrap_pairing_secret_env")
                pairing_secret = (
                    secret_from_env(str(pairing_secret_env))
                    if pairing_secret_env is not None
                    else None
                )
                router.register(
                    TelegramAdapter(
                        token,
                        poll_timeout=int(settings.get("poll_timeout", 25)),
                        allowed_chat_ids=[int(item) for item in settings.get("allowed_chat_ids", [])],
                        allowed_user_ids=[int(item) for item in settings.get("allowed_user_ids", [])],
                        allow_all_chats=bool(settings.get("allow_all_chats", False)),
                        accept_edited=bool(settings.get("accept_edited", False)),
                        bootstrap_owner=bool(settings.get("bootstrap_owner", True)),
                        bootstrap_pairing_secret=pairing_secret,
                        owner_path=(
                            self.path(str(settings["owner_path"]))
                            if "owner_path" in settings and settings.get("owner_path")
                            else (self.path("data/telegram.owner") if "owner_path" not in settings else None)
                        ),
                        outbound_allowed_chat_ids=(
                            [int(item) for item in settings["outbound_allowed_chat_ids"]]
                            if settings.get("outbound_allowed_chat_ids") is not None else None
                        ),
                        # Keep Telegram's cursor durable by default; an empty
                        # value explicitly opts into the in-memory legacy mode.
                        offset_path=(
                            self.path(str(settings["offset_path"]))
                            if "offset_path" in settings and settings.get("offset_path")
                            else (self.path("data/telegram.offset") if "offset_path" not in settings else None)
                        ),
                        parse_mode=settings.get("parse_mode", "HTML"),
                        max_message_chars=int(settings.get("max_message_chars", 3500)),
                        api_timeout=float(settings.get("api_timeout", 35.0)),
                        max_retries=int(settings.get("max_retries", 3)),
                        retry_backoff=float(settings.get("retry_backoff", 0.5)),
                        retry_max_delay=float(settings.get("retry_max_delay", 30.0)),
                        queue_size=int(settings.get("queue_size", 1000)),
                        ledger=communication_ledger,
                    )
                )
            elif name == "email":
                password = secret_from_env(str(settings.get("password_env", "EMAIL_PASSWORD")))
                router.register(
                    EmailAdapter(
                        imap_host=str(settings["imap_host"]),
                        smtp_host=str(settings["smtp_host"]),
                        username=str(settings["username"]),
                        password=password,
                        imap_port=int(settings.get("imap_port", 993)),
                        smtp_port=int(settings.get("smtp_port", 465)),
                        mailbox=str(settings.get("mailbox", "INBOX")),
                        poll_interval=float(settings.get("poll_interval", 60.0)),
                        subject=str(settings.get("subject", "Orion")),
                        smtp_starttls=bool(settings.get("smtp_starttls", False)),
                        # Legacy/manual configs may omit the field.  Keep them
                        # parseable, but make the runtime default explicit and
                        # fail-closed instead of restoring historical accept-all.
                        allowed_senders=settings.get("allowed_senders", ()),
                        allowed_recipient_domains=settings.get(
                            "allowed_recipient_domains", ()
                        ),
                    )
                )
            elif name == "discord":
                webhook_url = secret_from_env(str(settings.get("webhook_url_env", "DISCORD_WEBHOOK_URL")))
                router.register(DiscordWebhookAdapter(webhook_url))
            elif name in {"web", "api", "webhook"}:
                auth_token = None
                if settings.get("auth_token_env"):
                    auth_token = secret_from_env(str(settings["auth_token_env"]))
                outbound_url = None
                if settings.get("outbound_url_env"):
                    outbound_url = secret_from_env(str(settings["outbound_url_env"]))
                router.register(
                    HttpWebhookAdapter(
                        name=name,
                        host=str(settings.get("host", "127.0.0.1")),
                        port=int(settings.get("port", 8080)),
                        path=str(settings.get("path", "/webhook")),
                        auth_token=auth_token,
                        outbound_url=outbound_url,
                        replay_window=float(settings.get("replay_window", 300.0)),
                        allowlist=settings.get("allowlist", ()),
                        ledger=communication_ledger,
                    )
                )
            else:
                raise ValueError(f"Aucun adaptateur fourni pour le channel configure : {name}")

    def _configure_gateway(self, router: Any, *, ledger: Any = None) -> Any | None:
        """Register the top-level ``[gateway]`` adapter when enabled.

        The communication ledger belongs to the application lifetime.  Return
        it to :meth:`build` so ``OrionApplication.stop`` can close the SQLite
        connection after the adapter has quiesced.
        """
        if not self.gateway.enabled:
            return None
        from channel_adapters import HttpWebhookAdapter, secret_from_env
        from communication_ledger import CommunicationLedger

        auth_token = None
        hmac_secret = None
        if self.gateway.auth_mode.lower() == "token":
            auth_token = secret_from_env(self.gateway.auth_token_env)
        else:
            hmac_secret = secret_from_env(self.gateway.hmac_secret_env or "ORION_GATEWAY_HMAC_SECRET")
        owns_ledger = ledger is None
        if ledger is None:
            ledger = CommunicationLedger(self.path(self.channels.ledger_path))
        try:
            router.register(
                HttpWebhookAdapter(
                    name="gateway",
                    host=self.gateway.host,
                    port=self.gateway.port,
                    path=self.gateway.path,
                    auth_token=auth_token,
                    hmac_secret=hmac_secret,
                    timeout=self.gateway.request_timeout,
                    request_timeout=self.gateway.request_timeout,
                    replay_window=self.gateway.replay_window,
                    max_body_bytes=self.gateway.max_body_bytes,
                    queue_size=self.gateway.queue_size,
                    allowlist=self.gateway.allowlist,
                    outbound_allowlist=self.gateway.reply_allowlist,
                    ledger=ledger,
                )
            )
        except Exception:
            if owns_ledger:
                ledger.close()
            raise
        return ledger

    def _effective_tool_settings(self) -> dict[str, dict[str, Any]]:
        """Resolve Orion-owned compatibility semantics before plugins load.

        Tool packages can evolve independently from the core.  ``orion.web``
        historically documents ``provider = \"auto\"`` as Tavily when its API
        key exists, otherwise the public HTTP fallback.  Newer installed
        versions may instead interpret ``auto`` as "start Selenium first".
        Resolve the value here so installing/updating a package cannot silently
        launch Chrome for an ordinary web search.  Selenium remains available
        only when the operator explicitly configures ``provider = \"browser\"``.
        """
        settings = {
            str(name): dict(value)
            for name, value in self.tools.settings.items()
            if isinstance(value, dict)
        }
        web = settings.get("web")
        if isinstance(web, dict) and str(web.get("provider", "auto")).strip().lower() == "auto":
            env_name = str(web.get("api_key_env", "TAVILY_API_KEY")).strip() or "TAVILY_API_KEY"
            web["provider"] = "tavily" if os.getenv(env_name, "").strip() else "public"
        return settings

    def build(self) -> OrionApplication:
        """Construit Orion et toutes ses dependances a partir de la config."""
        self.validate()
        from action_ledger import ActionLedger
        from approvals import ApprovalStore
        from channels import ChannelRouter
        from communication_ledger import CommunicationLedger
        from event_handler import EventHandler
        from openrouter_client import OpenRouterClient
        from context_assembler import ContextAssembler, ContextPolicy
        from context_os import ThreadStateStore
        from context_registry import ContextRegistry
        from memory_store import MemoryStore
        from prompt_context import (
            ConversationJournal,
            MemoryExtractor,
            MemoryMaintenance,
            PromptComposer,
            PromptContextStore,
            SQLiteConversationJournal,
        )
        from reflection_engine import ReflectionEngine
        from runtime import AgentRuntime
        from scheduler import JsonScheduleStore, Scheduler
        from subagents import SubAgentManager
        from teams import TeamBus
        from tasks import JsonTaskStore
        from tool_manager import ToolManager

        llm = OpenRouterClient(
            api_key=os.getenv(self.llm.api_key_env),
            model=self.llm.model,
            base_url=self.llm.base_url,
            timeout=self.llm.timeout,
            max_retries=self.llm.max_retries,
            retry_backoff=self.llm.retry_backoff,
            retry_max_delay=self.llm.retry_max_delay,
            request_deadline=self.llm.request_deadline,
            site_url=self.llm.site_url,
            site_name=self.llm.site_name,
            default_params=self.llm.default_params,
        )
        events = EventHandler(
            workers=self.events.workers,
            queue_size=self.events.queue_size,
            default_max_attempts=self.events.default_max_attempts,
            retry_delay=self.events.retry_delay,
            retry_backoff=self.events.retry_backoff,
            dedupe_ttl=self.events.dedupe_ttl,
            dedupe_max_entries=self.events.dedupe_max_entries,
            durable_path=(
                self.path(self.events.durable_path)
                if self.events.durable_path is not None
                else None
            ),
        )
        prompt_store = PromptContextStore(
            self.path(self.prompt.context_path),
            core_path=self.path(self.prompt.core_path),
            **{key: value for key, value in {
                "personality": self.prompt.personality,
                "methodology": self.prompt.methodology,
                "additional": self.prompt.additional,
            }.items() if value is not None},
        )
        prompt_composer = PromptComposer(
            prompt_store,
            context_mode=self.context.context_mode,
            max_chars=self.context.policy_max_chars,
            max_tokens=self.context.policy_max_tokens,
        )
        # Context OS is opt-in.  Existing deployments retain JSONL and
        # in-memory runtime state unless the new section is explicitly enabled.
        context_registry = None
        thread_state_store = None
        retrieval_store = None
        runtime_holder: dict[str, Any] = {}

        def active_context_scope() -> dict[str, str] | None:
            runtime_instance = runtime_holder.get("runtime")
            run_context = getattr(runtime_instance, "_run_context", None)
            event = getattr(run_context, "event", None)
            if event is None:
                return None
            metadata = event.metadata if isinstance(getattr(event, "metadata", None), dict) else {}
            payload = event.payload if isinstance(getattr(event, "payload", None), dict) else {}
            conversation_fn = getattr(runtime_instance, "_conversation_id", None)
            if callable(conversation_fn):
                conversation_id = str(conversation_fn(event))
            else:
                conversation_id = str(
                    metadata.get("conversation_id")
                    or payload.get("conversation_id")
                    or getattr(event, "id", "default")
                )
            thread_id = str(
                metadata.get("message_thread_id")
                or metadata.get("thread_id")
                or payload.get("message_thread_id")
                or payload.get("thread_id")
                or conversation_id
            )
            scope = str(
                metadata.get("context_scope")
                or payload.get("context_scope")
                or metadata.get("scope")
                or payload.get("scope")
                or "global"
            )
            return {
                "scope": scope,
                "conversation_id": conversation_id,
                "thread_id": thread_id,
            }

        if self.context_os.enabled:
            for backend_path in (self.context_os.registry_path, self.context_os.journal_path,
                                 self.context_os.memory_path, self.context_os.state_path):
                self.path(backend_path) and Path(self.path(backend_path)).parent.mkdir(parents=True, exist_ok=True)
            context_registry = ContextRegistry(
                self.path(self.context_os.registry_path),
                scope_resolver=active_context_scope,
            )
            thread_state_store = ThreadStateStore(
                self.path(self.context_os.state_path),
                scope_resolver=active_context_scope,
            )
            retrieval_store = MemoryStore(self.path(self.context_os.memory_path))
        journal = (SQLiteConversationJournal(self.path(self.context_os.journal_path))
                   if self.context_os.enabled and self.context_os.journal_enabled
                   else ConversationJournal(self.path(self.prompt.journal_path)))
        assembler_options = {
            "compactor": llm if (self.context.compaction_enabled and (self.context.context_mode == "legacy" or self.context.llm_compaction_enabled)) else None,
            "compactor_model": self.context.compactor_model,
            "total_max_chars": self.context.total_max_chars,
            "total_max_tokens": self.context.total_max_tokens,
            "output_reserve_tokens": self.context.output_reserve_tokens,
            "compactor_input_chars": self.context.compactor_input_chars,
            "cache_size": self.context.cache_size,
            "context_contract_version": self.context.context_contract_version,
            "context_mode": self.context.context_mode,
            "policy_max_chars": self.context.policy_max_chars,
            "policy_max_tokens": self.context.policy_max_tokens,
            "request_max_chars": self.context.request_max_chars,
            "request_max_tokens": self.context.request_max_tokens,
            "profile_memory_max_chars": self.context.profile_memory_max_chars,
            "profile_memory_max_tokens": self.context.profile_memory_max_tokens,
            "task_max_chars": self.context.task_max_chars,
            "task_max_tokens": self.context.task_max_tokens,
            "event_max_chars": self.context.event_max_chars,
            "event_max_tokens": self.context.event_max_tokens,
            "history_max_chars": self.context.history_max_chars,
            "history_max_tokens": self.context.history_max_tokens,
            "notifications_tools_max_chars": self.context.notifications_tools_max_chars,
            "notifications_tools_max_tokens": self.context.notifications_tools_max_tokens,
            "reflection_max_chars": self.context.reflection_max_chars,
            "reflection_max_tokens": self.context.reflection_max_tokens,
            "history_turn_limit": self.context.history_turn_limit,
            "redaction_enabled": self.context.redaction_enabled,
            "llm_compaction_enabled": self.context.llm_compaction_enabled,
            "token_counter": self.context.token_counter,
        }
        # ContextAssembler owns reduction, redaction, and token accounting;
        # this object only translates the validated TOML knobs into its policy.
        policy = ContextPolicy(
            version=self.context.context_contract_version,
            context_mode=self.context.context_mode,
            policy_max_chars=self.context.policy_max_chars,
            policy_max_tokens=self.context.policy_max_tokens,
            request_max_chars=self.context.request_max_chars,
            request_max_tokens=self.context.request_max_tokens,
            profile_max_chars=self.context.profile_memory_max_chars,
            profile_max_tokens=self.context.profile_memory_max_tokens,
            task_max_chars=self.context.task_max_chars,
            task_max_tokens=self.context.task_max_tokens,
            event_max_chars=self.context.event_max_chars,
            event_max_tokens=self.context.event_max_tokens,
            history_max_chars=self.context.history_max_chars,
            history_max_tokens=self.context.history_max_tokens,
            observations_max_chars=self.context.notifications_tools_max_chars,
            observations_max_tokens=self.context.notifications_tools_max_tokens,
            reflection_max_chars=self.context.reflection_max_chars,
            reflection_max_tokens=self.context.reflection_max_tokens,
            total_max_chars=self.context.total_max_chars,
            total_max_tokens=self.context.total_max_tokens,
            output_reserve_tokens=self.context.output_reserve_tokens,
            history_turn_limit=self.context.history_turn_limit,
            redaction_enabled=self.context.redaction_enabled,
            llm_compaction_enabled=self.context.llm_compaction_enabled,
        )
        assembler_options["policy"] = policy
        supported = inspect.signature(ContextAssembler).parameters
        context_assembler = ContextAssembler(
            **{key: value for key, value in assembler_options.items() if key in supported},
            **({"memory_store": retrieval_store, "memory_namespace": self.context_os.memory_namespace,
                "context_registry": context_registry} if self.context_os.enabled else {})
        )
        effective_tool_settings = self._effective_tool_settings()
        tool_manager = ToolManager(
            self.path(self.tools.directory),
            state_path=self.path(self.tools.state_path),
            root_dir=self.base_dir,
            bundled_dir=Path(__file__).resolve().with_name("tool_packages"),
            config={
                "enabled": self.tools.enabled,
                "disabled": self.tools.disabled,
                **effective_tool_settings,
                "_core": {
                    "tasks": dict(vars(self.tasks)),
                    "scheduler": dict(vars(self.scheduler)),
                    "subagents": dict(vars(self.subagents)),
                    "teams": dict(vars(self.teams)),
                },
            },
        )
        # Only guidance from tools that were actually loaded (and therefore
        # enabled by the configured allow/deny lists) is exposed to the
        # runtime.  ToolManager resets this mapping on every load_all call.
        loaded_manifests = tool_manager.load_all(llm)
        tool_guidance = tool_manager.loaded_guidance()
        tool_policy = tool_manager.tool_policy()
        tool_policy.set_approvals_enabled(self.approvals.enabled)
        active_modules = {
            manifest.id for manifest in loaded_manifests if manifest.kind == "module"
        }
        disabled_modules = set(self.tools.disabled)

        def module_or_legacy(module_id: str, legacy_enabled: bool) -> bool:
            if module_id in disabled_modules:
                return False
            return module_id in active_modules or bool(legacy_enabled)

        tasks_enabled = module_or_legacy(
            TASKS_MODULE_ID,
            self.tasks.enabled or self.scheduler.enabled,
        )
        scheduler_enabled = module_or_legacy(TASKS_MODULE_ID, self.scheduler.enabled)
        subagents_enabled = module_or_legacy(SUBAGENTS_MODULE_ID, self.subagents.enabled)
        team_enabled = module_or_legacy(TEAM_MODULE_ID, self.teams.enabled)
        runtime_surfaces: set[str] = set()
        if tasks_enabled:
            runtime_surfaces.add("task")
        if subagents_enabled:
            runtime_surfaces.add("subagent")
        if team_enabled:
            runtime_surfaces.add("team")
        # Deferred-event acknowledgement is only model-visible when at least
        # one runtime capability is active. A truly empty Orion therefore
        # exposes no runtime tools at all.
        if runtime_surfaces:
            runtime_surfaces.add("event")
        approval_store = (
            ApprovalStore(self.path(self.approvals.path))
            if self.approvals.enabled
            else None
        )
        # Runtime and sub-agents must share the exact same durable action
        # ledger.  Besides avoiding duplicate ownership/SQLite connections,
        # this ensures a custom [ledger].path protects side effects uniformly
        # across both execution paths.
        action_ledger = ActionLedger(self.path(self.ledger.path))
        subagent_manager = None
        if subagents_enabled:
            subagent_manager = SubAgentManager(
                llm,
                events,
                state_path=self.path(self.subagents.state_path),
                workers=self.subagents.workers,
                default_model=self.subagents.default_model or self.llm.model,
                default_tools=self.subagents.default_tools,
                tool_guidance=tool_guidance,
                default_max_turns=self.subagents.default_max_turns,
                max_context_chars=self.subagents.max_context_chars,
                max_result_chars=self.subagents.max_result_chars,
                max_tool_output_chars=self.subagents.max_tool_output_chars,
                max_session_messages=self.subagents.max_session_messages,
                history_limit=self.subagents.history_limit,
                emit_progress_events=self.subagents.emit_progress_events,
                tool_policy=tool_policy,
                action_ledger=action_ledger,
            )
        team_bus = None
        if team_enabled:
            team_bus = TeamBus(
                self.path(self.teams.path),
                instance_id=self.teams.instance_id,
                team=self.teams.team,
                poll_interval=self.teams.poll_interval,
                max_message_chars=self.teams.max_message_chars,
                event_handler=events,
            )
        maintenance = None
        if self.memory.enabled:
            extractor = MemoryExtractor(
                llm,
                prompt_store,
                model=self.memory.model,
                max_input_chars=self.memory.max_input_chars,
            )
            maintenance_kwargs = {
                "batch_size": self.memory.batch_size,
                "min_entries": self.memory.min_entries,
                "run_at": self._run_time(),
                "poll_interval": self.memory.poll_interval,
            }
            maintenance_parameters = inspect.signature(MemoryMaintenance).parameters
            if "max_batches_per_run" in maintenance_parameters:
                maintenance_kwargs["max_batches_per_run"] = self.memory.max_batches_per_run
            if "tail_max_age" in maintenance_parameters:
                maintenance_kwargs["tail_max_age"] = self.memory.tail_max_age
            if "max_batches_per_run" not in maintenance_parameters and self.memory.max_batches_per_run > 1:
                # Compatibility adapter for an older worker API.  The
                # inherited scheduler calls self.run_once(), so overriding only
                # this method drains several bounded batches per scheduled pass.
                max_batches = self.memory.max_batches_per_run

                class _DrainingMemoryMaintenance(MemoryMaintenance):
                    def run_once(self) -> int:
                        total = 0
                        for _ in range(max_batches):
                            processed = super().run_once()
                            total += processed
                            if processed < self.batch_size:
                                break
                        return total

                maintenance = _DrainingMemoryMaintenance(
                    journal,
                    extractor,
                    **maintenance_kwargs,
                )
            else:
                maintenance = MemoryMaintenance(journal, extractor, **maintenance_kwargs)
            # Keep the effective drain limit observable across both the current
            # compatibility adapter and a future native implementation.
            maintenance.max_batches_per_run = self.memory.max_batches_per_run
        reflection_engine = None
        if self.reflection.enabled:
            reflection_engine = ReflectionEngine(
                llm,
                prompt_path=self.path(self.reflection.prompt_path),
                model=self.reflection.model,
                max_input_chars=self.reflection.max_input_chars,
                max_output_chars=self.reflection.max_output_chars,
                temperature=self.reflection.temperature,
                reflection_format=self.context.reflection_format,
            )
        scheduler = None
        if scheduler_enabled:
            scheduler = Scheduler(
                events,
                store=JsonScheduleStore(self.path(self.scheduler.schedules_path)),
                poll_interval=self.scheduler.poll_interval,
            )
        # One application-owned communication ledger backs every channel's
        # inbound dedupe where supported and, critically, the router's durable
        # outbound outbox.  It exists even when [gateway] is disabled.
        communication_ledger = CommunicationLedger(self.path(self.channels.ledger_path))
        channel_router = ChannelRouter(
            events,
            default_channel=self.channels.default or ("gateway" if self.gateway.enabled else None),
            ledger=communication_ledger,
        )
        try:
            self._configure_channels(
                channel_router,
                usage_ledger=getattr(llm, "usage_ledger", None),
                communication_ledger=communication_ledger,
            )
            gateway_ledger = self._configure_gateway(
                channel_router,
                ledger=communication_ledger,
            )
            runtime = AgentRuntime(
                llm_client=llm,
                task_store=(JsonTaskStore(self.path(self.tasks.path)) if tasks_enabled else None),
                scheduler=scheduler,
                subagent_manager=subagent_manager,
                team_bus=team_bus,
                action_ledger=action_ledger,
                tool_policy=tool_policy,
                approval_store=approval_store,
                max_turns=self.runtime.max_turns,
                wake_queue_size=self.runtime.wake_queue_size,
                dedupe_window=self.runtime.dedupe_window,
                parallel_tool_calls=self.runtime.parallel_tool_calls,
                queue_events_during_run=self.runtime.queue_events_during_run,
                wake_on_subagent_progress=self.runtime.wake_on_subagent_progress,
                max_deferred_events=self.runtime.max_deferred_events,
                durable_path=(
                    self.path(self.runtime.durable_path)
                    if self.runtime.durable_path is not None
                    else None
                ),
                response_max_chars=self.response.max_chars,
                response_max_sentences=self.response.max_sentences,
                response_concise=self.response.concise,
                reflection_engine=reflection_engine if self.context.reflection_enabled else None,
                prompt_store=prompt_store,
                prompt_composer=prompt_composer,
                conversation_journal=journal,
                history_enabled=self.prompt.history_enabled,
                history_limit=self.prompt.history_limit,
                history_max_chars=self.prompt.history_max_chars,
                context_assembler=context_assembler,
                task_context_max_chars=self.context.task_max_chars,
                event_context_max_chars=self.context.event_max_chars,
                context_mode=self.context.context_mode,
                tool_guidance=tool_guidance,
                runtime_surfaces=sorted(runtime_surfaces),
                thread_state_store=thread_state_store,
                context_registry=context_registry,
                retrieval_store=retrieval_store,
                memory_maintenance=maintenance,
                on_output=channel_router.route,
            ).attach(events)
            runtime_holder["runtime"] = runtime
            if subagent_manager is not None and self.approvals.enabled:
                # Optional strict mode: privileged worker calls are brokered by
                # the parent Orion runtime and require an operator decision.
                subagent_manager.tool_approval_broker = (
                    runtime._subagent_tool_approval_broker
                )
                runtime._reconcile_subagent_approval_decisions()
        except Exception:
            communication_ledger.close()
            raise
        return OrionApplication(
            events=events,
            llm=llm,
            runtime=runtime,
            tool_manager=tool_manager,
            scheduler=scheduler,
            subagents=subagent_manager,
            team_bus=team_bus,
            channels=channel_router,
            communication_ledger=communication_ledger,
            gateway_ledger=gateway_ledger,
        )


@dataclass
class OrionApplication:
    """Objets construits et cycle de vie de l'application Orion."""

    events: Any
    llm: Any
    runtime: Any
    tool_manager: Any = None
    scheduler: Any = None
    subagents: Any = None
    team_bus: Any = None
    channels: Any = None
    communication_ledger: Any = None
    gateway_ledger: Any = None
    _lifecycle_lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _started: bool = field(default=False, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def task_store(self) -> Any:
        """Expose the runtime task store without duplicating ownership."""
        return getattr(self.runtime, "task_store", None)

    @property
    def context_registry(self) -> Any:
        """Expose the optional Context OS registry owned by the runtime."""
        return getattr(self.runtime, "context_registry", None)

    @property
    def retrieval_store(self) -> Any:
        """Expose the optional retrieval/memory store owned by the runtime."""
        return getattr(self.runtime, "retrieval_store", None)

    @property
    def conversation_journal(self) -> Any:
        """Expose the conversation journal owned by the runtime."""
        return getattr(self.runtime, "conversation_journal", None)

    @property
    def usage_ledger(self) -> Any:
        """Expose the LLM usage ledger as the canonical cost/usage source."""
        return getattr(self.llm, "usage_ledger", None)

    @property
    def approval_store(self) -> Any:
        """Expose the runtime-owned approval store without duplicating ownership."""
        return getattr(self.runtime, "approval_store", None)

    def start(self) -> OrionApplication:
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("L'application Orion est fermée.")
            if self._started:
                return self
            started: list[Any] = []
            try:
                started.append(self.events)
                self.events.start()
                if self.scheduler is not None:
                    started.append(self.scheduler)
                    self.scheduler.start()
                if self.subagents is not None:
                    started.append(self.subagents)
                    self.subagents.start()
                if self.team_bus is not None:
                    started.append(self.team_bus)
                    self.team_bus.start()
                if self.channels is not None:
                    started.append(self.channels)
                    self.channels.start()
                started.append(self.runtime)
                self.runtime.start()
            except Exception:
                # Un démarrage partiel ne doit pas laisser de threads de
                # poller ou de channel derrière l'exception de configuration.
                for component in reversed(started):
                    try:
                        component.stop()
                    except Exception:
                        pass
                closed_resources: set[int] = set()
                for resource in (
                    self.scheduler,
                    self.subagents,
                    self.communication_ledger,
                    self.gateway_ledger,
                ):
                    if resource is None or id(resource) in closed_resources:
                        continue
                    close = getattr(resource, "close", None)
                    if callable(close):
                        closed_resources.add(id(resource))
                        close()
                self._closed = True
                raise
            self._started = True
        return self

    def stop(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            self._started = False
            # Quiesce all producers first. EventHandler must then drain every
            # event it already accepted while the runtime is still alive; only
            # after that handoff is complete may the runtime perform its own
            # final drain. Keep TeamBus open through the runtime drain so team
            # deliveries already in flight can still be acknowledged.
            if self.channels is not None:
                self.channels.stop()
            if self.scheduler is not None:
                self.scheduler.stop()
            if self.subagents is not None:
                self.subagents.stop()
            if self.team_bus is not None:
                self.team_bus.stop()
            self.events.stop()
            self.runtime.stop()

            closed_resources: set[int] = set()

            def close_once(resource: Any) -> None:
                if resource is None:
                    return
                resource_id = id(resource)
                if resource_id in closed_resources:
                    return
                close = getattr(resource, "close", None)
                if not callable(close):
                    return
                closed_resources.add(resource_id)
                close()

            # Runtime-owned stores must stay open until both EventHandler and
            # the runtime have drained. Some configurations can reuse the same
            # backing object for more than one role, so close by identity once.
            for attribute in (
                "action_ledger",
                "approval_store",
                "_durable_store",
                "context_registry",
                "retrieval_store",
                "conversation_journal",
            ):
                close_once(getattr(self.runtime, attribute, None))

            close_once(self.events)
            close_once(self.team_bus)
            close_once(self.scheduler)
            close_once(self.subagents)
            close_once(self.llm)
            close_once(self.communication_ledger)
            close_once(self.gateway_ledger)

    def run_forever(self, stop_event: threading.Event | None = None) -> None:
        """Demarre Orion et maintient le processus actif jusqu'a son arret."""
        shutdown = stop_event or threading.Event()
        self.start()
        try:
            shutdown.wait()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()


def load_orion(path: str | Path = "orion.toml") -> OrionApplication:
    """Charge ``path`` puis construit l'application Orion."""
    return OrionConfig.from_file(path).build()


__all__ = [
    "CONFIG_VERSION",
    "ApprovalConfig",
    "EventConfig",
    "ChannelConfig",
    "GatewayConfig",
    "LedgerConfig",
    "LLMConfig",
    "MemoryConfig",
    "ContextOSConfig",
    "OrionApplication",
    "OrionConfig",
    "PromptConfig",
    "ReflectionConfig",
    "RuntimeConfig",
    "SubAgentConfig",
    "TeamConfig",
    "SchedulerConfig",
    "TaskConfig",
    "load_orion",
]
