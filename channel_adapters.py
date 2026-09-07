"""Adaptateurs de channels fournis avec Orion.

Chaque adaptateur depend uniquement du contrat de ``channels.py``. Les
integrations plus specifiques peuvent reutiliser ``HttpWebhookAdapter`` ou
implementer le meme contrat sans modifier le Core.
"""

from __future__ import annotations

import email
import hashlib
import hmac
import html
import imaplib
import json
import os
import queue
import re
import smtplib
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from email.message import EmailMessage
from email.policy import default as email_policy
from email.utils import parseaddr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

import httpx

from channels import AgentOutput, InboundMessage
from cli_ui import CLICommand, CLICommandParser, CLIConsole, CLIParseError, CLIUserInput
from communication_ledger import CommunicationLedger


MessageCallback = Callable[[InboundMessage], None]
CLIProvider = Callable[[], Any]

MAX_WEBHOOK_BODY_BYTES = 256 * 1024
MAX_CHANNEL_QUEUE = 1000


def _is_loopback_host(host: str) -> bool:
    """Retourne si l'adresse de liaison est explicitement locale.

    La résolution DNS n'est volontairement pas utilisée : une configuration
    publique ne doit pas devenir locale selon l'état du résolveur.
    """
    normalized = host.strip().lower().strip("[]")
    return normalized in {"127.0.0.1", "::1", "localhost"}


def _validate_http_url(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(str(url))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("La destination doit être une URL HTTP(S) valide.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Les identifiants URL sont interdits.")
    if parsed.fragment:
        raise ValueError("Les fragments URL sont interdits pour une destination HTTP.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Le port URL est invalide.") from exc
    return parsed.scheme, parsed.hostname.lower(), port


def _url_matches_allowlist(url: str, allowlist: Iterable[str]) -> bool:
    scheme, hostname, port = _validate_http_url(url)
    for candidate in allowlist:
        value = str(candidate).strip()
        if not value:
            continue
        if "://" in value:
            try:
                allowed_scheme, allowed_host, allowed_port = _validate_http_url(value)
            except ValueError:
                continue
            if (scheme, hostname, port) == (allowed_scheme, allowed_host, allowed_port):
                return True
        else:
            candidate_host = value.lower().strip("[]")
            if hostname == candidate_host:
                return True
    return False


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
        slow_request_seconds: float = 15.0,
        usage_provider: Any = None,
        usage_ledger: Any = None,
        cost_provider: Any = None,
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
            usage_provider=usage_provider,
            usage_ledger=usage_ledger,
            cost_provider=cost_provider,
        )
        # Keep command grammar in the shared parser. The adapter only maps
        # parsed commands to local actions/providers.
        self._command_parser = CLICommandParser()
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_message: MessageCallback | None = None
        self._exit_handler: Callable[[], Any] | None = None
        self._status_provider: CLIProvider | None = None
        self._tools_provider: CLIProvider | None = None
        self._tasks_provider: CLIProvider | None = None
        self._agents_provider: CLIProvider | None = None
        self._jobs_provider: CLIProvider | None = None
        self._usage_provider: Any = usage_provider or usage_ledger or cost_provider
        self._pending = 0
        self._pending_lock = threading.Lock()
        self._pending_requests: list[dict[str, Any]] = []
        self._request_sequence = 0
        self.slow_request_seconds = max(0.0, float(slow_request_seconds))
        self._slow_alert_stop = threading.Event()
        self._slow_alert_thread: threading.Thread | None = None

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

    def set_usage_provider(self, provider: Any = None) -> None:
        """Expose the session usage ledger to the console when available."""
        self._usage_provider = provider
        self.console.set_usage_provider(provider)

    # Compatibility alias for integrations that call the aggregate a cost
    # provider rather than a usage provider.
    set_usage_ledger = set_usage_provider
    set_cost_provider = set_usage_provider

    def start(self, on_message: MessageCallback) -> None:
        self._on_message = on_message
        self._stop_requested.clear()
        self._slow_alert_stop.clear()
        self._thread = threading.Thread(target=self._run, name="orion-cli", daemon=True)
        self._thread.start()
        if self.slow_request_seconds > 0:
            self._slow_alert_thread = threading.Thread(
                target=self._slow_request_loop,
                name="orion-cli-slow-alert",
                daemon=True,
            )
            self._slow_alert_thread.start()

    def _begin_pending(self, text: str = "") -> int:
        request = self.console.new_request(text)
        # La demande est réellement remise au router juste après sa création.
        # ``running`` permet au tracker de distinguer une saisie en attente
        # d'une requête déjà publiée.
        self.console.requests.update(request.request_id, "running")
        with self._pending_lock:
            self._request_sequence += 1
            sequence = self._request_sequence
            self._pending_requests.append(
                {
                    "sequence": sequence,
                    "request_id": request.request_id,
                    "event_id": None,
                    "started_at": time.monotonic(),
                    "alerted": False,
                }
            )
            self._pending = len(self._pending_requests)
            self.console.set_busy(True)
            return sequence

    def _bind_pending_event(self, sequence: int, event: Any) -> None:
        event_id = getattr(event, "id", None)
        if not event_id:
            return
        with self._pending_lock:
            for request in self._pending_requests:
                if request["sequence"] == sequence:
                    request["event_id"] = str(event_id)
                    return

    def _finish_pending(
        self,
        event_id: str | None = None,
        *,
        state: str = "succeeded",
        error: str | None = None,
    ) -> None:
        with self._pending_lock:
            index = None
            if event_id:
                for position, request in enumerate(self._pending_requests):
                    if request.get("event_id") == str(event_id):
                        index = position
                        break
                if index is None:
                    # Une sortie asynchrone (scheduler, sous-agent, etc.) ne
                    # doit pas terminer arbitrairement une requête CLI.
                    return
            elif self._pending_requests:
                # Repli pour les callbacks qui ne renvoient pas d'Event.
                index = 0
            if index is not None:
                request = self._pending_requests.pop(index)
            self._pending = len(self._pending_requests)
            self.console.set_busy(bool(self._pending_requests))
        if index is not None:
            try:
                self.console.requests.update(
                    request["request_id"], state, error=error
                )
            except (KeyError, ValueError, RuntimeError):
                # Une requête peut avoir été annulée pendant qu'une sortie
                # réseau arrivait. Son état terminal reste alors ``canceled``.
                pass

    def _cancel_pending(self, request_id: str | None) -> None:
        if not request_id:
            return
        with self._pending_lock:
            self._pending_requests = [
                item
                for item in self._pending_requests
                if item.get("request_id") != request_id
            ]
            self._pending = len(self._pending_requests)
            self.console.set_busy(bool(self._pending_requests))

    def _slow_request_loop(self) -> None:
        """Alerte une fois lorsqu'une requête semble anormalement lente."""
        interval = min(0.5, max(0.1, self.slow_request_seconds / 4))
        while not self._slow_alert_stop.wait(interval):
            now = time.monotonic()
            should_alert = False
            with self._pending_lock:
                for request in self._pending_requests:
                    if (
                        not request["alerted"]
                        and now - request["started_at"] >= self.slow_request_seconds
                    ):
                        request["alerted"] = True
                        should_alert = True
            if should_alert:
                self.console.warning(
                    "Orion travaille toujours… OpenRouter met plus de temps que prévu. "
                    "Patiente encore un peu avant de renvoyer la demande."
                )

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
            self._submit_text(line)

    def _submit_text(self, text: str) -> None:
        """Publie un texte sans laisser une exception de callback tuer la CLI."""
        if self._on_message is None:
            return
        sequence = self._begin_pending(text)
        try:
            with self._pending_lock:
                request = next(
                    item
                    for item in self._pending_requests
                    if item["sequence"] == sequence
                )
            tracked = self.console.requests.get(request["request_id"])
            correlation_id = tracked.correlation_id if tracked is not None else None
            event = self._on_message(
                InboundMessage(
                    channel=self.name,
                    payload={"text": text},
                    reply_to="stdout",
                    correlation_id=correlation_id,
                )
            )
            self._bind_pending_event(sequence, event)
        except Exception as exc:
            self._finish_pending(
                state="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            self.console.error(f"Impossible d'envoyer la requête : {exc}")

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
        """Parse and dispatch one local command.

        The parser owns quoting, aliases, arity and option validation.  This
        method only translates the canonical command into an existing console
        action/provider, keeping malformed slash input local to the CLI.
        """
        try:
            parsed = self._command_parser.parse(line)
        except CLIParseError as exc:
            self.console.error(str(exc))
            return True
        if isinstance(parsed, CLIUserInput):
            return False

        command: CLICommand = parsed
        name = command.name
        if name == "help":
            self.console.help()
        elif name == "clear":
            self.console.clear()
        elif name == "stop":
            target = command.args[0] if command.args else None
            if target == "all":
                with self._pending_lock:
                    request_ids = [item["request_id"] for item in self._pending_requests]
                for request_id in request_ids:
                    self.console.requests.cancel(request_id)
                    self._cancel_pending(request_id)
                if request_ids:
                    self.console.system(f"{len(request_ids)} requête(s) annulée(s).")
                else:
                    self.console.warning("Aucune requête active à arrêter.")
            else:
                canceled = self.console.requests.cancel(target)
                if canceled is None:
                    self.console.warning("Aucune requête active à arrêter.")
                else:
                    self._cancel_pending(canceled.request_id)
                    self.console.system(f"Requête {canceled.request_id} annulée.")
        elif name == "exit":
            self._request_exit()
        elif name == "status":
            values = self._provider_value(
                self._status_provider,
                {"CLI": "active", "Requêtes en attente": self._pending},
            )
            self.console.status(values)
        elif name == "tools":
            values = self._filter_items(
                self._provider_value(self._tools_provider, []),
                command.args[0] if command.args else None,
            )
            self.console.items("Tools disponibles", values, empty="Aucun tool chargé.")
        elif name == "tasks":
            values = self._filter_items(
                self._provider_value(self._tasks_provider, []),
                command.args[0] if command.args else None,
            )
            self.console.items("Tâches récentes", values, empty="Aucune tâche durable.")
        elif name == "agents":
            values = self._provider_value(self._agents_provider, [])
            self.console.items("Sous-agents", values, empty="Aucun sous-agent configuré.")
        elif name == "jobs":
            values = self._filter_items(
                self._provider_value(self._jobs_provider, []),
                command.args[0] if command.args else None,
            )
            self.console.items("Travaux délégués", values, empty="Aucun travail délégué.")
        elif name == "requests":
            values = self._filter_items(
                self.console.requests.snapshot(),
                command.args[0] if command.args else None,
            )
            self.console.items("Requêtes récentes", values, empty="Aucune requête récente.")
        elif name == "retry":
            request = self.console.requests.get(command.args[0])
            if request is None or not request.text:
                self.console.error(f"Requête introuvable : {command.args[0]}")
            elif request.state not in {"failed", "canceled"}:
                self.console.warning("Seules les requêtes échouées ou annulées peuvent être relancées.")
            else:
                self._submit_text(request.text)
        elif name == "debug":
            self.console.status(
                {"Requêtes en attente": self._pending, "Arrêt demandé": self._stop_requested.is_set()}
            )
        return True

    @staticmethod
    def _filter_items(items: Any, identifier: str | None) -> list[Any]:
        if identifier is None:
            return list(items) if isinstance(items, (list, tuple)) else []
        if not isinstance(items, (list, tuple)):
            return []
        wanted = str(identifier).lower()
        result: list[Any] = []
        for item in items:
            values = (
                (
                    item.get("id"),
                    item.get("request_id"),
                    item.get("name"),
                    item.get("label"),
                )
                if isinstance(item, Mapping)
                else (item,)
            )
            if any(value is not None and str(value).lower() == wanted for value in values):
                result.append(item)
        return result

    def send(self, output: AgentOutput) -> None:
        intermediate = bool(output.metadata.get("intermediate", False))
        if not intermediate:
            self._finish_pending(output.event_id)
        elif output.event_id:
            with self._pending_lock:
                request = next(
                    (
                        item
                        for item in self._pending_requests
                        if item.get("event_id") == str(output.event_id)
                    ),
                    None,
                )
            if request is not None:
                try:
                    self.console.requests.update(request["request_id"], "streaming")
                except (KeyError, ValueError, RuntimeError):
                    pass
        self.console.assistant(
            output.content,
            intermediate=intermediate,
            timestamp=output.metadata.get("timestamp"),
        )

    def report_error(self, event: object, error: Exception) -> None:
        self._finish_pending(
            getattr(event, "id", None),
            state="failed",
            error=f"{type(error).__name__}: {error}",
        )
        event_type = getattr(event, "type", "unknown")
        self.console.error(f"{type(error).__name__} · événement {event_type}\n{error}")

    def stop(self) -> None:
        self._stop_requested.set()
        self._slow_alert_stop.set()
        self.console.stop()
        with self._pending_lock:
            self._pending_requests.clear()
            self._pending = 0
            self.console.set_busy(False)
        current = threading.current_thread()
        if self._thread is not None and self._thread is not current:
            self._thread.join(timeout=1.0)
        if self._slow_alert_thread is not None and self._slow_alert_thread is not current:
            self._slow_alert_thread.join(timeout=1.0)


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
        hmac_secret: str | None = None,
        outbound_url: str | None = None,
        outbound_allowlist: Iterable[str] | None = None,
        timeout: float = 20.0,
        request_timeout: float = 10.0,
        max_body_bytes: int = MAX_WEBHOOK_BODY_BYTES,
        queue_size: int = MAX_CHANNEL_QUEUE,
        allowlist: Iterable[str] | None = None,
        ledger: CommunicationLedger | None = None,
    ) -> None:
        self.name = name
        self.host = host
        self.port = int(port)
        self.path = path if path.startswith("/") else f"/{path}"
        self.auth_token = auth_token
        self.hmac_secret = hmac_secret
        self.outbound_url = outbound_url
        self.timeout = float(timeout)
        self.request_timeout = float(request_timeout)
        self.max_body_bytes = int(max_body_bytes)
        self.queue_size = int(queue_size)
        self.source_allowlist = tuple(str(item).strip() for item in (allowlist or ()) if str(item).strip())
        self.ledger = ledger
        if self.max_body_bytes < 1 or self.max_body_bytes > MAX_WEBHOOK_BODY_BYTES:
            raise ValueError("max_body_bytes doit être compris entre 1 et 256 KiB.")
        if self.queue_size < 1 or self.queue_size > MAX_CHANNEL_QUEUE:
            raise ValueError("queue_size doit être compris entre 1 et 1000.")
        if self.timeout <= 0 or self.request_timeout <= 0:
            raise ValueError("Les timeouts HTTP doivent être positifs.")
        if not (self.auth_token or self.hmac_secret) and not _is_loopback_host(self.host):
            raise ValueError("Un webhook hors loopback doit utiliser un token ou une signature HMAC.")
        if self.outbound_url:
            _validate_http_url(self.outbound_url)
        self.outbound_allowlist = tuple(str(item) for item in (outbound_allowlist or ()))
        if self.outbound_url and not self.outbound_allowlist:
            # La configuration historique outbound_url devient explicitement
            # l'allowlist de sortie, sans ouvrir d'autres destinations.
            self.outbound_allowlist = (self.outbound_url,)
        for candidate in self.outbound_allowlist:
            if "://" in candidate:
                _validate_http_url(candidate)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._on_message: MessageCallback | None = None
        self._queue: queue.Queue[InboundMessage | None] = queue.Queue(maxsize=self.queue_size)
        self._worker: threading.Thread | None = None
        self._stop_requested = threading.Event()
        self._idempotency_lock = threading.Lock()
        self._idempotency: OrderedDict[str, str] = OrderedDict()

    def _worker_loop(self) -> None:
        while not self._stop_requested.is_set():
            try:
                message = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if message is None:
                    return
                callback = self._on_message
                if callback is not None:
                    callback(message)
                    if self.ledger is not None:
                        self.ledger.ack(message.message_id or "")
            except Exception:
                # Une erreur de traitement ne doit pas tuer le lecteur HTTP.
                continue
            finally:
                self._queue.task_done()

    @staticmethod
    def _error(handler: BaseHTTPRequestHandler, status: int, code: str, message: str) -> None:
        body = json.dumps({"error": code, "message": message}, ensure_ascii=False).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        if status == 429:
            handler.send_header("Retry-After", "1")
        handler.end_headers()
        try:
            handler.wfile.write(body)
        except OSError:
            pass

    def _authenticate(self, raw: bytes, headers: Mapping[str, str]) -> bool:
        if self.auth_token:
            supplied = headers.get("X-Orion-Token", "")
            if not supplied:
                supplied = headers.get("Authorization", "")
                if supplied.lower().startswith("bearer "):
                    supplied = supplied[7:]
            return hmac.compare_digest(str(supplied), self.auth_token)
        if self.hmac_secret:
            supplied = headers.get("X-Orion-Signature", "") or headers.get("X-Hub-Signature-256", "")
            if supplied.startswith("sha256="):
                supplied = supplied[7:]
            expected = hmac.new(self.hmac_secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
            return hmac.compare_digest(supplied, expected)
        # Le seul mode sans secret est la liaison loopback, validée au démarrage.
        return _is_loopback_host(self.host)

    def _remember_idempotency(self, key: str, digest: str) -> str:
        with self._idempotency_lock:
            previous = self._idempotency.get(key)
            if previous is not None:
                self._idempotency.move_to_end(key)
                return "same" if previous == digest else "conflict"
            self._idempotency[key] = digest
            while len(self._idempotency) > self.queue_size:
                self._idempotency.popitem(last=False)
        return "new"

    def _handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        if urlsplit(handler.path).path != self.path:
            self._error(handler, 404, "not_found", "Ressource introuvable.")
            return
        if not callable(self._on_message):
            # Consume a bounded request body before replying.  Otherwise
            # clients that sent a non-empty POST can observe a reset while
            # the server closes a connection with unread bytes.
            try:
                length = max(0, int(handler.headers.get("Content-Length", "0")))
                handler.connection.settimeout(self.request_timeout)
                while length:
                    chunk = handler.rfile.read(min(64 * 1024, length))
                    if not chunk:
                        break
                    length -= len(chunk)
            except (OSError, TimeoutError, ValueError):
                pass
            self._error(handler, 503, "dependency_unavailable", "Le callback du channel est indisponible.")
            return
        if self.source_allowlist:
            source = str(handler.client_address[0])
            if source not in self.source_allowlist and "*" not in self.source_allowlist:
                self._error(handler, 403, "forbidden", "La source du gateway n'est pas autorisee.")
                return
        content_type = handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            try:
                remaining = max(0, int(handler.headers.get("Content-Length", "0")))
                handler.connection.settimeout(self.request_timeout)
                while remaining:
                    chunk = handler.rfile.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except (OSError, TimeoutError, ValueError):
                pass
            self._error(handler, 415, "unsupported_media_type", "Le corps doit être en application/json.")
            return
        raw_length = handler.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else -1
        except ValueError:
            length = -1
        if length < 0:
            self._error(handler, 400, "invalid_request", "Content-Length est obligatoire.")
            return
        if length > self.max_body_bytes:
            # Drainer le flux déclaré évite de réinitialiser la connexion du
            # client avant l'envoi du 413, sans conserver le corps en mémoire.
            try:
                handler.connection.settimeout(self.request_timeout)
                remaining = length
                while remaining:
                    chunk = handler.rfile.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except (OSError, TimeoutError):
                pass
            self._error(handler, 413, "body_too_large", "Le corps dépasse la limite autorisée.")
            return
        try:
            handler.connection.settimeout(self.request_timeout)
            raw = handler.rfile.read(length)
        except (OSError, TimeoutError):
            self._error(handler, 400, "invalid_request", "Corps HTTP incomplet.")
            return
        if len(raw) != length:
            self._error(handler, 400, "invalid_request", "Corps HTTP incomplet.")
            return
        if not self._authenticate(raw, handler.headers):
            self._error(handler, 401, "unauthorized", "Authentification invalide.")
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(handler, 400, "invalid_json", "Le corps doit être un JSON valide.")
            return
        if not isinstance(payload, dict):
            self._error(handler, 400, "invalid_schema", "Le JSON racine doit être un objet.")
            return
        reply_to = payload.get("reply_to") or handler.headers.get("X-Orion-Reply-To")
        if reply_to is not None and (not isinstance(reply_to, str) or len(reply_to) > 2048):
            self._error(handler, 400, "invalid_schema", "reply_to doit être un handle opaque valide.")
            return
        key = handler.headers.get("Idempotency-Key") or handler.headers.get("X-Idempotency-Key")
        digest = hashlib.sha256(raw).hexdigest()
        if key:
            state = self._remember_idempotency(key, digest)
            if state == "conflict":
                self._error(handler, 409, "idempotency_conflict", "Cette clé a déjà été utilisée avec un contenu différent.")
                return
            if state == "same":
                self._respond_accepted(handler)
                return
        if not self.receive_payload(payload, reply_to=reply_to, enqueue_only=True, idempotency_key=key):
            if key:
                with self._idempotency_lock:
                    self._idempotency.pop(key, None)
            self._error(handler, 429, "queue_full", "La file du channel est pleine.")
            return
        self._respond_accepted(handler)

    @staticmethod
    def _respond_accepted(handler: BaseHTTPRequestHandler) -> None:
        body = b'{"status":"queued"}'
        handler.send_response(202)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        try:
            handler.wfile.write(body)
        except OSError:
            pass

    def start(self, on_message: MessageCallback) -> None:
        self._on_message = on_message
        self._stop_requested.clear()
        self._worker = threading.Thread(target=self._worker_loop, name=f"orion-{self.name}-worker", daemon=True)
        self._worker.start()
        adapter = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                try:
                    adapter._handle_post(self)
                except Exception:
                    adapter._error(self, 503, "dependency_unavailable", "Le gateway ne peut pas traiter la requête.")

            def log_message(self, *_: Any) -> None:
                return

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, name=f"orion-{self.name}", daemon=True)
        self._thread.start()

    def receive_payload(
        self,
        payload: Mapping[str, Any],
        *,
        reply_to: str | None = None,
        enqueue_only: bool = False,
        idempotency_key: str | None = None,
    ) -> bool:
        if self._on_message is None:
            if enqueue_only:
                return False
            raise RuntimeError(f"Le channel {self.name} n'est pas demarre.")
        message = InboundMessage(
            channel=self.name,
            payload=dict(payload),
            reply_to=reply_to,
            source=self.name,
            message_id=str(payload["message_id"]) if payload.get("message_id") is not None else None,
            sender=str(payload["sender"]) if payload.get("sender") is not None else None,
            text=str(payload["text"]) if payload.get("text") is not None else None,
            correlation_id=str(payload["correlation_id"]) if payload.get("correlation_id") is not None else None,
        )
        if self.ledger is not None:
            try:
                ledger_id, is_new = self.ledger.record(
                    channel=self.name,
                    payload=dict(payload),
                    message_id=message.message_id,
                    idempotency_key=idempotency_key,
                    correlation_id=message.correlation_id,
                )
            except ValueError:
                return False
            if not is_new:
                # A previous request may have been persisted just before a
                # bounded in-memory queue filled.  Re-enqueue that durable
                # row; delivered rows remain deduplicated.
                existing = self.ledger.get(ledger_id)
                if existing is None or existing.get("status") not in {"queued", "failed"}:
                    return True
            if ledger_id != message.message_id:
                message = InboundMessage(
                    channel=message.channel,
                    payload=message.payload,
                    event_type=message.event_type,
                    reply_to=message.reply_to,
                    source=message.source,
                    priority=message.priority,
                    metadata=message.metadata,
                    message_id=ledger_id,
                    sender=message.sender,
                    text=message.text,
                    received_at=message.received_at,
                    correlation_id=message.correlation_id,
                )
        try:
            self._queue.put_nowait(message)
        except queue.Full:
            if enqueue_only:
                return False
            raise RuntimeError(f"La file du channel {self.name} est pleine.")
        return True

    def send(self, output: AgentOutput) -> None:
        candidate = output.recipient or output.metadata.get("reply_to") or self.outbound_url
        if not candidate:
            raise RuntimeError(f"Aucune URL de sortie pour le channel {self.name}.")
        url = str(candidate)
        try:
            if not self.outbound_allowlist or not _url_matches_allowlist(url, self.outbound_allowlist):
                raise ValueError("La destination n'est pas dans l'allowlist de sortie.")
        except ValueError as exc:
            raise RuntimeError(f"Destination de sortie refusée pour le channel {self.name}.") from exc
        response = httpx.post(
            url,
            json={"text": output.content, "content": output.content, "task_id": output.task_id},
            timeout=self.timeout,
            follow_redirects=False,
        )
        response.raise_for_status()

    def stop(self) -> None:
        self._stop_requested.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._worker is not None:
            self._worker.join(timeout=2.0)
            self._worker = None
        self._on_message = None


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
        self._worker: threading.Thread | None = None
        self._on_message: MessageCallback | None = None
        self._offset = 0
        self._queue: queue.Queue[InboundMessage | None] = queue.Queue(maxsize=MAX_CHANNEL_QUEUE)

    def _worker_loop(self) -> None:
        while not self._stop_requested.is_set():
            try:
                message = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if message is None:
                    return
                callback = self._on_message
                if callback is not None:
                    callback(message)
            except Exception:
                continue
            finally:
                self._queue.task_done()

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
        self._worker = threading.Thread(target=self._worker_loop, name="orion-telegram-worker", daemon=True)
        self._worker.start()
        self._thread = threading.Thread(target=self._run, name="orion-telegram", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_requested.is_set():
            try:
                data = self._api("getUpdates", {"offset": self._offset, "timeout": self.poll_timeout})
                for update in data.get("result", []):
                    message = update.get("message") or update.get("edited_message") or {}
                    chat = message.get("chat") or {}
                    chat_id = chat.get("id")
                    if chat_id is None or (self.allowed_chat_ids and int(chat_id) not in self.allowed_chat_ids):
                        continue
                    text = message.get("text")
                    if not isinstance(text, str) or self._on_message is None:
                        continue
                    inbound = InboundMessage(
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
                    try:
                        self._queue.put_nowait(inbound)
                    except queue.Full:
                        # L'offset reste inchangé : Telegram renverra ce message
                        # une fois la file à nouveau disponible.
                        break
                    self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
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
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._worker is not None:
            self._worker.join(timeout=2.0)
            self._worker = None
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
        allowed_recipient_domains: Iterable[str] | None = None,
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
        self.allowed_recipient_domains = {
            str(domain).strip().lower().lstrip("@")
            for domain in (allowed_recipient_domains or ())
            if str(domain).strip()
        }
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        self._on_message: MessageCallback | None = None
        self._queue: queue.Queue[InboundMessage | None] = queue.Queue(maxsize=MAX_CHANNEL_QUEUE)

    def _worker_loop(self) -> None:
        while not self._stop_requested.is_set():
            try:
                message = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if message is None:
                    return
                callback = self._on_message
                if callback is not None:
                    callback(message)
            except Exception:
                continue
            finally:
                self._queue.task_done()

    def start(self, on_message: MessageCallback) -> None:
        self._on_message = on_message
        self._stop_requested.clear()
        self._worker = threading.Thread(target=self._worker_loop, name="orion-email-worker", daemon=True)
        self._worker.start()
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
                if self._on_message is None:
                    continue
                try:
                    self._queue.put_nowait(
                        InboundMessage(
                            channel=self.name,
                            event_type="email",
                            payload=payload,
                            reply_to=sender,
                            source=self.name,
                        )
                    )
                except queue.Full:
                    # Ne pas marquer le message : il sera repris au prochain poll.
                    continue
                # L'acceptation dans la file est durable pour le cycle du
                # channel ; Seen ne doit avancer qu'après cette acceptation.
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
        recipient = str(recipient).strip()
        if "\r" in recipient or "\n" in recipient:
            raise RuntimeError("Destinataire email invalide.")
        display, address = parseaddr(recipient)
        if not address or "@" not in address or (display and address != recipient and not recipient.endswith(f">")):
            raise RuntimeError("Destinataire email invalide.")
        domain = address.rsplit("@", 1)[1].lower()
        if self.allowed_recipient_domains and domain not in self.allowed_recipient_domains:
            raise RuntimeError("Destinataire email hors allowlist.")
        message = EmailMessage()
        message["From"] = self.username
        message["To"] = address
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
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._worker is not None:
            self._worker.join(timeout=2.0)
            self._worker = None


class DiscordWebhookAdapter:
    """Adaptateur sortant Discord webhook et point d'injection entrant."""

    name = "discord"

    def __init__(self, webhook_url: str, *, timeout: float = 20.0) -> None:
        if not webhook_url:
            raise ValueError("webhook_url est obligatoire pour DiscordWebhookAdapter.")
        _validate_http_url(webhook_url)
        self.webhook_url = webhook_url
        self.timeout = float(timeout)
        if self.timeout <= 0:
            raise ValueError("Le timeout Discord doit être positif.")
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
                reply_to=reply_to,
                source=self.name,
            )
        )

    def send(self, output: AgentOutput) -> None:
        destination = str(output.recipient or self.webhook_url)
        if destination != self.webhook_url:
            raise RuntimeError("Destination Discord hors allowlist.")
        response = httpx.post(
            destination,
            json={"content": output.content[:2000]},
            timeout=self.timeout,
            follow_redirects=False,
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
