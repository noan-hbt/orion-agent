"""Assistant d'installation local d'Orion.

Usage : ``python orion_install.py``. Le script ne contacte aucun service et
n'ecrit jamais la cle API dans ``orion.toml``.
"""

from __future__ import annotations

import argparse
import getpass
import json
import shutil
from pathlib import Path


CHANNEL_SECRET_DEFAULTS = {
    "telegram": "TELEGRAM_BOT_TOKEN",
    "discord": "DISCORD_WEBHOOK_URL",
    "email": "EMAIL_PASSWORD",
    "web": "ORION_WEBHOOK_TOKEN",
    "api": "ORION_WEBHOOK_TOKEN",
    "webhook": "ORION_WEBHOOK_TOKEN",
}


def _ask(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def _ask_secret(label: str) -> str:
    return getpass.getpass(f"{label} (laisser vide pour conserver l'existant): ").strip()


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _write_env(path: Path, values: dict[str, str]) -> None:
    values = {key: value for key, value in values.items() if value}
    if not values:
        return
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = existing.splitlines()
    for key, value in values.items():
        prefix = f"{key}="
        for index, line in enumerate(lines):
            if line.startswith(prefix):
                lines[index] = f"{prefix}{value}"
                break
        else:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(f"{prefix}{value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _config_text(
    model: str,
    channels: list[str],
    default_channel: str | None,
    memory: bool,
    secret_envs: dict[str, str],
    email_settings: dict[str, str] | None = None,
    compactor_model: str = "openai/gpt-4o-mini",
    memory_model: str = "openai/gpt-4o-mini",
    reflection_model: str = "openai/gpt-4o-mini",
) -> str:
    enabled = ", ".join(_toml_string(channel) for channel in channels)
    default_line = f"default = {_toml_string(default_channel)}" if default_channel else "# default = \"cli\""
    channel_sections: list[str] = []
    for channel in channels:
        if channel == "telegram":
            channel_sections.append(
                f'[channels.telegram]\nenabled = true\n'
                f'token_env = {_toml_string(secret_envs[channel])}\n'
                "max_message_chars = 3500"
            )
        elif channel == "discord":
            channel_sections.append(f'[channels.discord]\nenabled = true\nwebhook_url_env = {_toml_string(secret_envs[channel])}')
        elif channel == "email":
            settings = email_settings or {}
            channel_sections.append(
                "[channels.email]\n"
                "enabled = true\n"
                f"imap_host = {_toml_string(settings.get('imap_host', 'imap.example.com'))}\n"
                f"smtp_host = {_toml_string(settings.get('smtp_host', 'smtp.example.com'))}\n"
                f"username = {_toml_string(settings.get('username', ''))}\n"
                f"password_env = {_toml_string(secret_envs[channel])}"
            )
        elif channel in {"web", "api", "webhook"}:
            channel_sections.append(
                f'[channels.{channel}]\nenabled = true\nauth_token_env = {_toml_string(secret_envs[channel])}'
            )
    channels_detail = "\n\n".join(channel_sections)
    return f'''# Configuration generee par orion_install.py

[orion]
name = "Orion"

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
queue_size = 0
default_max_attempts = 3
retry_delay = 1.0
retry_backoff = 2.0

[runtime]
max_turns = 12
wake_queue_size = 0
dedupe_window = 86400.0
parallel_tool_calls = true

[response]
concise = true
max_chars = 3000
max_sentences = 8

[reflection]
enabled = false
model = {_toml_string(reflection_model)}
prompt_path = "REFLECTION_CORE.md"
max_input_chars = 12000
max_output_chars = 5000
temperature = 0.7

[tasks]
path = "data/tasks.json"

[tools]
directory = "tools"
state_path = "data/installed_tools.json"
enabled = []
disabled = []

[tools.terminal]
max_timeout = 120
max_output_chars = 12000
allow_outside_root = false

[tools.web]
# auto utilise Tavily si TAVILY_API_KEY existe, sinon la recherche publique.
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

[tools.github]
repo = "noan-hbt/orion-tools"
ref = "main"
timeout = 20

[ledger]
path = "data/action_ledger.sqlite3"

[scheduler]
enabled = true
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

[prompt]
core_path = "ORION_CORE.md"
context_path = "data/prompt_context.json"
journal_path = "data/conversations.jsonl"
history_enabled = true
history_limit = 20
history_max_chars = 12000
additional = ""

[context]
compaction_enabled = true
compactor_model = {_toml_string(compactor_model)}
total_max_chars = 60000
compactor_input_chars = 30000
cache_size = 64
task_max_chars = 12000
event_max_chars = 10000

[memory]
enabled = {str(memory).lower()}
model = {_toml_string(memory_model)}
run_at = "23:00"
batch_size = 20
poll_interval = 30.0
max_input_chars = 30000
'''


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
    secrets: dict[str, str] | None = None,
    force: bool = False,
) -> None:
    if config_path.exists() and not force:
        raise FileExistsError(f"{config_path} existe deja ; utilisez --force pour le remplacer.")
    backup_path = _backup_config(config_path) if config_path.exists() and force else None
    selected_model = model or _ask("Modele OpenRouter", "openai/gpt-5.6-luna")
    selected_compactor_model = compactor_model or _ask(
        "Modele de compaction du contexte",
        "deepseek/deepseek-v4-flash-0731",
    )
    selected_reflection_model = reflection_model or _ask(
        "Modele de pre-reflexion",
        "deepseek/deepseek-v4-flash-0731",
    )
    selected_key = api_key if api_key is not None else _ask_secret("Cle OPENROUTER_API_KEY")
    selected_channels = channels if channels is not None else [item.strip() for item in _ask("Channels actives (separes par des virgules)", "cli").split(",") if item.strip()]
    selected_memory = memory if memory is not None else _ask("Activer la memoire automatique ? oui/non", "non").lower() in {"oui", "o", "yes", "y"}
    selected_memory_model = memory_model or (
        _ask("Modele d'extraction de memoire", "deepseek/deepseek-v4-flash-0731")
        if selected_memory
        else "deepseek/deepseek-v4-flash-0731"
    )
    collected_secrets = dict(secrets or {})
    if "TAVILY_API_KEY" not in collected_secrets:
        collected_secrets["TAVILY_API_KEY"] = _ask_secret(
            "Cle Tavily (optionnelle ; laisser vide pour garder la recherche publique)"
        )
    secret_envs: dict[str, str] = {}
    for channel in selected_channels:
        default_env = CHANNEL_SECRET_DEFAULTS.get(channel)
        if default_env is None:
            continue
        env_name = default_env
        secret_envs[channel] = env_name
        if env_name not in collected_secrets:
            collected_secrets[env_name] = _ask_secret(f"Secret du channel {channel} ({env_name})")
    email_settings: dict[str, str] = {}
    if "email" in selected_channels:
        email_settings = {
            "imap_host": _ask("Serveur IMAP", "imap.example.com"),
            "smtp_host": _ask("Serveur SMTP", "smtp.example.com"),
            "username": _ask("Adresse email Orion"),
        }
    default_channel = selected_channels[0] if selected_channels else None
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        _config_text(
            selected_model,
            selected_channels,
            default_channel,
            selected_memory,
            secret_envs,
            email_settings,
            selected_compactor_model,
            selected_memory_model,
            selected_reflection_model,
        ),
        encoding="utf-8",
    )
    _write_env(env_path, {"OPENROUTER_API_KEY": selected_key, **collected_secrets})
    print(f"Configuration ecrite dans {config_path}")
    if backup_path is not None:
        print(f"Ancienne configuration sauvegardee dans {backup_path}")
    print(f"Secrets ecrits dans {env_path}" if selected_key or collected_secrets else "Cles API conservees/omises")


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure une installation Orion")
    parser.add_argument("--config", type=Path, default=Path("orion.toml"))
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--model")
    parser.add_argument("--compactor-model", help="Modele utilise pour compacter le contexte")
    parser.add_argument("--memory-model", help="Modele utilise pour extraire la memoire")
    parser.add_argument("--reflection-model", help="Modele utilise pour la pre-reflexion")
    parser.add_argument("--api-key")
    parser.add_argument("--channels", help="Liste separee par des virgules")
    parser.add_argument("--secret-env", action="append", default=[], metavar="NAME", help="Demande interactivement un secret de channel")
    parser.add_argument("--set-secret", action="append", default=[], metavar="NAME=VALUE", help="Ajoute un secret sans interaction")
    parser.add_argument("--memory", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    channels = [item.strip() for item in args.channels.split(",") if item.strip()] if args.channels else None
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
        secrets=secrets,
        force=args.force,
    )


if __name__ == "__main__":
    main()
