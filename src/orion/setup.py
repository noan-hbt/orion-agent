"""`orion setup`: install Orion by answering a few questions (D92).

Only what Orion cannot do itself is asked here: the secrets (they never go through a message)
and who the owner is. Each key is checked online before it is kept; the owner's Telegram id
is read from the message he sends to the bot, not typed. Everything else (modules, times,
models, the people who may write to Orion) is changed afterwards by talking to Orion.

What it writes:

- orion.toml (created, or edited in place with its comments kept: safe to run again);
- the secrets, one `NAME=value` per line, mode 0600, never in orion.toml: `core.env` holds
  the model's key; `guardian.env` the rest when Orion runs split (D28), so the core never sees
  them. `orion` loads these files at start when they sit beside orion.toml (on a server,
  systemd loads them from /etc/orion).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import httpx

from orion.configure.module import Change, apply_changes, validate

CORE_ENV = "core.env"
GUARDIAN_ENV = "guardian.env"
GUARDIAN_COMMANDS = ("guardian",)
"""Commands that run as the guardian: they read its secrets, never the core's."""
CORE_COMMANDS = ("serve", "chat")
"""Commands that run as the core: they never read the guardian's secrets."""

START = """# Orion. Written by `orion setup`; everything else is changed by talking to Orion.
# Every option is described in orion.example.toml. Secrets are never here (core.env,
# guardian.env).
instance = "orion-main"
data_dir = "data"
"""

OPENROUTER = "https://openrouter.ai/api/v1"
TELEGRAM = "https://api.telegram.org"
WAIT_FOR_OWNER = 300.0
"""Seconds to wait for the owner's first message to the bot."""


class SetupError(RuntimeError):
    pass


def read_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    found: dict[str, str] = {}
    for line in path.read_text("utf-8").splitlines():
        name, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#") and name.strip():
            found[name.strip()] = value.strip()
    return found


def write_env(path: Path, values: dict[str, str]) -> None:
    """Set these variables, keeping the file's other lines; mode 0600."""
    lines = path.read_text("utf-8").splitlines() if path.is_file() else []
    left = dict(values)
    kept: list[str] = []
    for line in lines:
        name = line.partition("=")[0].strip()
        if not line.lstrip().startswith("#") and name in left:
            kept.append(f"{name}={left.pop(name)}")
        else:
            kept.append(line)
    kept += [f"{name}={value}" for name, value in left.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text("\n".join(kept) + "\n", "utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def load_env_files(folder: Path, command: str) -> list[str]:
    """The secrets written by `orion setup` beside orion.toml, for this command: the core's
    commands never read the guardian's. Never overrides a variable already set. Returns the
    files read."""
    if command in GUARDIAN_COMMANDS:
        names = [GUARDIAN_ENV]
    elif command in CORE_COMMANDS:
        names = [CORE_ENV]
    else:
        names = [CORE_ENV, GUARDIAN_ENV]
    read: list[str] = []
    for name in names:
        path = folder / name
        values = read_env(path)
        if not values:
            continue
        for key, value in values.items():
            if value:
                os.environ.setdefault(key, value)
        read.append(str(path))
    return read


Ask = Callable[[str], str]
Say = Callable[[str], None]


@dataclass
class Setup:
    config: Path
    secrets: Path
    ask: Ask
    secret: Ask
    say: Say
    server: bool = False
    http: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=40.0))
    sleep: Callable[[float], None] = time.sleep
    changes: list[Change] = field(default_factory=list[Change])
    core_env: dict[str, str] = field(default_factory=dict[str, str])
    guardian_env: dict[str, str] = field(default_factory=dict[str, str])

    # -- the questions -------------------------------------------------------------------------

    def run(self) -> int:
        text = self.config.read_text("utf-8") if self.config.is_file() else START
        current = validate(text, self.config)
        self.say(
            "Installation d'Orion. Je ne demande que ce qu'il ne peut pas régler lui-même : "
            "les clés, et qui tu es. Le reste, tu le lui diras.\n"
        )
        names = current.memory.owner_names
        name = self.ask(f"Ton prénom{f' [{names[0]}]' if names else ''} : ").strip()
        if name:
            self.set("memory.owner_names", [name, *(n for n in names if n != name)])
        zone = self.ask(f"Ton fuseau horaire [{current.agent.timezone}] : ").strip()
        if zone:
            self.set("agent.timezone", zone)
        split = self.yes(
            "Garder les secrets dans un processus à part, le gardien (recommandé sur un serveur) ?",
            default=current.guardian.enabled or self.server,
        )
        self.set("guardian.enabled", split)
        self.openrouter(current.providers.openrouter.base_url)
        self.say("")
        if self.yes("Lui parler sur Telegram (gratuit) ?", default=True):
            self.telegram(split, current.channels.telegram.owner_id)
            if current.channels.imessage.enabled and not self.yes(
                "Garder aussi iMessage ?", default=False
            ):
                self.set("channels.imessage.enabled", False)
        self.finish(text, split)
        return 0

    def openrouter(self, base_url: str) -> None:
        known = self.known("OPENROUTER_API_KEY")
        self.say(
            "\nLes modèles passent par OpenRouter : crée une clé sur "
            "https://openrouter.ai/settings/keys (quelques euros de crédit suffisent)."
        )
        while True:
            key = self.secret(f"Clé OpenRouter{' [Entrée : garder]' if known else ''} : ")
            key = key.strip() or known
            if not key:
                self.say("Sans clé, Orion ne peut pas penser. Recommence quand tu l'as.")
                raise SetupError("no OpenRouter key")
            try:
                self.check_openrouter(base_url, key)
            except SetupError as exc:
                self.say(f"Cette clé ne marche pas : {exc}")
                continue
            self.say("Clé valide.")
            self.core_env["OPENROUTER_API_KEY"] = key
            return

    def telegram(self, split: bool, owner_id: int) -> None:
        held = self.guardian_env if split else self.core_env
        known = self.known("TELEGRAM_BOT_TOKEN")
        self.say(
            "\nSur Telegram, écris à @BotFather : /newbot, choisis un nom, puis copie le "
            "jeton qu'il te donne."
        )
        while True:
            token = self.secret(f"Jeton du bot{' [Entrée : garder]' if known else ''} : ")
            token = token.strip() or known
            if not token:
                self.say("Sans jeton, pas de Telegram : tu pourras relancer `orion setup`.")
                return
            try:
                bot = self.bot(token, "getMe")
            except SetupError as exc:
                self.say(f"Ce jeton ne marche pas : {exc}")
                continue
            break
        username = str(cast(dict[str, Any], bot).get("username", "?"))
        held["TELEGRAM_BOT_TOKEN"] = token
        self.set("channels.telegram.enabled", True)
        if owner_id and self.yes(
            f"Le bot @{username} répond déjà au compte {owner_id} : le garder ?", default=True
        ):
            return
        self.say(
            f"Bot @{username} trouvé. Écris-lui maintenant un message depuis ton téléphone "
            f"(https://t.me/{username}) : c'est comme ça qu'il saura que c'est toi. Orion ne "
            "doit pas tourner pendant ce temps."
        )
        found = self.first_writer(token)
        if found is None:
            self.say("Je n'ai rien reçu. Relance `orion setup` quand tu veux : le reste est gardé.")
            return
        user_id, first_name = found
        if not self.yes(f"C'est bien toi, {first_name} (id {user_id}) ?", default=True):
            self.say("Alors relance `orion setup` et écris au bot depuis ton propre compte.")
            return
        self.set("channels.telegram.owner_id", user_id)
        self.bot(
            token,
            "sendMessage",
            chat_id=user_id,
            text="C'est noté, c'est toi. Je démarre bientôt.",
        )

    def finish(self, text: str, split: bool) -> None:
        after = apply_changes(text, self.changes)
        validate(after, self.config)
        self.config.parent.mkdir(parents=True, exist_ok=True)
        if self.config.is_file() and after != text:
            self.config.with_name(f"{self.config.name}.previous").write_text(text, "utf-8")
        if after != text or not self.config.is_file():
            self.config.write_text(after, "utf-8")
        if not split:  # one process: it holds every secret
            self.core_env |= self.guardian_env
            self.guardian_env = {}
        if self.core_env:
            write_env(self.secrets / CORE_ENV, self.core_env)
        if self.guardian_env:
            write_env(self.secrets / GUARDIAN_ENV, self.guardian_env)
        self.say(f"\nC'est écrit : {self.config}, et les secrets dans {self.secrets}.")
        if self.server:
            self.say("Démarre-le : sudo systemctl restart orion-guardian orion-core")
        elif split:
            self.say(
                "Démarre-le (deux terminaux, ou deux services) :\n"
                f"  orion --config {self.config} guardian\n"
                f"  orion --config {self.config} serve"
            )
        else:
            self.say(f"Démarre-le : orion --config {self.config} serve")
        self.say(
            "Ensuite, dis-lui ce que tu veux : « active le mail », « ajoute Angèle », « mets "
            "tes points à 9 h »… Il change ses réglages lui-même, avec ton accord."
        )

    # -- helpers -------------------------------------------------------------------------------

    def set(self, path: str, value: Any) -> None:
        self.changes.append(Change(path=path, value=value))

    def yes(self, question: str, default: bool) -> bool:
        while True:
            answer = self.ask(f"{question} [{'O/n' if default else 'o/N'}] ").strip().lower()
            if not answer:
                return default
            if answer in ("o", "oui", "y", "yes"):
                return True
            if answer in ("n", "non", "no"):
                return False

    def known(self, name: str) -> str:
        """A secret already set: in the environment, or in a file setup wrote earlier."""
        for file in (CORE_ENV, GUARDIAN_ENV):
            value = read_env(self.secrets / file).get(name)
            if value:
                return value
        return os.environ.get(name, "")

    def check_openrouter(self, base_url: str, key: str) -> None:
        try:
            response = self.http.get(
                f"{base_url.rstrip('/')}/key", headers={"Authorization": f"Bearer {key}"}
            )
        except httpx.HTTPError as exc:
            raise SetupError(f"OpenRouter injoignable ({exc})") from exc
        if response.status_code != 200:
            raise SetupError(f"OpenRouter répond {response.status_code}")

    def bot(self, token: str, method: str, **params: Any) -> Any:
        try:
            response = self.http.post(f"{TELEGRAM}/bot{token}/{method}", json=params)
            data = cast(dict[str, Any], response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise SetupError(f"Telegram injoignable ({exc})") from exc
        if not data.get("ok"):
            raise SetupError(str(data.get("description", response.status_code)))
        return data.get("result")

    def first_writer(self, token: str) -> tuple[int, str] | None:
        """The first person to write to the bot in private, within WAIT_FOR_OWNER."""
        waited = 0.0
        offset = 0
        while waited < WAIT_FOR_OWNER:
            try:
                updates = self.bot(
                    token,
                    "getUpdates",
                    offset=offset,
                    timeout=20,
                    allowed_updates=["message"],
                )
            except SetupError as exc:
                self.say(f"({exc} : je réessaie)")
                self.sleep(5.0)
                waited += 5.0
                continue
            for update in cast(list[dict[str, Any]], updates or []):
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                message = cast(dict[str, Any], update.get("message") or {})
                chat = cast(dict[str, Any], message.get("chat") or {})
                sender = cast(dict[str, Any], message.get("from") or {})
                if chat.get("type") == "private" and sender.get("id"):
                    return int(sender["id"]), str(sender.get("first_name", "?"))
            waited += 20.0
        return None
