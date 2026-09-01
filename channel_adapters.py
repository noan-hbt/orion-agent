"""Adaptateurs de channels fournis avec Orion.

Chaque adaptateur depend uniquement du contrat de ``channels.py``. Les
integrations plus specifiques peuvent reutiliser ``HttpWebhookAdapter`` ou
implementer le meme contrat sans modifier le Core.
"""

from __future__ import annotations

import email
import html
import imaplib
import os
import re
import smtplib
import sys
import threading
from collections.abc import Callable, Mapping
from email.message import EmailMessage
from email.policy import default as email_policy
from email.utils import parseaddr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

from channels import AgentOutput, InboundMessage
from cli_ui import CLIConsole


MessageCallback = Callable[[InboundMessage], None]
CLIProvider = Callable[[], Any]


def markdown_to_telegram_html(text: str) -> str:
    """Convertit un sous-ensemble courant du Markdown vers Telegram HTML."""
    protected: dict[str, str] = {}

    def protect_markup(markup: str) -> str:
        token = f"\x00ORIONCODE{len(protected)}\x00"
        protected[token] = markup
        return token

    def protect_code_block(match: re.Match[str]) -> str:
        return protect_markup(
            f"<pre><code>{html.escape(match.group(1) or '', quote=False)}</code></pre>"
        )

    def protect_inline_code(match: re.Match[str]) -> str:
        return protect_markup(f"<code>{html.escape(match.group(1), quote=False)}</code>")

    # Protéger le code avant d'échapper et de transformer les marqueurs.
    text = re.sub(r"```(?:[^\n]*)\n(.*?)```", protect_code_block, text, flags=re.DOTALL)
    text = re.sub(r"`([^`\n]+)`", protect_inline_code, text)
    converted = html.escape(text, quote=False)

    converted = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        lambda match: f'<a href="{html.escape(html.unescape(match.group(2)), quote=True)}">{match.group(1)}</a>',
        converted,
    )
    converted = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", converted, flags=re.DOTALL)
    converted = re.sub(r"__((?s:.+?))__", r"<b>\1</b>", converted)
    converted = re.sub(r"~~([^~\n]+)~~", r"<s>\1</s>", converted)
    converted = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", converted)
    converted = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"<i>\1</i>", converted)

    for token, markup in protected.items():
        converted = converted.replace(token, markup)
    return converted


def split_telegram_message(text: str, *, max_chars: int = 3500) -> list[str]:
    """Découpe un message en conservant autant que possible ses lignes."""
    if max_chars < 1:
        raise ValueError("max_chars doit être positif.")
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(line) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            while len(line) > max_chars:
                chunks.append(line[:max_chars])
                line = line[max_chars:]
            current = line
        elif current and len(current) + len(line) > max_chars:
            chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks or [text[:max_chars]]


class CLIAdapter:
    """Adaptateur stdin/stdout, utile pour le developpement et le serveur CLI."""

    name = "cli"

    def __init__(
        self,
        *,
        prompt: str = "❯ ",
        output: Any = None,
        style: bool | None = None,
        banner: bool = True,
        name: str = "Orion",
        model: str | None = None,
        history_path: str | None = "data/cli_history.txt",
        markdown: bool = True,
        timestamps: bool = True,
    ) -> None:
        self.prompt = prompt
        self.output = output or sys.stdout
        self.console = CLIConsole(
            output=self.output,
            use_color=style,
            show_banner=banner,
            name=name,
            model=model,
            history_path=history_path,
            render_markdown=markdown,
            show_timestamps=timestamps,
        )
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_message: MessageCallback | None = None
        self._exit_handler: Callable[[], Any] | None = None
        self._status_provider: CLIProvider | None = None
        self._tools_provider: CLIProvider | None = None
        self._tasks_provider: CLIProvider | None = None
        self._agents_provider: CLIProvider | None = None
        self._jobs_provider: CLIProvider | None = None
        self._pending = 0
        self._pending_lock = threading.Lock()

    def set_exit_handler(self, handler: Callable[[], Any]) -> None:
        self._exit_handler = handler

    def set_status_provider(self, provider: CLIProvider) -> None:
        self._status_provider = provider

    def set_tools_provider(self, provider: CLIProvider) -> None:
        self._tools_provider = provider

    def set_tasks_provider(self, provider: CLIProvider) -> None:
        self._tasks_provider = provider

    def set_agents_provider(self, provider: CLIProvider) -> None:
        self._agents_provider = provider

    def set_jobs_provider(self, provider: CLIProvider) -> None:
        self._jobs_provider = provider

    def start(self, on_message: MessageCallback) -> None:
        self._on_message = on_message
        self._stop_requested.clear()
        self._thread = threading.Thread(target=self._run, name="orion-cli", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        self.console.banner()
        while not self._stop_requested.is_set():
            try:
                line = self.console.read(self.prompt)
            except KeyboardInterrupt:
                self.console.system("Saisie annulée.")
                continue
            except EOFError:
                self._request_exit()
                return
            line = line.strip()
            if not line:
                continue
            if self._handle_command(line):
                continue
            if self._on_message is not None:
                with self._pending_lock:
                    self._pending += 1
                    self.console.set_busy(True)
                self._on_message(
                    InboundMessage(
                        channel=self.name,
                        payload={"text": line},
                        reply_to="stdout",
                    )
                )

    def _request_exit(self) -> None:
        self._stop_requested.set()
        self.console.system("Arrêt d'Orion…")
        if self._exit_handler is not None:
            self._exit_handler()

    def _provider_value(self, provider: CLIProvider | None, fallback: Any) -> Any:
        if provider is None:
            return fallback
        try:
            return provider()
        except Exception as exc:
            self.console.error(f"Impossible de charger ces informations : {exc}")
            return fallback

    def _handle_command(self, line: str) -> bool:
        if not line.startswith("/"):
            return False
        command = line.split(maxsplit=1)[0].lower()
        if command == "/help":
            self.console.help()
        elif command == "/clear":
            self.console.clear()
        elif command in {"/exit", "/quit"}:
            self._request_exit()
        elif command == "/status":
            values = self._provider_value(
                self._status_provider,
                {"CLI": "active", "Requêtes en attente": self._pending},
            )
            self.console.status(values)
        elif command == "/tools":
            tools = self._provider_value(self._tools_provider, [])
            self.console.items("Tools disponibles", tools, empty="Aucun tool chargé.")
        elif command == "/tasks":
            tasks = self._provider_value(self._tasks_provider, [])
            self.console.items("Tâches récentes", tasks, empty="Aucune tâche durable.")
        elif command == "/agents":
            agents = self._provider_value(self._agents_provider, [])
            self.console.items("Sous-agents", agents, empty="Aucun sous-agent configuré.")
        elif command == "/jobs":
            jobs = self._provider_value(self._jobs_provider, [])
            self.console.items("Travaux délégués", jobs, empty="Aucun travail délégué.")
        else:
            self.console.warning(f"Commande inconnue : {command}. Utilisez /help.")
        return True

    def send(self, output: AgentOutput) -> None:
        intermediate = bool(output.metadata.get("intermediate", False))
        if not intermediate:
            with self._pending_lock:
                self._pending = max(0, self._pending - 1)
                self.console.set_busy(self._pending > 0)
        self.console.assistant(
            output.content,
            intermediate=intermediate,
            timestamp=output.metadata.get("timestamp"),
        )

    def report_error(self, event: object, error: Exception) -> None:
        with self._pending_lock:
            self._pending = max(0, self._pending - 1)
            self.console.set_busy(self._pending > 0)
        event_type = getattr(event, "type", "unknown")
        self.console.error(f"{type(error).__name__} · événement {event_type}\n{error}")

    def stop(self) -> None:
        self._stop_requested.set()


class HttpWebhookAdapter:
    """Serveur HTTP generique pour webhooks entrants et sorties JSON."""

    def __init__(
        self,
        *,
        name: str = "web",
        host: str = "127.0.0.1",
        port: int = 8080,
        path: str = "/webhook",
        auth_token: str | None = None,
        outbound_url: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.name = name
        self.host = host
        self.port = int(port)
        self.path = path if path.startswith("/") else f"/{path}"
        self.auth_token = auth_token
        self.outbound_url = outbound_url
        self.timeout = timeout
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._on_message: MessageCallback | None = None

    def start(self, on_message: MessageCallback) -> None:
        self._on_message = on_message
        adapter = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                if self.path != adapter.path:
                    self.send_error(404)
                    return
                if adapter.auth_token and self.headers.get("X-Orion-Token") != adapter.auth_token:
                    self.send_error(401)
                    return
                try:
                    import json

                    length = int(self.headers.get("Content-Length", "0"))
                    raw = self.rfile.read(length)
                    payload = json.loads(raw.decode("utf-8"))
                    if not isinstance(payload, dict):
                        payload = {"data": payload}
                    adapter.receive_payload(
                        payload,
                        reply_to=payload.get("reply_to") or self.headers.get("X-Orion-Reply-To"),
                    )
                except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    self.send_error(400, str(exc))
                    return
                self.send_response(202)
                self.end_headers()

            def log_message(self, *_: Any) -> None:
                return

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name=f"orion-{self.name}", daemon=True)
        self._thread.start()

    def receive_payload(self, payload: Mapping[str, Any], *, reply_to: str | None = None) -> None:
        if self._on_message is None:
            raise RuntimeError(f"Le channel {self.name} n'est pas demarre.")
        self._on_message(
            InboundMessage(
                channel=self.name,
                payload=dict(payload),
                reply_to=reply_to,
                source=self.name,
            )
        )

    def send(self, output: AgentOutput) -> None:
        url = output.recipient or output.metadata.get("reply_to") or self.outbound_url
        if not url:
            raise RuntimeError(f"Aucune URL de sortie pour le channel {self.name}.")
        response = httpx.post(
            url,
            json={"text": output.content, "content": output.content, "task_id": output.task_id},
            timeout=self.timeout,
        )
        response.raise_for_status()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


class TelegramAdapter:
    """Adaptateur Telegram Bot API base sur long polling."""

    name = "telegram"

    def __init__(
        self,
        token: str,
        *,
        poll_timeout: int = 25,
        allowed_chat_ids: list[int] | None = None,
        api_timeout: float = 35.0,
        parse_mode: str | None = "HTML",
        max_message_chars: int = 3500,
    ) -> None:
        if not token:
            raise ValueError("Le token Telegram est obligatoire.")
        self.token = token
        self.poll_timeout = poll_timeout
        self.allowed_chat_ids = {int(item) for item in (allowed_chat_ids or [])}
        if parse_mode not in {None, "HTML", "MarkdownV2"}:
            raise ValueError("parse_mode Telegram doit être HTML, MarkdownV2 ou null.")
        if max_message_chars < 500 or max_message_chars > 4096:
            raise ValueError("max_message_chars Telegram doit être compris entre 500 et 4096.")
        self.parse_mode = parse_mode
        self.max_message_chars = int(max_message_chars)
        self.client = httpx.Client(base_url=f"https://api.telegram.org/bot{token}", timeout=api_timeout)
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_message: MessageCallback | None = None
        self._offset = 0

    def _api(self, method: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        response = self.client.post(f"/{method}", json=dict(payload))
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text[:1000].replace("\n", " ")
            raise httpx.HTTPStatusError(
                f"Telegram HTTP {response.status_code}: {detail}",
                request=exc.request,
                response=exc.response,
            ) from exc
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data}")
        return data

    def start(self, on_message: MessageCallback) -> None:
        self._on_message = on_message
        self._stop_requested.clear()
        self._thread = threading.Thread(target=self._run, name="orion-telegram", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_requested.is_set():
            try:
                data = self._api("getUpdates", {"offset": self._offset, "timeout": self.poll_timeout})
                for update in data.get("result", []):
                    self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
                    message = update.get("message") or update.get("edited_message") or {}
                    chat = message.get("chat") or {}
                    chat_id = chat.get("id")
                    if chat_id is None or (self.allowed_chat_ids and int(chat_id) not in self.allowed_chat_ids):
                        continue
                    text = message.get("text")
                    if not isinstance(text, str) or self._on_message is None:
                        continue
                    self._on_message(
                        InboundMessage(
                            channel=self.name,
                            payload={
                                "text": text,
                                "chat_id": chat_id,
                                "user_id": (message.get("from") or {}).get("id"),
                                "username": (message.get("from") or {}).get("username"),
                                "raw": update,
                            },
                            reply_to=str(chat_id),
                            source=self.name,
                            metadata={"chat_id": chat_id},
                        )
                    )
            except (httpx.HTTPError, RuntimeError, ValueError):
                if not self._stop_requested.wait(2.0):
                    continue

    def send(self, output: AgentOutput) -> None:
        chat_id = output.recipient or output.metadata.get("chat_id") or output.metadata.get("reply_to")
        if chat_id is None:
            raise RuntimeError("Aucun chat_id Telegram pour cette sortie.")
        for chunk in split_telegram_message(output.content, max_chars=self.max_message_chars):
            text = markdown_to_telegram_html(chunk) if self.parse_mode == "HTML" else chunk
            payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
            if self.parse_mode:
                payload["parse_mode"] = self.parse_mode
            try:
                self._api("sendMessage", payload)
            except httpx.HTTPStatusError as exc:
                # Un découpage au milieu d'un bloc Markdown peut produire un
                # HTML incomplet. La réponse doit tout de même parvenir à
                # l'utilisateur plutôt que de perdre tout le RUN.
                if self.parse_mode != "HTML" or exc.response.status_code != 400:
                    raise
                self._api(
                    "sendMessage",
                    {"chat_id": chat_id, "text": chunk},
                )

    def stop(self) -> None:
        self._stop_requested.set()
        if self._thread is not None:
            self._thread.join(timeout=self.poll_timeout + 2)
            self._thread = None
        self.client.close()


class EmailAdapter:
    """Adaptateur email IMAP entrant et SMTP sortant."""

    name = "email"

    def __init__(
        self,
        *,
        imap_host: str,
        smtp_host: str,
        username: str,
        password: str,
        imap_port: int = 993,
        smtp_port: int = 465,
        mailbox: str = "INBOX",
        poll_interval: float = 60.0,
        subject: str = "Orion",
        smtp_starttls: bool = False,
    ) -> None:
        if not username or not password:
            raise ValueError("username et password sont obligatoires pour EmailAdapter.")
        self.imap_host = imap_host
        self.smtp_host = smtp_host
        self.username = username
        self.password = password
        self.imap_port = imap_port
        self.smtp_port = smtp_port
        self.mailbox = mailbox
        self.poll_interval = poll_interval
        self.subject = subject
        self.smtp_starttls = smtp_starttls
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_message: MessageCallback | None = None

    def start(self, on_message: MessageCallback) -> None:
        self._on_message = on_message
        self._stop_requested.clear()
        self._thread = threading.Thread(target=self._run, name="orion-email", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_requested.is_set():
            try:
                self._poll_once()
            except (OSError, imaplib.IMAP4.error):
                pass
            self._stop_requested.wait(self.poll_interval)

    def _poll_once(self) -> None:
        connection = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
        try:
            connection.login(self.username, self.password)
            connection.select(self.mailbox)
            status, data = connection.search(None, "UNSEEN")
            if status != "OK":
                return
            for message_id in data[0].split():
                status, fetched = connection.fetch(message_id, "(RFC822)")
                if status != "OK" or not fetched:
                    continue
                raw = next((item[1] for item in fetched if isinstance(item, tuple)), b"")
                message = email.message_from_bytes(raw, policy=email_policy)
                body = self._body(message)
                sender = parseaddr(message.get("From", ""))[1]
                payload = {
                    "from": sender,
                    "to": message.get("To", ""),
                    "subject": str(message.get("Subject", "")),
                    "body": body,
                    "message_id": message.get("Message-ID"),
                }
                if self._on_message is not None:
                    self._on_message(
                        InboundMessage(
                            channel=self.name,
                            event_type="email",
                            payload=payload,
                            reply_to=sender,
                            source=self.name,
                        )
                    )
                connection.store(message_id, "+FLAGS", "(\\Seen)")
        finally:
            try:
                connection.logout()
            except OSError:
                pass

    @staticmethod
    def _body(message: email.message.Message) -> str:
        if message.is_multipart():
            for part in message.walk():
                if part.get_content_type() == "text/plain" and not part.get("Content-Disposition", "").startswith("attachment"):
                    return part.get_content()
        return message.get_content() if message.get_content_type() == "text/plain" else ""

    def send(self, output: AgentOutput) -> None:
        recipient = output.recipient or output.metadata.get("reply_to")
        if not recipient:
            raise RuntimeError("Aucun destinataire email pour cette sortie.")
        message = EmailMessage()
        message["From"] = self.username
        message["To"] = str(recipient)
        message["Subject"] = str(output.metadata.get("subject") or self.subject)
        message.set_content(output.content)
        if self.smtp_starttls:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as connection:
                connection.starttls()
                connection.login(self.username, self.password)
                connection.send_message(message)
        else:
            with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=30) as connection:
                connection.login(self.username, self.password)
                connection.send_message(message)

    def stop(self) -> None:
        self._stop_requested.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.poll_interval + 1.0))
            self._thread = None


class DiscordWebhookAdapter:
    """Adaptateur sortant Discord webhook et point d'injection entrant."""

    name = "discord"

    def __init__(self, webhook_url: str, *, timeout: float = 20.0) -> None:
        if not webhook_url:
            raise ValueError("webhook_url est obligatoire pour DiscordWebhookAdapter.")
        self.webhook_url = webhook_url
        self.timeout = timeout
        self._on_message: MessageCallback | None = None

    def start(self, on_message: MessageCallback) -> None:
        self._on_message = on_message

    def receive_payload(self, payload: Mapping[str, Any], *, reply_to: str | None = None) -> None:
        if self._on_message is None:
            raise RuntimeError("Le channel Discord n'est pas demarre.")
        self._on_message(
            InboundMessage(
                channel=self.name,
                payload=dict(payload),
                reply_to=reply_to or self.webhook_url,
                source=self.name,
            )
        )

    def send(self, output: AgentOutput) -> None:
        response = httpx.post(
            output.recipient or self.webhook_url,
            json={"content": output.content[:2000]},
            timeout=self.timeout,
        )
        response.raise_for_status()

    def stop(self) -> None:
        self._on_message = None


def secret_from_env(name: str) -> str:
    """Charge un secret de channel sans l'ecrire dans la configuration."""
    value = os.getenv(name)
    if not value:
        raise ValueError(f"La variable d'environnement {name} est absente.")
    return value


__all__ = [
    "CLIAdapter",
    "DiscordWebhookAdapter",
    "EmailAdapter",
    "HttpWebhookAdapter",
    "TelegramAdapter",
    "secret_from_env",
]
