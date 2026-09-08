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
from pathlib import Path
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


class TelegramAPIError(RuntimeError):
    """Erreur fonctionnelle renvoyée par l'API Telegram (JSON ``ok=false``)."""

    def __init__(
        self,
        message: str,
        *,
        error_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = int(error_code) if error_code is not None else None
        self.retry_after = float(retry_after) if retry_after is not None else None


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


def escape_telegram_markdown_v2(text: str) -> str:
    """Escape plain text for Telegram's MarkdownV2 parser.

    Telegram treats a surprisingly large set of punctuation as syntax.  This
    helper deliberately escapes all of it; callers can still pass intentional
    markup by using the HTML mode (the safe fallback used by the adapter).
    """
    return re.sub(r"([_\\*\[\]\(\)~`>#+\-=|{}.!])", r"\\\1", str(text))


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
        # A valid injected stream may intentionally be falsy (for example a
        # test wrapper or a closed-looking proxy).  Only ``None`` means
        # "use stdout"; replacing such a stream makes the CLI impossible to
        # embed reliably.
        self.output = sys.stdout if output is None else output
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
        self._threads_provider: CLIProvider | None = None
        self._trace_provider: CLIProvider | None = None
        self._usage_provider: Any = usage_provider or usage_ledger or cost_provider
        self._pending = 0
        self._pending_lock = threading.Lock()
        self._pending_requests: list[dict[str, Any]] = []
        self._request_sequence = 0
        self.slow_request_seconds = max(0.0, float(slow_request_seconds))
        self._slow_alert_stop = threading.Event()
        self._slow_alert_thread: threading.Thread | None = None
        # Runtime calls the diagnostic callback before publishing its
        # user-facing error output.  Keep the diagnostic correlated so the
        # latter can be rendered once as a single error panel.
        self._reported_errors: dict[str, str] = {}
        self._canceled_tokens: set[str] = set()
        # Les callbacks de runtime peuvent être rejoués (ou arriver dans le
        # désordre).  La CLI garde une petite fenêtre de déduplication locale;
        # elle ne doit jamais imprimer deux fois le même fragment.
        self._seen_output_keys: set[tuple[str, str, str]] = set()
        self._last_output_seq: dict[str, int] = {}

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

    def set_threads_provider(self, provider: CLIProvider) -> None:
        self._threads_provider = provider
        self.console.set_threads_provider(provider)

    def set_trace_provider(self, provider: CLIProvider) -> None:
        self._trace_provider = provider
        self.console.set_trace_provider(provider)

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

    def _begin_pending(
        self,
        text: str = "",
        *,
        parent_request_id: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> int:
        request = self.console.new_request(
            text,
            parent_request_id=parent_request_id,
            context=context,
        )
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
                    "correlation_id": request.correlation_id,
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
        correlation_id: str | None = None,
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
            if index is None and correlation_id:
                for position, request in enumerate(self._pending_requests):
                    if request.get("correlation_id") == str(correlation_id):
                        index = position
                        break
            if (event_id or correlation_id) and index is None:
                # Une sortie asynchrone (scheduler, sous-agent, etc.) ne doit
                # pas terminer arbitrairement une requête CLI.
                return
            if not (event_id or correlation_id) and self._pending_requests:
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
            removed = [
                item
                for item in self._pending_requests
                if item.get("request_id") == request_id
            ]
            for item in removed:
                for key in ("request_id", "event_id", "correlation_id"):
                    value = item.get(key)
                    if value:
                        self._canceled_tokens.add(str(value))
            while len(self._canceled_tokens) > 512:
                self._canceled_tokens.pop()
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

    def _submit_text(self, text: str, *, parent_request_id: str | None = None, context: Any = None) -> None:
        """Publie un texte sans laisser une exception de callback tuer la CLI."""
        if self._on_message is None:
            return
        sequence = self._begin_pending(
            text,
            parent_request_id=parent_request_id,
            context=context,
        )
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
                    payload={"text": text, **({"parent_request_id": parent_request_id} if parent_request_id else {}), **({"context": context} if context is not None else {})},
                    reply_to="stdout",
                    metadata={**({"parent_request_id": parent_request_id} if parent_request_id else {}), **({"context": context} if context is not None else {})},
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

    def _resolve_request_id(self, identifier: str | None) -> str | None:
        """Resolve a full request id or an unambiguous displayed prefix."""
        if not identifier:
            return None
        wanted = str(identifier).strip().lower()
        if not wanted:
            return None
        exact = self.console.requests.get(wanted)
        if exact is not None:
            return exact.request_id
        candidates = [
            str(item.get("request_id") or "")
            for item in self.console.requests.snapshot()
            if str(item.get("request_id") or "").lower().startswith(wanted)
        ]
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            self.console.warning(
                f"Identifiant ambigu : {identifier} ({len(candidates)} requêtes correspondent)."
            )
        return None

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
            self.console.help(
                command.args[0] if command.args else None,
                json_output=command.has("json"),
            )
        elif name == "clear":
            self.console.clear()
        elif name == "stop":
            target = command.args[0] if command.args else None
            if command.has("force") and target is None:
                target = "all"
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
                resolved = self._resolve_request_id(target) if target else None
                canceled = self.console.requests.cancel(resolved)
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
            if command.has("watch"):
                self.console.warning("--watch affiche un instantané ; utilisez /status pour le rafraîchir.")
            self.console.status(values, json_output=command.has("json"))
        elif name == "tools":
            values = self._filter_items(
                self._provider_value(self._tools_provider, []),
                command.args[0] if command.args else None,
            )
            self.console.items("Tools disponibles", values, empty="Aucun tool chargé.", json_output=command.has("json"))
        elif name == "tasks":
            values = self._filter_items(
                self._provider_value(self._tasks_provider, []),
                command.args[0] if command.args else None,
            )
            self.console.items("Tâches récentes", values, empty="Aucune tâche durable.", json_output=command.has("json"))
        elif name == "agents":
            values = self._provider_value(self._agents_provider, [])
            self.console.items("Sous-agents", values, empty="Aucun sous-agent configuré.", json_output=command.has("json"))
        elif name == "jobs":
            values = self._filter_items(
                self._provider_value(self._jobs_provider, []),
                command.args[0] if command.args else None,
            )
            if command.has("watch"):
                self.console.warning("--watch affiche un instantané ; utilisez /jobs pour le rafraîchir.")
            self.console.items("Travaux délégués", values, empty="Aucun travail délégué.", json_output=command.has("json"))
        elif name == "requests":
            values = self._filter_items(
                self.console.requests.snapshot(),
                command.args[0] if command.args else None,
            )
            self.console.items("Requêtes récentes", values, empty="Aucune requête récente.", json_output=command.has("json"))
        elif name == "retry":
            resolved = self._resolve_request_id(command.args[0])
            request = self.console.requests.get(resolved) if resolved else None
            if request is None or not request.text:
                self.console.error(f"Requête introuvable : {command.args[0]}")
            elif request.state not in {"failed", "canceled"}:
                self.console.warning("Seules les requêtes échouées ou annulées peuvent être relancées.")
            else:
                self._submit_text(request.text)
        elif name == "resume":
            target_id = command.args[0] if command.args else None
            target_id = self._resolve_request_id(target_id) if target_id else None
            request = self.console.requests.get(target_id) if target_id else None
            if request is None and target_id:
                self.console.error(f"Requête introuvable : {target_id}")
            elif request is None:
                candidates = [
                    r for r in self.console.requests.snapshot()
                    if isinstance(r, Mapping)
                    and r.get("state") in {"failed", "canceled"}
                    and r.get("request_id")
                ]
                # L'ordre du snapshot ne constitue pas un contrat de reprise.
                candidates.sort(
                    key=lambda item: (
                        str(item.get("updated_at") or ""),
                        str(item.get("request_id")),
                    ),
                    reverse=True,
                )
                if candidates:
                    target_id = str(candidates[0]["request_id"])
                    request = self.console.requests.get(target_id)
                else:
                    self.console.warning("Aucune requête à reprendre.")
            resume = getattr(self.console, "resume_context", None)
            if callable(resume) and (target_id is not None or request is not None):
                try:
                    resume_data = resume(target_id) if target_id else resume()
                    if isinstance(resume_data, Mapping):
                        text = resume_data.get("text")
                        parent_id = resume_data.get("parent_request_id") or target_id
                        resume_context = resume_data.get("context")
                    else:
                        text, parent_id, resume_context = resume_data, target_id, None
                    if text:
                        self._submit_text(str(text), parent_request_id=parent_id, context=resume_context)
                    else:
                        self.console.warning("Aucun contexte à reprendre.")
                except Exception as exc:
                    self.console.error(f"Impossible de reprendre la requête : {exc}")
            elif request is not None and getattr(request, "text", None):
                self._submit_text(request.text, parent_request_id=target_id)
            else:
                self.console.warning("La console n'expose pas de contexte de reprise.")
        elif name == "debug":
            self.console.status(
                {"Requêtes en attente": self._pending, "Arrêt demandé": self._stop_requested.is_set()},
                json_output=command.has("json"),
            )
        elif name == "threads":
            values = self._filter_items(
                self._provider_value(self._threads_provider, []),
                command.args[0] if command.args else None,
            )
            self.console.threads(values, json_output=command.has("json"))
        elif name == "trace":
            values = self._filter_items(
                self._provider_value(self._trace_provider, []),
                command.args[0] if command.args else None,
            )
            self.console.trace(values, json_output=command.has("json"))
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
            if any(
                value is not None
                and (
                    str(value).lower() == wanted
                    or str(value).lower().startswith(wanted)
                )
                for value in values
            ):
                result.append(item)
        return result

    def send(self, output: AgentOutput) -> None:
        """Route a normalized output to the CLI renderer.

        The runtime is deliberately not a terminal writer.  Keeping the
        event projection here gives the renderer one canonical input format
        (and means streaming/final messages share the same replay guards).
        Other channel adapters retain their transport-specific ``send``
        implementations below.
        """
        event_id = str(output.event_id or "")
        correlation_id = str(output.correlation_id or output.metadata.get("correlation_id") or "")
        output_seq = output.metadata.get("seq")
        try:
            normalized_seq = int(output_seq) if output_seq is not None else None
        except (TypeError, ValueError, OverflowError):
            normalized_seq = None
        with self._pending_lock:
            if any(
                token
                and token in self._canceled_tokens
                for token in (event_id, correlation_id, str(output.output_id or ""), str(output.idempotency_key or ""))
            ):
                # A network/provider callback can arrive after /stop.  The
                # cancelled request must stay quiet instead of reopening the
                # transcript with a late answer.
                return
            # A correlation identifies a CLI request, not merely a channel.
            # Never let an asynchronous job resurrect or complete another
            # request's transcript.
            matching = [
                item for item in self._pending_requests
                if (correlation_id and item.get("correlation_id") == correlation_id)
                or (event_id and item.get("event_id") == event_id)
            ]
            if correlation_id and not matching:
                # report_error() may have closed a failed request just before
                # its user-facing AgentOutput is delivered.  That one final
                # error is still valid; canceled requests remain suppressed.
                terminal = next(
                    (item for item in self.console.requests.snapshot()
                     if item.get("correlation_id") == correlation_id),
                    None,
                )
                # A request may have completed before a delayed asynchronous
                # (notably sub-agent) result arrives.  It is still a valid
                # result when the request is known and was not cancelled.
                if terminal is None or terminal.get("state") in {"canceled", "cancelled"}:
                    return
            identity = str(output.output_id or output.idempotency_key or event_id or correlation_id)
            fingerprint = hashlib.sha256(str(output.content).encode("utf-8", "replace")).hexdigest()[:16]
            key = (identity, str(normalized_seq) if normalized_seq is not None else fingerprint, str(bool(output.metadata.get("intermediate", False))))
            if key in self._seen_output_keys:
                return
            if normalized_seq is not None and identity:
                previous = self._last_output_seq.get(identity, -1)
                if normalized_seq <= previous:
                    return
                self._last_output_seq[identity] = normalized_seq
            self._seen_output_keys.add(key)
            if len(self._seen_output_keys) > 4096:
                self._seen_output_keys.clear()
                self._last_output_seq.clear()
        is_error = bool(output.metadata.get("error", False))
        if is_error:
            # ``runtime.on_error`` records the technical diagnostic first and
            # then emits the user-facing AgentOutput.  Render one compact
            # error block instead of routing the error through the assistant
            # transcript (which used to create the repeated Orion blocks in
            # the CLI screenshot).
            with self._pending_lock:
                diagnostic = self._reported_errors.pop(event_id, None) if event_id else None
            self._finish_pending(
                output.event_id,
                correlation_id=output.correlation_id,
                state="failed",
                error=str(output.content or diagnostic or "échec de la requête"),
            )
            message = str(output.content or "La requête a échoué.")
            if diagnostic and diagnostic not in message:
                message = f"{message}\nDétail : {diagnostic}"
            self.console.render_event(
                self._output_event(output, text=message, state="failed")
            )
            return
        intermediate = bool(output.metadata.get("intermediate", False))
        if not intermediate:
            self._finish_pending(output.event_id, correlation_id=output.correlation_id)
        elif output.event_id or output.correlation_id:
            with self._pending_lock:
                request = next(
                    (
                        item
                        for item in self._pending_requests
                        if (
                            output.event_id
                            and item.get("event_id") == str(output.event_id)
                        )
                        or (
                            output.correlation_id
                            and item.get("correlation_id") == str(output.correlation_id)
                        )
                    ),
                    None,
                )
            if request is not None:
                try:
                    self.console.requests.update(request["request_id"], "streaming")
                except (KeyError, ValueError, RuntimeError):
                    pass
        event = self._output_event(
            output,
            text=str(output.content),
            state="streaming" if intermediate else "succeeded",
        )
        # Keep the small, historical monkey-patch/integration seam used by
        # embedders and tests.  The normal console has no instance-level
        # ``assistant`` override and always receives the canonical event.
        if "assistant" in getattr(self.console, "__dict__", {}):
            self.console.assistant(
                str(output.content),
                intermediate=intermediate,
                timestamp=output.metadata.get("timestamp"),
                request_id=output.event_id,
            )
        else:
            self.console.render_event(event)

    def _output_event(
        self,
        output: AgentOutput,
        *,
        text: str | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        """Build the canonical event consumed by :meth:`CLIConsole.render_event`."""
        metadata = dict(output.metadata or {})
        request_id = str(output.event_id or output.correlation_id or metadata.get("request_id") or "")
        if not request_id:
            # render_event can still display channel-level output, but a
            # stable id makes replay protection effective for legacy outputs.
            request_id = hashlib.sha256(str(output.content).encode("utf-8", "replace")).hexdigest()[:24]
        sequence = metadata.get("seq", metadata.get("sequence", 0))
        try:
            sequence = int(sequence or 0)
        except (TypeError, ValueError, OverflowError):
            sequence = 0
        state = state or ("streaming" if metadata.get("intermediate") else "succeeded")
        event: dict[str, Any] = {
            "kind": "request.streaming" if state == "streaming" else "request." + state,
            "request_id": request_id,
            "correlation_id": output.correlation_id or metadata.get("correlation_id"),
            "state": state,
            "seq": sequence,
            "text": text if text is not None else str(output.content),
            "timestamp": metadata.get("timestamp"),
            "meta": {
                key: value
                for key, value in metadata.items()
                if key not in {"intermediate", "error", "timestamp", "seq", "sequence", "request_id"}
            },
        }
        if state == "failed":
            event["error"] = text if text is not None else str(output.content)
        return event

    def report_error(self, event: object, error: Exception) -> None:
        event_id = str(getattr(event, "id", "") or "")
        diagnostic = f"{type(error).__name__} · événement {getattr(event, 'type', 'unknown')}\n{error}"
        if event_id:
            with self._pending_lock:
                self._reported_errors[event_id] = diagnostic
                # Keep diagnostics bounded in a long-running CLI.  Dicts
                # preserve insertion order on supported Python versions.
                while len(self._reported_errors) > 256:
                    self._reported_errors.pop(next(iter(self._reported_errors)))
        self._finish_pending(
            getattr(event, "id", None),
            correlation_id=getattr(event, "correlation_id", None),
            state="failed",
            error=f"{type(error).__name__}: {error}",
        )

    def stop(self) -> None:
        self._stop_requested.set()
        self._slow_alert_stop.set()
        self.console.stop()
        with self._pending_lock:
            for item in self._pending_requests:
                for key in ("request_id", "event_id", "correlation_id"):
                    value = item.get(key)
                    if value:
                        self._canceled_tokens.add(str(value))
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
        replay_window: float = 300.0,
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
        self.replay_window = float(replay_window)
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
        if self.replay_window <= 0 or self.replay_window > 86400:
            raise ValueError("replay_window doit être compris entre 0 et 86400 secondes.")
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
        self._nonces: OrderedDict[str, float] = OrderedDict()

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
            timestamp = headers.get("X-Orion-Timestamp", "")
            nonce = headers.get("X-Orion-Nonce", "")
            signed = f"{timestamp}\n{nonce}\n".encode("utf-8") + raw
            expected = hmac.new(self.hmac_secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
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

    def _check_replay(self, headers: Mapping[str, str]) -> bool:
        if not self.hmac_secret:
            return True
        try:
            timestamp = float(headers.get("X-Orion-Timestamp", ""))
        except (TypeError, ValueError):
            return False
        nonce = headers.get("X-Orion-Nonce", "")
        now = time.time()
        if not nonce or len(nonce) > 256 or abs(now - timestamp) > self.replay_window:
            return False
        with self._idempotency_lock:
            for key, value in list(self._nonces.items()):
                if now - value > self.replay_window:
                    self._nonces.pop(key, None)
            if nonce in self._nonces:
                return False
            self._nonces[nonce] = timestamp
        return True

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
        if not self._check_replay(handler.headers):
            self._error(handler, 401, "replay_rejected", "Signature expirée ou nonce déjà utilisé.")
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
                    conversation_id=message.conversation_id,
                    user_id=message.user_id,
                    message_thread_id=message.message_thread_id,
                    thread_id=message.thread_id,
                    parent_message_id=message.parent_message_id,
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
        allowed_user_ids: list[int] | None = None,
        allow_all_chats: bool = False,
        accept_edited: bool = False,
        bootstrap_owner: bool = True,
        owner_path: str | None = None,
        outbound_allowed_chat_ids: list[int] | None = None,
        offset_path: str | None = None,
        ledger: CommunicationLedger | None = None,
        replay_window: float = 300.0,
        api_timeout: float = 35.0,
        parse_mode: str | None = "HTML",
        max_message_chars: int = 3500,
        max_retries: int = 3,
        retry_backoff: float = 0.5,
        retry_max_delay: float = 30.0,
        queue_size: int = MAX_CHANNEL_QUEUE,
    ) -> None:
        if not token:
            raise ValueError("Le token Telegram est obligatoire.")
        self.token = token
        self.poll_timeout = int(poll_timeout)
        if self.poll_timeout < 0:
            raise ValueError("poll_timeout Telegram doit être positif ou nul.")
        if not isinstance(allow_all_chats, bool) or not isinstance(accept_edited, bool) or not isinstance(bootstrap_owner, bool):
            raise ValueError("allow_all_chats, accept_edited et bootstrap_owner doivent être des booléens.")
        self.allowed_chat_ids = {int(item) for item in (allowed_chat_ids or [])}
        self.allowed_user_ids = {int(item) for item in (allowed_user_ids or [])}
        self.allow_all_chats = allow_all_chats
        self.accept_edited = accept_edited
        self.bootstrap_owner = bootstrap_owner
        # The inbound allowlist is the default outbound policy too.  A
        # separate list is useful for bots that may receive from a larger set
        # but must only answer selected chats.
        self.outbound_allowed_chat_ids = {
            int(item) for item in (
                outbound_allowed_chat_ids
                if outbound_allowed_chat_ids is not None
                else allowed_chat_ids
                or []
            )
        }
        self._seen_chat_ids: set[int] = set()
        if parse_mode not in {None, "HTML", "MarkdownV2"}:
            raise ValueError("parse_mode Telegram doit être HTML, MarkdownV2 ou null.")
        if max_message_chars < 500 or max_message_chars > 4096:
            raise ValueError("max_message_chars Telegram doit être compris entre 500 et 4096.")
        if max_retries < 0:
            raise ValueError("max_retries Telegram doit être positif ou nul.")
        if retry_backoff < 0 or retry_max_delay < 0:
            raise ValueError("Les délais de retry Telegram ne peuvent pas être négatifs.")
        if queue_size < 1 or queue_size > MAX_CHANNEL_QUEUE:
            raise ValueError("queue_size Telegram doit être compris entre 1 et 1000.")
        self.parse_mode = parse_mode
        self.max_message_chars = int(max_message_chars)
        self.max_retries = int(max_retries)
        self.retry_backoff = float(retry_backoff)
        self.retry_max_delay = float(retry_max_delay)
        self.api_timeout = float(api_timeout)
        self.client = httpx.Client(base_url=f"https://api.telegram.org/bot{token}", timeout=self.api_timeout)
        self._client_closed = False
        self.ledger = ledger
        self.replay_window = float(replay_window)
        if self.replay_window <= 0 or self.replay_window > 86400:
            raise ValueError("replay_window doit être compris entre 0 et 86400 secondes.")
        self.offset_path = os.fspath(offset_path) if offset_path else None
        self.owner_path = os.fspath(owner_path) if owner_path else None
        if self.offset_path:
            Path(self.offset_path).parent.mkdir(parents=True, exist_ok=True)
        if self.owner_path:
            Path(self.owner_path).parent.mkdir(parents=True, exist_ok=True)
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        self._on_message: MessageCallback | None = None
        self._state_lock = threading.RLock()
        self._offset = self._load_offset()
        self._owner_chat_id, self._owner_user_id = self._load_owner()
        self._dedupe_lock = threading.Lock()
        self._dedupe: OrderedDict[str, None] = OrderedDict()
        self._worker_id = f"telegram-worker-{os.getpid()}-{id(self)}"
        self._queue: queue.Queue[InboundMessage | None] = queue.Queue(maxsize=int(queue_size))

    @property
    def offset(self) -> int:
        return self._offset

    def _cursor_name(self) -> str:
        return "telegram:" + hashlib.sha256(self.token.encode("utf-8")).hexdigest()[:16]

    def _load_offset(self) -> int:
        if self.offset_path:
            try:
                value = int(Path(self.offset_path).read_text(encoding="utf-8").strip() or "0")
                return max(0, value)
            except (OSError, ValueError):
                return 0
        if self.ledger is not None and hasattr(self.ledger, "get_cursor"):
            try:
                return max(0, int(self.ledger.get_cursor(self._cursor_name())))
            except (OSError, ValueError, TypeError, RuntimeError):
                return 0
        return 0

    def _load_owner(self) -> tuple[int | None, int | None]:
        if not self.owner_path:
            return None, None
        try:
            data = json.loads(Path(self.owner_path).read_text(encoding="utf-8"))
            chat_id = int(data["chat_id"])
            user_id = int(data["user_id"]) if data.get("user_id") is not None else None
            return chat_id, user_id
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None, None

    def _persist_owner(self, chat_id: int, user_id: int | None) -> None:
        if not self.owner_path:
            self._owner_chat_id, self._owner_user_id = chat_id, user_id
            return
        path = Path(self.owner_path)
        temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        data = {"chat_id": int(chat_id), "user_id": int(user_id) if user_id is not None else None}
        try:
            with temp.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass
        self._owner_chat_id, self._owner_user_id = int(chat_id), user_id

    def _persist_offset(self, value: int) -> None:
        value = max(0, int(value))
        if value <= self._offset:
            return
        if self.offset_path:
            path = Path(self.offset_path)
            temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                with temp.open("w", encoding="utf-8") as handle:
                    handle.write(str(value))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, path)
                try:
                    directory_fd = os.open(str(path.parent), os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    pass
            finally:
                try:
                    temp.unlink()
                except FileNotFoundError:
                    pass
        elif self.ledger is not None and hasattr(self.ledger, "set_cursor"):
            self.ledger.set_cursor(self._cursor_name(), value)
        self._offset = value

    def _remember_update(self, key: str) -> bool:
        with self._dedupe_lock:
            if key in self._dedupe:
                self._dedupe.move_to_end(key)
                return False
            self._dedupe[key] = None
            while len(self._dedupe) > self._queue.maxsize * 2:
                self._dedupe.popitem(last=False)
            return True

    def _forget_update(self, key: str) -> None:
        with self._dedupe_lock:
            self._dedupe.pop(key, None)

    @staticmethod
    def _retry_status(exc: BaseException) -> tuple[int | None, float | None]:
        if isinstance(exc, httpx.HTTPStatusError):
            response = exc.response
            retry_after: float | None = None
            try:
                retry_after = float(response.headers.get("Retry-After", ""))
            except (TypeError, ValueError):
                pass
            return response.status_code, retry_after
        if isinstance(exc, TelegramAPIError):
            return exc.error_code, exc.retry_after
        return None, None

    def _worker_loop(self) -> None:
        while True:
            try:
                message = self._queue.get(timeout=0.2)
            except queue.Empty:
                if self._stop_requested.is_set():
                    return
                continue
            try:
                if message is None:
                    return
                ledger_claimed = True
                if self.ledger is not None and hasattr(self.ledger, "claim_by_id") and message.message_id:
                    try:
                        ledger_claimed = bool(
                            self.ledger.claim_by_id(message.message_id, worker_id=self._worker_id)
                        )
                    except Exception:
                        # Keep the item available for a transient SQLite
                        # failure instead of silently dropping it after the
                        # Telegram cursor has advanced.
                        try:
                            self._queue.put_nowait(message)
                        except queue.Full:
                            pass
                        self._stop_requested.wait(0.2)
                        ledger_claimed = False
                if not ledger_claimed:
                    continue
                callback = self._on_message
                if callback is None:
                    try:
                        self._queue.put_nowait(message)
                    except queue.Full:
                        pass
                    continue
                callback(message)
                if self.ledger is not None and hasattr(self.ledger, "ack") and message.message_id:
                    try:
                        self.ledger.ack(message.message_id, worker_id=self._worker_id)
                    except Exception:
                        pass
            except Exception as exc:
                if self.ledger is not None and hasattr(self.ledger, "fail") and message is not None and message.message_id:
                    try:
                        self.ledger.fail(message.message_id, exc, worker_id=self._worker_id)
                    except Exception:
                        pass
                continue
            finally:
                self._queue.task_done()

    def _api(self, method: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        for attempt in range(self.max_retries + 1):
            try:
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
                    parameters = data.get("parameters") or {}
                    raise TelegramAPIError(
                        f"Telegram API error: {data}",
                        error_code=data.get("error_code"),
                        retry_after=parameters.get("retry_after"),
                    )
                return data
            except (httpx.HTTPStatusError, httpx.RequestError, TelegramAPIError) as exc:
                status, retry_after = self._retry_status(exc)
                retryable = isinstance(exc, httpx.RequestError) or (
                    status is not None and (status == 429 or status >= 500)
                )
                if not retryable or attempt >= self.max_retries or self._stop_requested.is_set():
                    raise
                delay = retry_after if retry_after is not None else self.retry_backoff * (2 ** attempt)
                delay = min(self.retry_max_delay, max(0.0, float(delay)))
                self._stop_requested.wait(delay)
        raise RuntimeError("Telegram API retry loop unexpectedly exhausted")

    def start(self, on_message: MessageCallback) -> None:
        if not callable(on_message):
            raise TypeError("on_message doit être appelable.")
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                self._on_message = on_message
                return
            if self._client_closed:
                self.client = httpx.Client(base_url=f"https://api.telegram.org/bot{self.token}", timeout=self.api_timeout)
                self._client_closed = False
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
                    if not isinstance(update, Mapping):
                        continue
                    try:
                        update_id = int(update.get("update_id"))
                    except (TypeError, ValueError):
                        continue
                    message = update.get("message")
                    if not message and self.accept_edited:
                        message = update.get("edited_message")
                    message = message or {}
                    if not isinstance(message, Mapping):
                        self._persist_offset(update_id + 1)
                        continue
                    chat = message.get("chat") or {}
                    try:
                        chat_id = int(chat.get("id"))
                    except (TypeError, ValueError):
                        self._persist_offset(update_id + 1)
                        continue
                    sender_info = message.get("from") or {}
                    try:
                        user_id = int(sender_info.get("id")) if sender_info.get("id") is not None else None
                    except (TypeError, ValueError):
                        user_id = None
                    accepted_chat = self.allow_all_chats or chat_id in self.allowed_chat_ids
                    accepted_user = bool(self.allowed_user_ids and user_id in self.allowed_user_ids)
                    owner_bootstrap = (
                        self.bootstrap_owner
                        and not self.allow_all_chats
                        and not self.allowed_chat_ids
                        and not self.allowed_user_ids
                        and self._owner_chat_id is None
                        # Bootstrap is intentionally restricted to a direct
                        # private conversation.  A group/channel must never
                        # be able to claim the bot as its owner, and Telegram
                        # updates without a sender identity are not bindable.
                        and str(chat.get("type", "")).lower() == "private"
                        and user_id is not None
                    )
                    owner_match = (
                        self.bootstrap_owner
                        and self._owner_chat_id is not None
                        and chat_id == self._owner_chat_id
                        and (self._owner_user_id is None or user_id == self._owner_user_id)
                    )
                    if not accepted_chat and not accepted_user and not owner_bootstrap and not owner_match:
                        self._persist_offset(update_id + 1)
                        continue
                    text = message.get("text")
                    if not isinstance(text, str) or self._on_message is None:
                        self._persist_offset(update_id + 1)
                        continue
                    raw_message_id = message.get("message_id")
                    try:
                        numeric_message_id = int(raw_message_id) if raw_message_id is not None else None
                    except (TypeError, ValueError):
                        numeric_message_id = None
                    update_kind = "edited:" if update.get("edited_message") and not update.get("message") else ""
                    message_id = (
                        f"{update_kind}{chat_id}:{numeric_message_id}"
                        if numeric_message_id is not None
                        else f"{update_kind}update:{update_id}"
                    )
                    thread_id = message.get("message_thread_id")
                    try:
                        numeric_thread_id = int(thread_id) if thread_id is not None else None
                    except (TypeError, ValueError):
                        numeric_thread_id = None
                    conversation_id = f"{chat_id}:{numeric_thread_id}" if numeric_thread_id is not None else str(chat_id)
                    correlation_id = f"telegram:update:{update_id}"
                    if not self._remember_update(message_id):
                        self._persist_offset(update_id + 1)
                        continue
                    inbound = InboundMessage(
                        channel=self.name,
                        payload={
                            "text": text,
                            "chat_id": chat_id,
                            "user_id": user_id,
                            "username": sender_info.get("username"),
                            "update_id": update_id,
                            "message_id": message_id,
                            "conversation_id": conversation_id,
                            "message_thread_id": numeric_thread_id,
                            "raw": update,
                        },
                        reply_to=str(chat_id),
                        source=self.name,
                        metadata={
                            "chat_id": chat_id,
                            "conversation_id": conversation_id,
                            "update_id": update_id,
                            "message_thread_id": numeric_thread_id,
                        },
                        message_id=message_id,
                        sender=str(user_id) if user_id is not None else None,
                        text=text,
                        correlation_id=correlation_id,
                    )
                    if self.ledger is not None and hasattr(self.ledger, "record_inbound"):
                        try:
                            ledger_id, is_new = self.ledger.record_inbound(
                                channel=self.name,
                                payload=dict(inbound.payload),
                                message_id=message_id,
                                event_id=f"telegram:update:{update_id}",
                                correlation_id=correlation_id,
                                reply_to=str(chat_id),
                            )
                            if not is_new:
                                existing = self.ledger.get(ledger_id) if hasattr(self.ledger, "get") else None
                                # A queued durable row can have been created
                                # immediately before a full local queue.  It
                                # must be offered again; delivered rows are
                                # genuine duplicates and may advance offset.
                                if existing is None or existing.get("status") not in {"queued", "failed"}:
                                    self._persist_offset(update_id + 1)
                                    continue
                            if ledger_id != message_id:
                                inbound = InboundMessage(
                                    channel=inbound.channel, payload=inbound.payload,
                                    reply_to=inbound.reply_to, source=inbound.source,
                                    metadata=inbound.metadata, message_id=ledger_id,
                                    sender=inbound.sender, text=inbound.text,
                                    correlation_id=inbound.correlation_id,
                                    event_type=inbound.event_type, priority=inbound.priority,
                                    received_at=inbound.received_at,
                                    conversation_id=inbound.conversation_id,
                                    user_id=inbound.user_id,
                                    message_thread_id=inbound.message_thread_id,
                                    thread_id=inbound.thread_id,
                                    parent_message_id=inbound.parent_message_id,
                                )
                        except Exception:
                            # Do not advance Telegram's cursor if durable
                            # deduplication could not accept this update.
                            self._forget_update(message_id)
                            break
                    try:
                        self._queue.put_nowait(inbound)
                    except queue.Full:
                        # L'offset reste inchangé : Telegram renverra ce message
                        # une fois la file à nouveau disponible.
                        self._forget_update(message_id)
                        break
                    self._seen_chat_ids.add(chat_id)
                    if owner_bootstrap:
                        self._persist_owner(chat_id, user_id)
                    self._persist_offset(update_id + 1)
            except (httpx.HTTPError, RuntimeError, ValueError, OSError):
                if not self._stop_requested.wait(2.0):
                    continue

    def send(self, output: AgentOutput) -> None:
        chat_id = output.recipient or output.metadata.get("chat_id") or output.metadata.get("reply_to")
        if chat_id is None:
            raise RuntimeError("Aucun chat_id Telegram pour cette sortie.")
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Le chat_id Telegram doit être un entier.") from exc
        if not (
            self.allow_all_chats
            or chat_id in self.outbound_allowed_chat_ids
            or chat_id in self._seen_chat_ids
            or (
                self.bootstrap_owner
                and self._owner_chat_id is not None
                and chat_id == self._owner_chat_id
            )
        ):
            raise RuntimeError("La destination Telegram n'est pas dans l'allowlist de sortie.")
        for chunk in split_telegram_message(output.content, max_chars=self.max_message_chars):
            text = (
                markdown_to_telegram_html(chunk)
                if self.parse_mode == "HTML"
                else escape_telegram_markdown_v2(chunk)
                if self.parse_mode == "MarkdownV2"
                else chunk
            )
            payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
            thread_id = output.metadata.get("message_thread_id")
            if thread_id is not None:
                try:
                    payload["message_thread_id"] = int(thread_id)
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("message_thread_id Telegram doit être un entier.") from exc
            if self.parse_mode:
                payload["parse_mode"] = self.parse_mode
            try:
                self._api("sendMessage", payload)
            except (httpx.HTTPStatusError, TelegramAPIError) as exc:
                # Un découpage au milieu d'un bloc Markdown peut produire un
                # HTML incomplet. La réponse doit tout de même parvenir à
                # l'utilisateur plutôt que de perdre tout le RUN.
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else exc.error_code
                if status != 400 or self.parse_mode not in {"HTML", "MarkdownV2"}:
                    raise
                fallback = (
                    {"chat_id": chat_id, "text": markdown_to_telegram_html(chunk), "parse_mode": "HTML"}
                    if self.parse_mode == "MarkdownV2"
                    else {"chat_id": chat_id, "text": chunk}
                )
                self._api("sendMessage", fallback)

    def stop(self) -> None:
        with self._state_lock:
            thread, worker = self._thread, self._worker
            if thread is None and worker is None:
                self._stop_requested.set()
                return
            self._stop_requested.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(2.0, self.poll_timeout + 2))
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=2.0)
        with self._state_lock:
            self._thread = None
            self._worker = None
            self._on_message = None
        try:
            self.client.close()
            self._client_closed = True
        except Exception:
            pass


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
    "TelegramAPIError",
    "escape_telegram_markdown_v2",
    "secret_from_env",
]
