"""Configuration centralisee et bootstrap de l'application Orion.

Le fichier de configuration est TOML. Les secrets restent dans `.env` ; le
champ ``api_key_env`` indique uniquement le nom de la variable a utiliser.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from datetime import time as day_time
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


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
    model: str = "~openai/gpt-latest"
    base_url: str = "https://openrouter.ai/api/v1"
    timeout: float = 60.0
    max_retries: int = 2
    retry_backoff: float = 0.5
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


@dataclass
class RuntimeConfig:
    max_turns: int = 12
    wake_queue_size: int = 0
    dedupe_window: float = 86400.0
    parallel_tool_calls: bool = False


@dataclass
class SubAgentConfig:
    """Workers IA persistants, indépendants du runtime principal."""

    enabled: bool = True
    state_path: str = "data/subagents.json"
    workers: int = 3
    default_model: str | None = "deepseek/deepseek-v4-flash-0731"
    default_tools: list[str] = field(
        default_factory=lambda: ["web_search", "web_fetch", "fetch_url", "fetch_json_api"]
    )
    default_max_turns: int = 8
    max_context_chars: int = 16000
    max_result_chars: int = 24000
    max_tool_output_chars: int = 12000
    history_limit: int = 200
    emit_progress_events: bool = True


@dataclass
class ResponseConfig:
    """Style et garde-fous des réponses envoyées aux utilisateurs."""

    concise: bool = True
    max_chars: int = 3000
    max_sentences: int = 8


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
    enabled: bool = True
    poll_interval: float = 1.0
    schedules_path: str = "data/schedules.json"


@dataclass
class PromptConfig:
    core_path: str = "ORION_CORE.md"
    context_path: str = "data/prompt_context.json"
    journal_path: str = "data/conversations.jsonl"
    history_enabled: bool = True
    history_limit: int = 20
    history_max_chars: int = 12000
    personality: str | None = None
    methodology: str | None = None
    additional: str = ""


@dataclass
class ContextConfig:
    compaction_enabled: bool = True
    compactor_model: str = "openai/gpt-4o-mini"
    total_max_chars: int = 60000
    compactor_input_chars: int = 30000
    cache_size: int = 64
    task_max_chars: int = 12000
    event_max_chars: int = 10000


@dataclass
class MemoryConfig:
    enabled: bool = False
    model: str = "openai/gpt-4o-mini"
    run_at: str = "23:00"
    batch_size: int = 20
    poll_interval: float = 30.0
    max_input_chars: int = 30000


@dataclass
class LedgerConfig:
    path: str = "data/action_ledger.sqlite3"


@dataclass
class TaskConfig:
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
    settings: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class OrionConfig:
    """Configuration complete, chargeable depuis ``orion.toml``."""

    name: str = "Orion"
    config_path: Path | None = None
    llm: LLMConfig = field(default_factory=LLMConfig)
    events: EventConfig = field(default_factory=EventConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    subagents: SubAgentConfig = field(default_factory=SubAgentConfig)
    response: ResponseConfig = field(default_factory=ResponseConfig)
    reflection: ReflectionConfig = field(default_factory=ReflectionConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    prompt: PromptConfig = field(default_factory=PromptConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    ledger: LedgerConfig = field(default_factory=LedgerConfig)
    tasks: TaskConfig = field(default_factory=TaskConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    channels: ChannelConfig = field(default_factory=ChannelConfig)

    @classmethod
    def from_file(cls, path: str | Path = "orion.toml") -> OrionConfig:
        source = Path(path)
        with source.open("rb") as handle:
            data = _expand(tomllib.load(handle))
        if not isinstance(data, dict):
            raise ValueError("La configuration Orion doit etre un objet TOML.")
        config = cls.from_mapping(data)
        config.config_path = source.resolve()
        return config

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> OrionConfig:
        orion = _section(data, "orion")
        llm = _section(data, "llm")
        events = _section(data, "events")
        runtime = _section(data, "runtime")
        subagents = _section(data, "subagents")
        response = _section(data, "response")
        reflection = _section(data, "reflection")
        scheduler = _section(data, "scheduler")
        prompt = _section(data, "prompt")
        context = _section(data, "context")
        memory = _section(data, "memory")
        ledger = _section(data, "ledger")
        tasks = _section(data, "tasks")
        tools = _section(data, "tools")
        channels = _section(data, "channels")
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
        return cls(
            name=str(orion.get("name", "Orion")),
            llm=LLMConfig(**{key: value for key, value in llm.items() if key in LLMConfig.__dataclass_fields__}),
            events=EventConfig(**{key: value for key, value in events.items() if key in EventConfig.__dataclass_fields__}),
            runtime=RuntimeConfig(**{key: value for key, value in runtime.items() if key in RuntimeConfig.__dataclass_fields__}),
            subagents=SubAgentConfig(**{key: value for key, value in subagents.items() if key in SubAgentConfig.__dataclass_fields__}),
            response=ResponseConfig(**{key: value for key, value in response.items() if key in ResponseConfig.__dataclass_fields__}),
            reflection=ReflectionConfig(**{key: value for key, value in reflection.items() if key in ReflectionConfig.__dataclass_fields__}),
            scheduler=SchedulerConfig(**{key: value for key, value in scheduler.items() if key in SchedulerConfig.__dataclass_fields__}),
            prompt=PromptConfig(**{key: value for key, value in prompt.items() if key in PromptConfig.__dataclass_fields__}),
            context=ContextConfig(**{key: value for key, value in context.items() if key in ContextConfig.__dataclass_fields__}),
            memory=MemoryConfig(**{key: value for key, value in memory.items() if key in MemoryConfig.__dataclass_fields__}),
            ledger=LedgerConfig(**{key: value for key, value in ledger.items() if key in LedgerConfig.__dataclass_fields__}),
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
                settings=channel_settings,
            ),
        )

    @property
    def base_dir(self) -> Path:
        return self.config_path.parent if self.config_path is not None else Path.cwd()

    def path(self, value: str) -> str:
        candidate = Path(value)
        return str(candidate if candidate.is_absolute() else self.base_dir / candidate)

    def _run_time(self) -> day_time:
        try:
            hour, minute = (int(item) for item in self.memory.run_at.split(":", 1))
            return day_time(hour, minute)
        except (ValueError, TypeError) as exc:
            raise ValueError("memory.run_at doit etre au format HH:MM.") from exc

    def _configure_channels(self, router: Any) -> None:
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
                    )
                )
            elif name == "telegram":
                token = secret_from_env(str(settings.get("token_env", "TELEGRAM_BOT_TOKEN")))
                router.register(
                    TelegramAdapter(
                        token,
                        poll_timeout=int(settings.get("poll_timeout", 25)),
                        allowed_chat_ids=[int(item) for item in settings.get("allowed_chat_ids", [])],
                        parse_mode=settings.get("parse_mode", "HTML"),
                        max_message_chars=int(settings.get("max_message_chars", 3500)),
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
                    )
                )
            else:
                raise ValueError(f"Aucun adaptateur fourni pour le channel configure : {name}")

    def build(self) -> OrionApplication:
        """Construit Orion et toutes ses dependances a partir de la config."""
        from action_ledger import ActionLedger
        from channels import ChannelRouter
        from event_handler import EventHandler
        from openrouter_client import OpenRouterClient
        from context_assembler import ContextAssembler
        from prompt_context import ConversationJournal, MemoryExtractor, MemoryMaintenance, PromptContextStore
        from reflection_engine import ReflectionEngine
        from runtime import AgentRuntime
        from scheduler import JsonScheduleStore, Scheduler
        from subagents import SubAgentManager
        from tasks import JsonTaskStore
        from tool_manager import ToolManager

        llm = OpenRouterClient(
            api_key=os.getenv(self.llm.api_key_env),
            model=self.llm.model,
            base_url=self.llm.base_url,
            timeout=self.llm.timeout,
            max_retries=self.llm.max_retries,
            retry_backoff=self.llm.retry_backoff,
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
        journal = ConversationJournal(self.path(self.prompt.journal_path))
        context_assembler = ContextAssembler(
            compactor=llm if self.context.compaction_enabled else None,
            compactor_model=self.context.compactor_model,
            total_max_chars=self.context.total_max_chars,
            compactor_input_chars=self.context.compactor_input_chars,
            cache_size=self.context.cache_size,
        )
        tool_manager = ToolManager(
            self.path(self.tools.directory),
            state_path=self.path(self.tools.state_path),
            root_dir=self.base_dir,
            config={
                "enabled": self.tools.enabled,
                "disabled": self.tools.disabled,
                **self.tools.settings,
            },
        )
        tool_manager.load_all(llm)
        subagent_manager = None
        if self.subagents.enabled:
            subagent_manager = SubAgentManager(
                llm,
                events,
                state_path=self.path(self.subagents.state_path),
                workers=self.subagents.workers,
                default_model=self.subagents.default_model or self.llm.model,
                default_tools=self.subagents.default_tools,
                default_max_turns=self.subagents.default_max_turns,
                max_context_chars=self.subagents.max_context_chars,
                max_result_chars=self.subagents.max_result_chars,
                max_tool_output_chars=self.subagents.max_tool_output_chars,
                history_limit=self.subagents.history_limit,
                emit_progress_events=self.subagents.emit_progress_events,
            )
        maintenance = None
        if self.memory.enabled:
            extractor = MemoryExtractor(
                llm,
                prompt_store,
                model=self.memory.model,
                max_input_chars=self.memory.max_input_chars,
            )
            maintenance = MemoryMaintenance(
                journal,
                extractor,
                batch_size=self.memory.batch_size,
                run_at=self._run_time(),
                poll_interval=self.memory.poll_interval,
            )
        reflection_engine = None
        if self.reflection.enabled:
            reflection_engine = ReflectionEngine(
                llm,
                prompt_path=self.path(self.reflection.prompt_path),
                model=self.reflection.model,
                max_input_chars=self.reflection.max_input_chars,
                max_output_chars=self.reflection.max_output_chars,
                temperature=self.reflection.temperature,
            )
        scheduler = None
        if self.scheduler.enabled:
            scheduler = Scheduler(
                events,
                store=JsonScheduleStore(self.path(self.scheduler.schedules_path)),
                poll_interval=self.scheduler.poll_interval,
            )
        channel_router = ChannelRouter(events, default_channel=self.channels.default)
        self._configure_channels(channel_router)
        runtime = AgentRuntime(
            llm_client=llm,
            task_store=JsonTaskStore(self.path(self.tasks.path)),
            scheduler=scheduler,
            subagent_manager=subagent_manager,
            action_ledger=ActionLedger(self.path(self.ledger.path)),
            max_turns=self.runtime.max_turns,
            wake_queue_size=self.runtime.wake_queue_size,
            dedupe_window=self.runtime.dedupe_window,
            parallel_tool_calls=self.runtime.parallel_tool_calls,
            response_max_chars=self.response.max_chars,
            response_max_sentences=self.response.max_sentences,
            response_concise=self.response.concise,
            reflection_engine=reflection_engine,
            prompt_store=prompt_store,
            conversation_journal=journal,
            history_enabled=self.prompt.history_enabled,
            history_limit=self.prompt.history_limit,
            history_max_chars=self.prompt.history_max_chars,
            context_assembler=context_assembler,
            task_context_max_chars=self.context.task_max_chars,
            event_context_max_chars=self.context.event_max_chars,
            memory_maintenance=maintenance,
            on_output=channel_router.route,
        ).attach(events)
        return OrionApplication(
            events=events,
            llm=llm,
            runtime=runtime,
            scheduler=scheduler,
            subagents=subagent_manager,
            channels=channel_router,
        )


@dataclass
class OrionApplication:
    """Objets construits et cycle de vie de l'application Orion."""

    events: Any
    llm: Any
    runtime: Any
    scheduler: Any = None
    subagents: Any = None
    channels: Any = None

    def start(self) -> OrionApplication:
        self.events.start()
        if self.scheduler is not None:
            self.scheduler.start()
        if self.subagents is not None:
            self.subagents.start()
        if self.channels is not None:
            self.channels.start()
        self.runtime.start()
        return self

    def stop(self) -> None:
        if self.subagents is not None:
            self.subagents.stop()
        self.runtime.stop()
        if self.channels is not None:
            self.channels.stop()
        if self.scheduler is not None:
            self.scheduler.stop()
        self.events.stop()
        self.llm.close()

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
    "EventConfig",
    "ChannelConfig",
    "LedgerConfig",
    "LLMConfig",
    "MemoryConfig",
    "OrionApplication",
    "OrionConfig",
    "PromptConfig",
    "ReflectionConfig",
    "RuntimeConfig",
    "SubAgentConfig",
    "SchedulerConfig",
    "TaskConfig",
    "load_orion",
]
