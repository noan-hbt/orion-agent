"""Assistant d'installation local d'Orion.

Usage : ``python orion_install.py``. Le script ne contacte aucun service et
n'ecrit jamais la cle API dans ``orion.toml``.
"""

from __future__ import annotations

import argparse
import getpass
import importlib.resources
import json
import os
import re
import secrets as secrets_module
import shutil
import sys
import tempfile
from pathlib import Path

from dotenv import dotenv_values

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib


CHANNEL_SECRET_DEFAULTS = {
    "telegram": "TELEGRAM_BOT_TOKEN",
    "discord": "DISCORD_WEBHOOK_URL",
    "email": "EMAIL_PASSWORD",
    "web": "ORION_WEBHOOK_TOKEN",
    "api": "ORION_WEBHOOK_TOKEN",
    "webhook": "ORION_WEBHOOK_TOKEN",
}
VALID_CHANNELS = frozenset(
    {"cli", "telegram", "discord", "email", "web", "api", "webhook"}
)
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PROMPT_RESOURCE_NAMES = ("ORION_CORE.md", "REFLECTION_CORE.md")
TELEGRAM_PAIRING_SECRET_ENV = "TELEGRAM_PAIRING_SECRET"
DEFAULT_ENABLED_TOOLS: tuple[str, ...] = ()


def _ask(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        interactive = bool(sys.stdin.isatty())
    except (AttributeError, OSError):
        interactive = False
    # ``install()`` is also a programmatic/non-interactive API.  Never try to
    # read stdin in CI, pipes or service provisioning: omitted optional values
    # fall back to the same defaults shown by the interactive installer.
    if not interactive:
        return default
    try:
        value = input(f"{label}{suffix}: ").strip()
    except EOFError:
        return default
    return value or default


def _ask_secret(label: str) -> str:
    prompt = f"{label} (laisser vide pour conserver l'existant): "
    try:
        if sys.stdin.isatty():
            return getpass.getpass(prompt).strip()
    except (AttributeError, OSError):
        pass
    return input(prompt).strip() if sys.stdin.isatty() else ""


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _write_env(path: Path, values: dict[str, str]) -> None:
    values = {key: value for key, value in values.items() if value}
    if not values:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = existing.splitlines()
    for key, value in values.items():
        prefix = f"{key}="
        for index, line in enumerate(lines):
            if re.match(rf"^(?:export\s+)?{re.escape(key)}\s*=", line):
                lead = "export " if line.lstrip().startswith("export ") else ""
                lines[index] = f"{lead}{key}={_dotenv_value(value)}"
                break
        else:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(f"{prefix}{_dotenv_value(value)}")
    _atomic_write(path, "\n".join(lines) + "\n")
    # Best-effort protection on POSIX; Windows ACLs are handled by the user
    # or deployment policy and chmod may be a no-op there.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _dotenv_value(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:@%+,-]+", value):
        return value
    return (
        '"'
        + value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        + '"'
    )


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def _prompt_resource_text(name: str) -> str:
    """Lit un prompt canonique depuis le checkout ou les ressources du wheel."""
    if name not in PROMPT_RESOURCE_NAMES:
        raise ValueError(f"Ressource de prompt inconnue: {name}")

    # En checkout source, conserver les fichiers racine comme source canonique
    # afin que le orion.toml versionné continue de fonctionner tel quel.
    source_path = Path(__file__).resolve().with_name(name)
    if source_path.is_file():
        return source_path.read_text(encoding="utf-8")

    try:
        resource = importlib.resources.files("orion_resources").joinpath(name)
        return resource.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise RuntimeError(f"Ressource Orion embarquée introuvable: {name}") from exc


def _provision_prompt_resources(config_path: Path) -> None:
    """Crée les prompts référencés par le TOML sans écraser les fichiers utilisateur."""
    for name in PROMPT_RESOURCE_NAMES:
        target = config_path.parent / name
        if target.exists():
            continue
        _atomic_write(target, _prompt_resource_text(name))


def _toml_literal(value: object) -> str:
    """Render a Python scalar/list back to TOML for config preservation."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_literal(item) for item in value) + "]"
    raise ValueError(f"valeur TOML non supportee: {type(value).__name__}")


# Sections that are regenerated by the template and must not be carried over.
# ``channels`` is handled separately: the top-level keys are regenerated while
# per-channel sub-tables keep the operator's security settings.
_REGENERATED_SECTIONS = frozenset(
    {
        "orion",
        "llm",
        "events",
        "runtime",
        "subagents",
        "teams",
        "response",
        "reflection",
        "tasks",
        "ledger",
        "scheduler",
        "prompt",
        "tools",
    }
)
# Preserved verbatim: these carry operator hardening that a template rewrite
# used to discard, silently re-opening channels and reverting policy.
_PRESERVED_CHANNEL_KEYS = frozenset(
    {
        "allowed_chat_ids",
        "allowed_user_ids",
        "outbound_allowed_chat_ids",
        "allow_all_chats",
        "bootstrap_owner",
        "bootstrap_pairing_secret_env",
        "token_env",
        "password_env",
        "webhook_url_env",
        "auth_token_env",
        "hmac_secret_env",
        "allowlist",
        "reply_allowlist",
        "allow_redirects",
        "allowed_senders",
        "owner_path",
        "offset_path",
    }
)


# Sub-tables inside a regenerated section that still carry operator policy and
# are not emitted by the template.
_PRESERVED_SUBTABLES = frozenset({("tools", "policy")})


def _preserved_config_sections(config_path: Path, base_text: str = "") -> str:
    """Return TOML text for settings a ``--force`` rewrite must not drop.

    Previously ``--force`` regenerated the file from a literal template and
    kept only ``tools.enabled``/``tools.disabled``.  Everything else the
    operator had hardened disappeared without warning: ``[tools.policy]``
    privileges, ``[approvals]``, ``[context]``/``[context_os]`` tuning, and
    every channel allowlist (re-opening a previously restricted bot).

    ``base_text`` is the freshly generated configuration; any table it already
    declares is skipped, because TOML forbids declaring the same table twice.
    Sections the template regenerates with its own defaults (``[context]``,
    ``[memory]``, ``[llm]``, ``[runtime]``, ...) are therefore reset by design;
    the previous file is always preserved as ``orion.toml.backup`` so those
    tuning values remain recoverable.
    """
    if not config_path.is_file():
        return ""
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""

    def declared(header: str) -> bool:
        return any(
            line.strip() == header
            for line in base_text.splitlines()
        )

    blocks: list[str] = []
    for section, table in data.items():
        if section in _REGENERATED_SECTIONS:
            # Still carry over explicitly known policy sub-tables that the
            # template does not emit (e.g. [tools.policy]).
            if isinstance(table, dict):
                for nested, sub in table.items():
                    if (section, nested) not in _PRESERVED_SUBTABLES:
                        continue
                    if not isinstance(sub, dict) or not sub:
                        continue
                    nested_header = f"[{section}.{nested}]"
                    if declared(nested_header):
                        continue
                    lines = [
                        f"{key} = {_toml_literal(value)}"
                        for key, value in sorted(sub.items())
                        if not isinstance(value, dict)
                    ]
                    if lines:
                        blocks.append("\n".join([nested_header, *lines]))
            continue
        if section == "channels":
            if not isinstance(table, dict):
                continue
            for channel, settings in table.items():
                if channel in {"enabled", "default"} or not isinstance(settings, dict):
                    continue
                header = f"[channels.{channel}]"
                if declared(header):
                    # The template wrote this channel: it already carries the
                    # pairing/allowlist fields it controls, and duplicating the
                    # table would make the file unparseable.
                    continue
                lines = [
                    f"{key} = {_toml_literal(value)}"
                    for key, value in sorted(settings.items())
                    if key in _PRESERVED_CHANNEL_KEYS
                ]
                if lines:
                    blocks.append("\n".join([header, *lines]))
            continue
        if section == "gateway":
            # The gateway holds auth material and is not regenerated.
            if isinstance(table, dict) and table and not declared("[gateway]"):
                lines = [
                    f"{key} = {_toml_literal(value)}"
                    for key, value in sorted(table.items())
                ]
                blocks.append("\n".join(["[gateway]", *lines]))
            continue
        if isinstance(table, dict):
            # Scalar-only tables (e.g. [approvals], [context], [tools.policy]).
            flat = {
                key: value
                for key, value in table.items()
                if not isinstance(value, dict)
            }
            header = f"[{section}]"
            if flat and not declared(header):
                lines = [
                    f"{key} = {_toml_literal(value)}"
                    for key, value in sorted(flat.items())
                ]
                blocks.append("\n".join([header, *lines]))
            for nested, sub in table.items():
                if isinstance(sub, dict) and sub:
                    nested_header = f"[{section}.{nested}]"
                    if declared(nested_header):
                        continue
                    lines = [
                        f"{key} = {_toml_literal(value)}"
                        for key, value in sorted(sub.items())
                        if not isinstance(value, dict)
                    ]
                    if lines:
                        blocks.append("\n".join([nested_header, *lines]))
    if not blocks:
        return ""
    header = (
        "\n# --- Reglages conserves depuis la configuration precedente ---\n"
        "# Ecrits par --force pour ne pas annuler un durcissement manuel.\n"
    )
    return header + "\n\n".join(blocks) + "\n"


def _existing_tool_activation(config_path: Path) -> tuple[list[str], list[str]]:
    """Récupère les autorisations explicites d'une config remplacée par --force."""
    if not config_path.is_file():
        return [], []
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        # --force doit rester capable de réparer une ancienne config invalide.
        return [], []
    tools = data.get("tools", {}) if isinstance(data, dict) else {}
    if not isinstance(tools, dict):
        tools = {}

    def names(key: str) -> list[str]:
        raw = tools.get(key, [])
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return []
        result: list[str] = []
        for item in raw:
            if isinstance(item, str) and item.strip() and item.strip() not in result:
                result.append(item.strip())
        return result

    enabled = names("enabled")
    disabled = names("disabled")

    # Migrate the old service booleans to explicit bundled-module activation
    # when --force rewrites a pre-module config. Explicit tools.disabled always
    # wins, matching OrionConfig.build's compatibility fallback.
    legacy_modules = (
        ("subagents", "orion.subagents"),
        ("scheduler", "orion.tasks"),
        ("tasks", "orion.tasks"),
        ("teams", "orion.team"),
    )
    if isinstance(data, dict):
        for section_name, module_id in legacy_modules:
            section = data.get(section_name, {})
            if (
                isinstance(section, dict)
                and section.get("enabled") is True
                and module_id not in disabled
                and module_id not in enabled
            ):
                enabled.append(module_id)
    return enabled, disabled


def _validate_inputs(channels: list[str], secrets: dict[str, str]) -> None:
    if len(set(channels)) != len(channels) or any(
        not c or c not in VALID_CHANNELS for c in channels
    ):
        raise ValueError("Channels invalides")
    for name in secrets:
        if not ENV_NAME_RE.fullmatch(name):
            raise ValueError(f"Nom de variable d'environnement invalide: {name!r}")


def _config_text(
    model: str,
    channels: list[str],
    default_channel: str | None,
    memory: bool,
    secret_envs: dict[str, str],
    email_settings: dict[str, object] | None = None,
    compactor_model: str = "openai/gpt-4o-mini",
    memory_model: str = "openai/gpt-4o-mini",
    reflection_model: str = "openai/gpt-4o-mini",
    telegram_pairing_secret_env: str | None = None,
    enabled_tools: list[str] | None = None,
    disabled_tools: list[str] | None = None,
) -> str:
    enabled = ", ".join(_toml_string(channel) for channel in channels)
    default_line = (
        f"default = {_toml_string(default_channel)}"
        if default_channel
        else '# default = "cli"'
    )
    channel_sections: list[str] = []
    for channel in channels:
        if channel == "telegram":
            pairing_line = (
                f"bootstrap_pairing_secret_env = {_toml_string(telegram_pairing_secret_env)}\n"
                if telegram_pairing_secret_env
                else ""
            )
            channel_sections.append(
                f"[channels.telegram]\nenabled = true\n"
                f"token_env = {_toml_string(secret_envs[channel])}\n"
                "# Le bootstrap owner exige /pair <secret> quand un secret de pairing est configure.\n"
                "bootstrap_owner = true\n"
                f"{pairing_line}"
                'owner_path = "data/telegram.owner"\n'
                'offset_path = "data/telegram.offset"\n'
                "max_message_chars = 3500"
            )
        elif channel == "discord":
            channel_sections.append(
                f"[channels.discord]\nenabled = true\nwebhook_url_env = {_toml_string(secret_envs[channel])}"
            )
        elif channel == "email":
            settings = email_settings or {}
            allowed_senders = settings.get("allowed_senders", [])
            if not isinstance(allowed_senders, list):
                raise ValueError("email_settings.allowed_senders doit etre une liste")
            allowed_senders_toml = ", ".join(
                _toml_string(str(sender)) for sender in allowed_senders
            )
            channel_sections.append(
                "[channels.email]\n"
                "enabled = true\n"
                f"imap_host = {_toml_string(settings.get('imap_host', 'imap.example.com'))}\n"
                f"smtp_host = {_toml_string(settings.get('smtp_host', 'smtp.example.com'))}\n"
                f"username = {_toml_string(settings.get('username', ''))}\n"
                f"password_env = {_toml_string(secret_envs[channel])}\n"
                "# Fail-closed pour les nouvelles installations: vide = aucun inbound accepte.\n"
                f"allowed_senders = [{allowed_senders_toml}]"
            )
        elif channel in {"web", "api", "webhook"}:
            channel_sections.append(
                f"[channels.{channel}]\nenabled = true\nauth_token_env = {_toml_string(secret_envs[channel])}"
            )
    channels_detail = "\n\n".join(channel_sections)
    disabled_tool_ids = list(dict.fromkeys(disabled_tools or []))
    enabled_tool_ids = list(
        dict.fromkeys([*DEFAULT_ENABLED_TOOLS, *(enabled_tools or [])])
    )
    disabled_set = set(disabled_tool_ids)
    enabled_tool_ids = [item for item in enabled_tool_ids if item not in disabled_set]
    enabled_tools_toml = ", ".join(_toml_string(item) for item in enabled_tool_ids)
    disabled_tools_toml = ", ".join(_toml_string(item) for item in disabled_tool_ids)
    return f"""# Configuration generee par orion_install.py

[orion]
name = "Orion"
config_version = 2

[gateway]
# Le gateway reste desactive jusqu'a une configuration explicite.
enabled = false
host = "127.0.0.1"
port = 8080
path = "/webhook"
max_body_bytes = 262144
queue_size = 1000
request_timeout = 20.0
auth_mode = "token"
auth_token_env = "ORION_GATEWAY_TOKEN"
hmac_secret_env = "ORION_GATEWAY_HMAC_SECRET"
allowlist = []
reply_allowlist = []
allow_redirects = false

[llm]
api_key_env = "OPENROUTER_API_KEY"
model = {_toml_string(model)}
base_url = "https://openrouter.ai/api/v1"
timeout = 60.0
max_retries = 2
retry_backoff = 0.5
site_name = "Orion"

[llm.default_params]
temperature = 0.2

[events]
workers = 1
queue_size = 1000
default_max_attempts = 3
retry_delay = 1.0
retry_backoff = 2.0
durable_path = "data/events.sqlite3"

[runtime]
max_turns = 12
wake_queue_size = 0
dedupe_window = 86400.0
parallel_tool_calls = true
queue_events_during_run = true
wake_on_subagent_progress = false
durable_path = "data/events.sqlite3"

[subagents]
enabled = false
state_path = "data/subagents.json"
workers = 5
default_model = "z-ai/glm-5.3-flash"
default_tools = []
default_max_turns = 10
max_context_chars = 16000
max_result_chars = 24000
max_tool_output_chars = 12000
max_session_messages = 100
history_limit = 200
emit_progress_events = false

[teams]
# Bus SQLite partage optionnel entre plusieurs processus Orion.
enabled = false
path = "data/teams.sqlite3"
instance_id = "orion"
team = "default"
poll_interval = 1.0
max_message_chars = 12000

[response]
concise = true
max_chars = 1800
max_sentences = 5

[reflection]
enabled = false
model = {_toml_string(reflection_model)}
prompt_path = "REFLECTION_CORE.md"
max_input_chars = 12000
max_output_chars = 5000
temperature = 0.7

[tasks]
enabled = false
path = "data/tasks.json"

[tools]
directory = "tools"
state_path = "data/installed_tools.json"
enabled = [{enabled_tools_toml}]
disabled = [{disabled_tools_toml}]

[tools.terminal]
max_timeout = 120
max_output_chars = 12000
allow_outside_root = false

[tools.web]
# auto est résolu par Orion avant le chargement du plugin : Tavily si
# TAVILY_API_KEY existe, sinon recherche HTTP publique. Selenium/Chrome n'est
# utilisé que si provider = "browser" est demandé explicitement.
provider = "auto"
api_provider = "tavily"
api_key_env = "TAVILY_API_KEY"
api_url = "https://api.tavily.com/search"
search_depth = "basic"
topic = "general"
timeout = 20
max_results = 8
max_chars = 16000
max_bytes = 2000000
max_search_bytes = 1500000
search_engines = ["bing_rss", "duckduckgo_html", "duckduckgo_lite"]
cache_ttl = 300
cache_size = 128
allow_private = false

[tools.files]
max_read_chars = 40000
max_file_bytes = 2000000

[tools.github]
repo = "noan-hbt/orion-tools"
ref = "main"
timeout = 20

[ledger]
path = "data/action_ledger.sqlite3"

[scheduler]
enabled = false
poll_interval = 1.0
schedules_path = "data/schedules.json"

[channels]
enabled = [{enabled}]
{default_line}
{channels_detail}

[channels.cli]
enabled = true
style = true
banner = true
markdown = true
timestamps = true
history_path = "data/cli_history.txt"
# Alerte visuelle si une requête dépasse ce délai ; 0 désactive l'alerte.
slow_request_seconds = 0

[prompt]
core_path = "ORION_CORE.md"
context_path = "data/prompt_context.json"
journal_path = "data/conversations.jsonl"
history_enabled = true
history_limit = 2000
history_max_chars = 140000
additional = ""

[context]
compaction_enabled = true
compactor_model = {_toml_string(compactor_model)}
total_max_chars = 200000
total_max_tokens = 48000
output_reserve_tokens = 3000
compactor_input_chars = 30000
cache_size = 64
task_max_chars = 12000
event_max_chars = 10000
history_max_chars = 140000
history_max_tokens = 30000
history_turn_limit = 2000

[memory]
enabled = {str(memory).lower()}
model = {_toml_string(memory_model)}
run_at = "23:00"
batch_size = 20
min_entries = 20
poll_interval = 30.0
max_input_chars = 30000
"""


def _backup_config(path: Path) -> Path:
    candidate = path.with_name(f"{path.name}.backup")
    index = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.backup.{index}")
        index += 1
    shutil.copy2(path, candidate)
    return candidate


def install(
    *,
    config_path: Path,
    env_path: Path,
    model: str | None,
    compactor_model: str | None = None,
    memory_model: str | None = None,
    reflection_model: str | None = None,
    api_key: str | None,
    channels: list[str] | None,
    memory: bool | None,
    email_allowed_senders: list[str] | None = None,
    secrets: dict[str, str] | None = None,
    force: bool = False,
) -> None:
    if config_path.exists() and not force:
        raise FileExistsError(
            f"{config_path} existe deja ; utilisez --force pour le remplacer."
        )
    selected_model = model or _ask("Modele OpenRouter", "openai/gpt-5.6-luna")
    previous_enabled_tools, previous_disabled_tools = (
        _existing_tool_activation(config_path) if force else ([], [])
    )
    selected_compactor_model = compactor_model or _ask(
        "Modele de compaction du contexte",
        "deepseek/deepseek-v4-flash-0731",
    )
    selected_reflection_model = reflection_model or _ask(
        "Modele de pre-reflexion",
        "deepseek/deepseek-v4-flash-0731",
    )
    selected_key = (
        api_key if api_key is not None else _ask_secret("Cle OPENROUTER_API_KEY")
    )
    selected_channels = (
        channels
        if channels is not None
        else [
            item.strip()
            for item in _ask(
                "Channels actives (separes par des virgules)", "cli"
            ).split(",")
            if item.strip()
        ]
    )
    _validate_inputs(selected_channels, secrets or {})
    selected_memory = (
        memory
        if memory is not None
        else _ask("Activer la memoire automatique ? oui/non", "non").lower()
        in {"oui", "o", "yes", "y"}
    )
    selected_memory_model = memory_model or (
        _ask("Modele d'extraction de memoire", "deepseek/deepseek-v4-flash-0731")
        if selected_memory
        else "deepseek/deepseek-v4-flash-0731"
    )
    collected_secrets = dict(secrets or {})
    generated_pairing_secret: str | None = None
    secret_envs: dict[str, str] = {}
    for channel in selected_channels:
        default_env = CHANNEL_SECRET_DEFAULTS.get(channel)
        if default_env is None:
            continue
        env_name = default_env
        secret_envs[channel] = env_name
        if env_name not in collected_secrets:
            collected_secrets[env_name] = _ask_secret(
                f"Secret du channel {channel} ({env_name})"
            )
    telegram_pairing_secret_env: str | None = None
    if "telegram" in selected_channels:
        telegram_pairing_secret_env = TELEGRAM_PAIRING_SECRET_ENV
        pairing_secret = str(
            collected_secrets.get(TELEGRAM_PAIRING_SECRET_ENV) or ""
        ).strip()
        if not pairing_secret and env_path.exists():
            pairing_secret = str(
                dotenv_values(env_path).get(TELEGRAM_PAIRING_SECRET_ENV) or ""
            ).strip()
        if not pairing_secret:
            pairing_secret = secrets_module.token_urlsafe(32)
            generated_pairing_secret = pairing_secret
        collected_secrets[TELEGRAM_PAIRING_SECRET_ENV] = pairing_secret
    email_settings: dict[str, object] = {}
    if "email" in selected_channels:
        if email_allowed_senders is None:
            normalized_email_senders: list[str] = []
        else:
            normalized_email_senders = []
            for raw_sender in email_allowed_senders:
                sender = str(raw_sender).strip()
                if not sender or "\r" in sender or "\n" in sender or "@" not in sender:
                    raise ValueError(
                        f"Expediteur email autorise invalide: {raw_sender!r}"
                    )
                normalized_email_senders.append(sender)
        email_settings = {
            "imap_host": _ask("Serveur IMAP", "imap.example.com"),
            "smtp_host": _ask("Serveur SMTP", "smtp.example.com"),
            "username": _ask("Adresse email Orion"),
            "allowed_senders": normalized_email_senders,
        }
    default_channel = selected_channels[0] if selected_channels else None
    config_content = _config_text(
        selected_model,
        selected_channels,
        default_channel,
        selected_memory,
        secret_envs,
        email_settings,
        selected_compactor_model,
        selected_memory_model,
        selected_reflection_model,
        telegram_pairing_secret_env,
        previous_enabled_tools,
        previous_disabled_tools,
    )
    preserved = _preserved_config_sections(config_path, config_content) if force else ""
    if preserved:
        config_content = config_content.rstrip("\n") + "\n" + preserved
    backup_path = (
        _backup_config(config_path) if config_path.exists() and force else None
    )
    _atomic_write(config_path, config_content)
    _provision_prompt_resources(config_path)
    _write_env(env_path, {"OPENROUTER_API_KEY": selected_key, **collected_secrets})
    print(f"Configuration ecrite dans {config_path}")
    if backup_path is not None:
        print(f"Ancienne configuration sauvegardee dans {backup_path}")
    print(
        f"Secrets ecrits dans {env_path}"
        if selected_key or collected_secrets
        else "Cles API conservees/omises"
    )
    if generated_pairing_secret is not None:
        print(
            "Pairing Telegram initial : envoyez ce message en privé au bot une seule fois : "
            f"/pair {generated_pairing_secret}"
        )
    if "email" in selected_channels and not email_settings.get("allowed_senders"):
        print(
            "Securite email : allowed_senders=[] ; aucun email entrant ne sera accepte "
            "tant qu'une allowlist d'expediteurs n'est pas definie dans orion.toml."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure une installation Orion")
    parser.add_argument("--config", type=Path, default=Path("orion.toml"))
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--model")
    parser.add_argument(
        "--compactor-model", help="Modele utilise pour compacter le contexte"
    )
    parser.add_argument(
        "--memory-model", help="Modele utilise pour extraire la memoire"
    )
    parser.add_argument(
        "--reflection-model", help="Modele utilise pour la pre-reflexion"
    )
    parser.add_argument("--api-key")
    parser.add_argument("--channels", help="Liste separee par des virgules")
    parser.add_argument(
        "--email-allowed-sender",
        action="append",
        default=None,
        metavar="ADDRESS",
        help="Autorise cet expediteur email entrant (option repetable). Sans cette option, l'email est fail-closed.",
    )
    parser.add_argument(
        "--secret-env",
        action="append",
        default=[],
        metavar="NAME",
        help="Demande interactivement un secret de channel",
    )
    parser.add_argument(
        "--set-secret",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Ajoute un secret sans interaction",
    )
    parser.add_argument("--memory", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    channels = (
        [item.strip() for item in args.channels.split(",") if item.strip()]
        if args.channels
        else None
    )
    secrets: dict[str, str] = {}
    for item in args.set_secret:
        name, separator, value = item.partition("=")
        if not separator or not name.strip():
            parser.error("--set-secret doit etre au format NAME=VALUE")
        secrets[name.strip()] = value
    for name in args.secret_env:
        secrets[name] = getpass.getpass(f"{name} (laisser vide pour ignorer): ").strip()
    install(
        config_path=args.config,
        env_path=args.env,
        model=args.model,
        compactor_model=args.compactor_model,
        memory_model=args.memory_model,
        reflection_model=args.reflection_model,
        api_key=args.api_key,
        channels=channels,
        memory=args.memory,
        email_allowed_senders=args.email_allowed_sender,
        secrets=secrets,
        force=args.force,
    )


if __name__ == "__main__":
    main()
