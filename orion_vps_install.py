"""Installe et prepare Orion pour un VPS headless.

Le script est non interactif par defaut : les secrets peuvent venir des
variables d'environnement, de secrets deja presents dans ``.env`` ou de
``--set-secret NAME=VALUE``. Il genere la configuration et, avec
``--systemd``, une unite de service a installer par l'administrateur.

Exemple :
    python orion_vps_install.py --channels telegram,discord --systemd \
        --set-secret OPENROUTER_API_KEY=... \
        --set-secret TELEGRAM_BOT_TOKEN=...
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path

from orion_install import (
    CHANNEL_SECRET_DEFAULTS,
    _backup_config,
    _config_text,
    _write_env,
)


SUPPORTED_CHANNELS = frozenset(CHANNEL_SECRET_DEFAULTS) | {"cli"}


def _env_values(path: Path) -> dict[str, str]:
    """Lit uniquement les noms/valeurs non vides deja presents dans .env."""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name and value:
            values[name] = value
    return values


def _parse_secrets(items: list[str], parser: argparse.ArgumentParser) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in items:
        name, separator, value = item.partition("=")
        if not separator or not name.strip() or not value:
            parser.error("--set-secret doit etre au format NAME=VALUE")
        values[name.strip()] = value
    return values


def _systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _service_text(
    *,
    service_name: str,
    install_dir: Path,
    config_path: Path,
    env_path: Path,
    python_executable: str,
    run_script: Path,
    user: str,
) -> str:
    return f"""[Unit]
Description=Orion Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
WorkingDirectory={_systemd_quote(str(install_dir))}
ExecStart={_systemd_quote(python_executable)} {_systemd_quote(str(run_script))} --config {_systemd_quote(str(config_path))}
EnvironmentFile={_systemd_quote(str(env_path))}
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"""


def _require_secret(
    name: str,
    *,
    values: dict[str, str],
    env_file_values: dict[str, str],
    parser: argparse.ArgumentParser,
) -> None:
    if values.get(name) or env_file_values.get(name) or os.getenv(name):
        return
    parser.error(
        f"Secret manquant : {name}. Utilisez --set-secret {name}=..., "
        f"une variable d'environnement ou renseignez .env."
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare une installation Orion headless pour un VPS"
    )
    parser.add_argument("--install-dir", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path)
    parser.add_argument("--env", type=Path)
    parser.add_argument("--channels", default="telegram", help="Ex: telegram,discord")
    parser.add_argument("--model", default="~openai/gpt-latest")
    parser.add_argument("--compactor-model", default="deepseek/deepseek-v4-flash-0731")
    parser.add_argument("--reflection-model", default="deepseek/deepseek-v4-flash-0731")
    parser.add_argument("--memory-model", default="deepseek/deepseek-v4-flash-0731")
    parser.add_argument("--memory", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--email-imap-host", default="imap.example.com")
    parser.add_argument("--email-smtp-host", default="smtp.example.com")
    parser.add_argument("--email-username", default="")
    parser.add_argument("--set-secret", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--systemd", action="store_true", help="Genere aussi orion.service")
    parser.add_argument("--service-name", default="orion")
    parser.add_argument("--service-user", default="orion")
    parser.add_argument("--python", dest="python_executable", default=sys.executable)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    channels = [item.strip().lower() for item in args.channels.split(",") if item.strip()]
    invalid = sorted(set(channels) - SUPPORTED_CHANNELS)
    if invalid:
        parser.error(f"Channels inconnus : {', '.join(invalid)}")
    if not channels:
        parser.error("Au moins un channel doit etre active sur le VPS.")

    install_dir = args.install_dir.expanduser().resolve()
    config_path = (args.config or install_dir / "orion.toml").expanduser().resolve()
    env_path = (args.env or install_dir / ".env").expanduser().resolve()
    install_dir.mkdir(parents=True, exist_ok=True)

    if config_path.exists() and not args.force:
        parser.error(f"{config_path} existe deja ; utilisez --force pour le remplacer.")
    if config_path.exists() and args.force:
        backup = _backup_config(config_path)
        print(f"Ancienne configuration sauvegardee dans {backup}")

    env_file_values = _env_values(env_path)
    secrets = _parse_secrets(args.set_secret, parser)
    selected_values = {**env_file_values, **secrets}
    for name in ("OPENROUTER_API_KEY",):
        _require_secret(name, values=selected_values, env_file_values=env_file_values, parser=parser)
    for channel in channels:
        env_name = CHANNEL_SECRET_DEFAULTS.get(channel)
        if env_name:
            _require_secret(env_name, values=selected_values, env_file_values=env_file_values, parser=parser)

    secret_envs = {
        channel: CHANNEL_SECRET_DEFAULTS[channel]
        for channel in channels
        if channel in CHANNEL_SECRET_DEFAULTS
    }
    email_settings = {
        "imap_host": args.email_imap_host,
        "smtp_host": args.email_smtp_host,
        "username": args.email_username,
    } if "email" in channels else None
    config_path.write_text(
        _config_text(
            args.model,
            channels,
            channels[0],
            bool(args.memory),
            secret_envs,
            email_settings,
            compactor_model=args.compactor_model,
            memory_model=args.memory_model,
            reflection_model=args.reflection_model,
        ),
        encoding="utf-8",
    )

    env_to_write = {
        name: value
        for name, value in {**selected_values, **os.environ, **secrets}.items()
        if name == "OPENROUTER_API_KEY" or name in secret_envs.values()
    }
    _write_env(env_path, env_to_write)
    if os.name == "posix":
        env_path.chmod(0o600)

    print(f"Configuration VPS ecrite dans {config_path}")
    print(f"Secrets ecrits dans {env_path} (permissions restreintes)")

    if args.systemd:
        service_path = install_dir / f"{args.service_name}.service"
        run_script = install_dir / "orion_run.py"
        service_path.write_text(
            _service_text(
                service_name=args.service_name,
                install_dir=install_dir,
                config_path=config_path,
                env_path=env_path,
                python_executable=args.python_executable,
                run_script=run_script,
                user=args.service_user,
            ),
            encoding="utf-8",
        )
        print(f"Unite systemd generee dans {service_path}")
        print(
            "Activation : sudo cp "
            f"{shlex.quote(str(service_path))} /etc/systemd/system/{args.service_name}.service "
            f"&& sudo systemctl daemon-reload && sudo systemctl enable --now {args.service_name}"
        )


if __name__ == "__main__":
    main()
