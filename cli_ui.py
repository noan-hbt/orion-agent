"""Interface terminal interactive d'Orion.

Rich assure le rendu Markdown et prompt_toolkit maintient le prompt stable
pendant les sorties asynchrones. Un mode de repli reste disponible lorsque
ces dépendances ne sont pas installées ou que la sortie n'est pas un TTY.
"""

from __future__ import annotations

import os
import json
import queue
import shlex
import sys
import threading
import textwrap
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from rich.console import Console, Group
    from rich.cells import cell_len as _rich_cell_len
    from rich.markdown import Markdown
    from rich.padding import Padding
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    _RICH_AVAILABLE = True
except ImportError:  # pragma: no cover - repli pour installation incomplète
    _RICH_AVAILABLE = False

    def _rich_cell_len(value: str) -> int:
        return len(value)

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.application.current import get_app
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.history import FileHistory, InMemoryHistory
    from prompt_toolkit.input import create_input
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.output import create_output
    from prompt_toolkit.patch_stdout import patch_stdout
    from prompt_toolkit.styles import Style

    _PROMPT_AVAILABLE = True
except ImportError:  # pragma: no cover - repli pour installation incomplète
    _PROMPT_AVAILABLE = False


_COMMANDS = (
    "/help",
    "/status",
    "/requests",
    "/tools",
    "/tasks",
    "/agents",
    "/jobs",
    "/clear",
    "/stop",
    "/retry",
    "/debug",
    "/exit",
)

REQUEST_STATES = ("queued", "running", "streaming", "succeeded", "failed", "canceled")
_TERMINAL_STATES = frozenset({"succeeded", "failed", "canceled"})
_REQUEST_TRANSITIONS = {
    "queued": frozenset({"queued", "running", "streaming", "succeeded", "failed", "canceled"}),
    "running": frozenset({"running", "streaming", "succeeded", "failed", "canceled"}),
    "streaming": frozenset({"streaming", "succeeded", "failed", "canceled"}),
    "succeeded": frozenset({"succeeded"}),
    "failed": frozenset({"failed"}),
    "canceled": frozenset({"canceled"}),
}


class CLIParseError(ValueError):
    """Erreur locale de syntaxe d'une commande CLI."""


@dataclass(frozen=True)
class CLIUserInput:
    """Ligne utilisateur qui doit être transmise au runtime."""

    text: str


@dataclass(frozen=True)
class CLICommand:
    """Commande analysée sans dépendance au runtime.

    ``name`` est canonique et ne contient pas le slash (``"status"``), tandis
    que ``command`` fournit la forme d'affichage ``"/status"``.  Les options
    sont booléennes et les arguments positionnels restent dans ``args`` afin
    que le controller puisse appliquer son propre comportement.
    """

    name: str
    args: tuple[str, ...] = ()
    options: Mapping[str, bool] = field(default_factory=dict)

    @property
    def command(self) -> str:
        return f"/{self.name}"

    def has(self, option: str) -> bool:
        return bool(self.options.get(option.lstrip("-"), False))


@dataclass(frozen=True)
class CLIEvent:
    """Événement normalisé rendu par les sorties texte et JSONL."""

    kind: str
    request_id: str | None = None
    correlation_id: str | None = None
    state: str | None = None
    seq: int = 0
    text: str | None = None
    error: str | None = None
    meta: Mapping[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().astimezone().isoformat())

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"kind": self.kind}
        for key in ("request_id", "correlation_id", "state", "seq", "text", "error", "timestamp"):
            value = getattr(self, key)
            if value is not None:
                result[key] = value
        result["meta"] = dict(self.meta)
        return result


_COMMAND_SPECS: dict[str, tuple[int, int, frozenset[str]]] = {
    "help": (0, 1, frozenset({"help", "json"})),
    "status": (0, 0, frozenset({"help", "json", "watch"})),
    "requests": (0, 1, frozenset({"help", "json"})),
    "stop": (0, 1, frozenset({"help", "force", "json"})),
    "retry": (1, 1, frozenset({"help", "json"})),
    "debug": (0, 0, frozenset({"help", "json"})),
    "jobs": (0, 1, frozenset({"help", "json", "watch"})),
    "agents": (0, 0, frozenset({"help", "json"})),
    "tasks": (0, 1, frozenset({"help", "json"})),
    "tools": (0, 1, frozenset({"help", "json"})),
    "clear": (0, 0, frozenset({"help"})),
    "exit": (0, 0, frozenset({"help", "force"})),
}
_COMMAND_ALIASES = {"commands": "help", "history": "requests", "quit": "exit"}

# Keep the help catalogue next to the parser contract.  The old help text was
# maintained separately and silently drifted: ``/requests``, ``/retry`` and
# ``/debug`` were valid commands but were absent from the screen.
_COMMAND_DESCRIPTIONS = {
    "help": "Afficher toutes les commandes et leurs options",
    "status": "Afficher l'etat du runtime et la tache active",
    "requests": "Lister les requetes recentes (ou une requete par identifiant)",
    "stop": "Annuler une requete active (ou toutes avec all)",
    "retry": "Relancer une requete echouee ou annulee",
    "debug": "Afficher les informations de diagnostic de la CLI",
    "jobs": "Lister les travaux delegues recents",
    "agents": "Lister les sous-agents disponibles",
    "tasks": "Lister les taches durables recentes",
    "tools": "Lister les tools disponibles pour Orion",
    "clear": "Nettoyer l'ecran",
    "exit": "Arreter Orion proprement",
}


class CLICommandParser:
    """Parseur déterministe de commandes et de texte utilisateur.

    Il n'exécute rien et ne connaît ni le runtime ni les providers. Les lignes
    commençant par ``/`` sont toujours des commandes locales ; une faute de
    frappe ne peut donc pas être envoyée par accident au modèle.
    """

    def parse(self, line: str) -> CLICommand | CLIUserInput:
        raw = str(line).strip()
        if not raw.startswith("/"):
            return CLIUserInput(raw)
        try:
            tokens = shlex.split(raw, posix=True, comments=False)
        except ValueError as exc:
            raise CLIParseError(f"Commande mal formée : {exc}") from exc
        if not tokens or not tokens[0].startswith("/"):
            return CLIUserInput(raw)
        name = tokens[0][1:].lower()
        name = _COMMAND_ALIASES.get(name, name)
        if name not in _COMMAND_SPECS:
            raise CLIParseError(f"Commande inconnue : {tokens[0]} (utilisez /help)")
        min_args, max_args, accepted = _COMMAND_SPECS[name]
        args: list[str] = []
        options: dict[str, bool] = {}
        for token in tokens[1:]:
            if token.startswith("--"):
                option = token[2:].lower()
                if option not in accepted:
                    raise CLIParseError(f"Option inconnue pour /{name} : {token}")
                options[option] = True
            else:
                args.append(token)
        if len(args) < min_args or len(args) > max_args:
            usage = f"/{name}" + (" [argument]" if max_args else "")
            raise CLIParseError(f"Usage : {usage}")
        return CLICommand(name, tuple(args), options)

    __call__ = parse


def parse_cli_input(line: str) -> CLICommand | CLIUserInput:
    """Raccourci sans état pour les adaptateurs et les tests."""

    return CLICommandParser().parse(line)


class CLIEventRenderer:
    """Rendu réutilisable d'événements normalisés en texte ou JSONL."""

    def __init__(self, output: Any = None, *, mode: str = "plain") -> None:
        self.output = sys.stdout if output is None else output
        normalized = str(mode).lower()
        if normalized == "text":
            normalized = "plain"
        if normalized not in {"plain", "jsonl"}:
            raise ValueError("mode doit être plain ou jsonl")
        self.mode = normalized
        self._lock = threading.RLock()

    @staticmethod
    def _mapping(event: CLIEvent | Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(event, CLIEvent):
            return event.as_dict()
        if not isinstance(event, Mapping):
            raise TypeError("event doit être un CLIEvent ou un mapping")
        result = dict(event)
        result.setdefault("kind", "event")
        return result

    def render(self, event: CLIEvent | Mapping[str, Any]) -> str:
        payload = self._mapping(event)
        if self.mode == "jsonl":
            line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        else:
            kind = str(payload.get("kind", "event"))
            fields = [f"{key}={payload[key]}" for key in ("request_id", "correlation_id", "state", "seq") if key in payload]
            text = payload.get("text")
            if text:
                fields.append(str(text))
            # Error payloads are often intentionally kept out of ``text``.
            # Plain output must retain that diagnostic just like JSONL does;
            # otherwise a redirected failure becomes an unhelpful state line.
            error = payload.get("error")
            if error and not text:
                fields.append(f"error={error}")
            # Les mises à jour de jobs restent utiles en mode pipe sans
            # exposer le payload interne complet ni des secrets éventuels.
            if kind.startswith("job.") and isinstance(payload.get("meta"), Mapping):
                safe_job_fields = ("owner", "instance", "team", "objective", "attempt", "result_summary")
                fields.extend(
                    f"{key}={payload['meta'][key]}"
                    for key in safe_job_fields
                    if key in payload["meta"] and payload["meta"][key] is not None
                )
            line = kind if not fields else f"{kind} " + " ".join(fields)
        with self._lock:
            print(line, file=self.output, flush=True)
        return line

    __call__ = render

    def flush(self) -> None:
        with self._lock:
            flush = getattr(self.output, "flush", None)
            if callable(flush):
                flush()

    def close(self) -> None:
        # Le renderer ne possède pas stdout ni les flux injectés par le caller.
        self.flush()


class CLIJSONLRenderer(CLIEventRenderer):
    """Alias explicite pour les intégrateurs automatisés."""

    def __init__(self, output: Any = None) -> None:
        super().__init__(output, mode="jsonl")


@dataclass
class CLIRequest:
    """Etat observable d'une requete CLI.

    Le tracker est volontairement independant du runtime : il peut donc etre
    utilise par un adaptateur TTY, un pipe ou un test StringIO.
    """

    request_id: str
    correlation_id: str
    state: str = "queued"
    text: str = ""
    error: str | None = None
    seq: int = 0
    last_fragment: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now().astimezone())
    updated_at: datetime = field(default_factory=lambda: datetime.now().astimezone())


class CLIRequestTracker:
    """Registre borne et thread-safe des requetes visibles par l'operateur.

    Contrat d'intégration : créer une requête avec :meth:`create`, conserver
    son ``request_id`` et faire progresser son état avec :meth:`update`.
    ``correlation_id`` est immuable et doit être recopié par le transport dans
    ses événements de sortie. Les états terminaux sont ``succeeded``,
    ``failed`` et ``canceled`` ; une requête terminée ne peut pas être
    relancée.
    """

    def __init__(self, *, max_items: int = 1000) -> None:
        if isinstance(max_items, bool):
            raise ValueError("max_items doit etre un entier positif.")
        try:
            normalized_max_items = int(max_items)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("max_items doit etre un entier positif.") from exc
        if normalized_max_items < 1:
            raise ValueError("max_items doit etre un entier positif.")
        self.max_items = normalized_max_items
        self._lock = threading.RLock()
        self._items: dict[str, CLIRequest] = {}
        self._active: str | None = None
        self._stopped = False

    def create(self, text: str = "", *, request_id: str | None = None, correlation_id: str | None = None) -> CLIRequest:
        with self._lock:
            if self._stopped:
                raise RuntimeError("Le tracker CLI est arrete.")
            identifier = str(request_id or uuid.uuid4())
            if identifier in self._items:
                return self._items[identifier]
            now = datetime.now().astimezone()
            item = CLIRequest(identifier, str(correlation_id or identifier), text=str(text), created_at=now, updated_at=now)
            self._items[identifier] = item
            self._active = identifier
            while len(self._items) > self.max_items:
                self._items.pop(next(iter(self._items)))
            return item

    def update(
        self,
        request_id: str,
        state: str,
        *,
        error: str | None = None,
        text: str | None = None,
        seq: int | None = None,
    ) -> CLIRequest:
        if state not in REQUEST_STATES:
            raise ValueError(f"Etat CLI inconnu : {state}.")
        with self._lock:
            item = self._items.get(str(request_id))
            if item is None:
                raise KeyError(str(request_id))
            if state not in _REQUEST_TRANSITIONS[item.state]:
                raise ValueError(f"Transition CLI interdite : {item.state} -> {state}.")
            if seq is not None:
                try:
                    normalized_seq = int(seq)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError("seq doit être un entier positif.") from exc
                if normalized_seq < item.seq:
                    # Les transports peuvent livrer un fragment en retard.
                    # Le tracker reste sur la dernière version connue.
                    return item
                item.seq = normalized_seq
            if text is not None:
                item.last_fragment = str(text)
            item.state = state
            item.error = None if error is None else str(error)
            item.updated_at = datetime.now().astimezone()
            if state in {"succeeded", "failed", "canceled"} and self._active == item.request_id:
                self._active = None
            return item

    def cancel(self, request_id: str | None = None) -> CLIRequest | None:
        with self._lock:
            identifier = str(request_id or self._active) if (request_id or self._active) else None
            if identifier is None:
                return None
            item = self._items.get(identifier)
            if item is None:
                return None
            if item.state not in {"succeeded", "failed", "canceled"}:
                item.state = "canceled"
                item.updated_at = datetime.now().astimezone()
            if self._active == identifier:
                self._active = None
            return item

    def get(self, request_id: str) -> CLIRequest | None:
        with self._lock:
            return self._items.get(str(request_id))

    def status(self, request_id: str | None = None) -> dict[str, Any] | None:
        item = self.get(request_id or self._active) if (request_id or self._active) else None
        if item is None:
            return None
        return {
            "request_id": item.request_id,
            "correlation_id": item.correlation_id,
            "state": item.state,
            "text": item.text,
            "error": item.error,
            "seq": item.seq,
            "last_fragment": item.last_fragment,
            "created_at": item.created_at.isoformat(),
            "updated_at": item.updated_at.isoformat(),
        }

    def jobs(self) -> list[dict[str, Any]]:
        return self.snapshot()

    def list_recent(self) -> list[dict[str, Any]]:
        """Alias explicite utilisé par le controller et les renderers."""

        return self.snapshot()

    def record_fragment(self, request_id: str, text: str, *, seq: int) -> CLIRequest:
        """Enregistre un fragment en ignorant les doublons tardifs.

        Une sortie arrivée après annulation ou succès ne peut pas rouvrir une
        requête. Elle est simplement ignorée, ce qui protège le rendu TTY des
        callbacks de transport en retard.
        """

        with self._lock:
            item = self._items.get(str(request_id))
            if item is None:
                raise KeyError(str(request_id))
            if item.state in _TERMINAL_STATES:
                return item
            if int(seq) <= item.seq:
                return item
            return self.update(request_id, "streaming", text=text, seq=int(seq))

    def stop(self) -> bool:
        """Arrete le tracker; les appels repetes sont sans effet."""
        with self._lock:
            changed = not self._stopped
            self._stopped = True
            # L'arrêt de la console doit laisser un état cohérent même lorsque
            # plusieurs demandes sont en vol. Elles ne doivent pas pouvoir
            # redevenir ``running`` après EOF/stop.
            now = datetime.now().astimezone()
            for item in self._items.values():
                if item.state not in _TERMINAL_STATES:
                    item.state = "canceled"
                    item.updated_at = now
            self._active = None
            return changed

    def snapshot(self) -> list[dict[str, Any]]:
        """Retourne une copie stable pour un rendu concurrent."""
        with self._lock:
            return [
                {
                    "request_id": item.request_id,
                    "correlation_id": item.correlation_id,
                    "state": item.state,
                    "text": item.text,
                    "error": item.error,
                    "seq": item.seq,
                    "last_fragment": item.last_fragment,
                    "created_at": item.created_at.isoformat(),
                    "updated_at": item.updated_at.isoformat(),
                }
                for item in self._items.values()
            ]


class CLIConsole:
    """Terminal sobre, thread-safe et adapté aux réponses asynchrones."""

    _ACCENT = "#7C5CFC"
    _MUTED = "#8b8b8b"
    _PROGRESS = "#56CCF2"
    _RUN = "#F2C94C"
    _SUCCESS = "#27AE60"
    _ERROR = "#EB5757"
    _STATE_LABELS = {
        "queued": "QUEUE",
        "running": "RUN",
        "streaming": "STREAM",
        "succeeded": "DONE",
        "failed": "ERROR",
        "canceled": "STOP",
    }
    _STATE_ALIASES = {
        "pending": "queued",
        "waiting": "queued",
        "wait": "queued",
        "in_progress": "running",
        "in-progress": "running",
        "processing": "running",
        "success": "succeeded",
        "complete": "succeeded",
        "completed": "succeeded",
        "done": "succeeded",
        "error": "failed",
        "failure": "failed",
        "cancelled": "canceled",
        "stopped": "canceled",
        "stop": "canceled",
    }
    _STATE_STYLES = {
        "queued": "#56CCF2",
        "running": _RUN,
        "streaming": _PROGRESS,
        "succeeded": _SUCCESS,
        "failed": _ERROR,
        "canceled": _MUTED,
    }

    def __init__(
        self,
        *,
        output: Any = None,
        input_stream: Any = None,
        use_color: bool | None = None,
        show_banner: bool = True,
        name: str = "Orion",
        model: str | None = None,
        history_path: str | Path | None = "data/cli_history.txt",
        render_markdown: bool = True,
        show_timestamps: bool = True,
        usage_provider: Any = None,
        usage_ledger: Any = None,
        cost_provider: Any = None,
    ) -> None:
        # Certains flux de test ou wrappers sont volontairement falsy. Leur
        # remplacer par stdout rend l'injection non déterministe.
        self.output = sys.stdout if output is None else output
        self._uses_stdout = output is None or output is sys.stdout
        self.input_stream = sys.stdin if input_stream is None else input_stream
        self.name = name
        self.model = model
        self.show_banner = bool(show_banner)
        self.render_markdown = bool(render_markdown)
        self.show_timestamps = bool(show_timestamps)
        self._lock = threading.RLock()
        self._banner_shown = False
        self._busy = False
        self._stop_requested = threading.Event()
        self._read_generation = 0
        self.requests = CLIRequestTracker()
        self._request_short_ids: dict[str, str] = {}
        self._short_id_owners: dict[str, str] = {}
        self._last_request_id: str | None = None
        self._job_items: list[Any] = []
        self._runtime_values: dict[str, Any] = {}
        self._last_rendered_seq: dict[str, int] = {}
        # TTY presentation state.  Events can arrive on worker threads and in
        # very small fragments; retaining a little conversation state keeps
        # the visible transcript calm without changing the event contract.
        self._conversation_requests: set[str] = set()
        self._stream_open: set[str] = set()
        self._active_stream: str | None = None
        self._stream_text: dict[str, str] = {}
        self._last_activity: dict[str, str] = {}
        self._ascii = os.environ.get("ORION_ASCII") == "1"
        self._screen_reader = os.environ.get("ORION_SCREEN_READER") == "1"
        # Usage is deliberately duck-typed.  The UI can therefore be used
        # with the OpenRouter UsageLedger, a callable returning a snapshot,
        # or no ledger at all (the latter is the normal pipe/test case).
        self._usage_provider: Any = None
        self._usage_unsubscribe: Any = None
        self._usage_lock = threading.RLock()
        self._usage_cache: dict[str, Any] | None = None
        self._usage_cache_at = 0.0
        self._usage_last_invalidation = 0.0
        self._usage_refresh_interval = 0.35

        # NO_COLOR is an explicit user contract and therefore wins over an
        # integration's optimistic ``use_color=True`` preference.
        if os.environ.get("NO_COLOR") is not None:
            use_color = False
        elif use_color is None:
            use_color = bool(getattr(self.output, "isatty", lambda: False)())
        self.use_color = bool(use_color)
        self._interactive = bool(
            _PROMPT_AVAILABLE
            and getattr(self.input_stream, "isatty", lambda: False)()
            and getattr(self.output, "isatty", lambda: False)()
        )
        self._session: Any = None
        self._prompt_input: Any = None
        self._prompt_output: Any = None
        if self._interactive:
            self._session = self._build_session(history_path)
        initial_usage = next(
            (candidate for candidate in (usage_provider, usage_ledger, cost_provider) if candidate is not None),
            None,
        )
        if initial_usage is not None:
            self.set_usage_provider(initial_usage)

    def set_usage_provider(self, provider: Any = None) -> None:
        """Bind a usage snapshot provider or ledger to the TTY presentation.

        ``provider`` may be a callable, a mapping, or an object exposing
        ``snapshot()``/``usage_snapshot()``.  A ledger with ``subscribe`` is
        observed as well, so prompt-toolkit can repaint after a usage event.
        All access is defensive because the CLI is also used without an LLM.
        """
        with self._usage_lock:
            unsubscribe = self._usage_unsubscribe
            self._usage_unsubscribe = None
            self._usage_provider = provider
            self._usage_cache = None
            self._usage_cache_at = 0.0
        if callable(unsubscribe):
            try:
                unsubscribe()
            except Exception:
                pass
        subscribe = getattr(provider, "subscribe", None)
        if callable(subscribe):
            try:
                unsubscribe = subscribe(self._on_usage_event)
            except Exception:
                unsubscribe = None
            with self._usage_lock:
                self._usage_unsubscribe = unsubscribe if callable(unsubscribe) else None

    # Naming used by a few integrations before UsageLedger had a public name.
    set_usage_ledger = set_usage_provider
    set_cost_provider = set_usage_provider
    bind_usage_provider = set_usage_provider
    bind_usage_ledger = set_usage_provider

    def _on_usage_event(self, event: Any) -> None:
        snapshot = event.get("snapshot") if isinstance(event, Mapping) else None
        with self._usage_lock:
            if isinstance(snapshot, Mapping):
                self._usage_cache = dict(snapshot)
                self._usage_cache_at = time.monotonic()
        # Repainting on every token event makes prompt-toolkit expensive for a
        # stream.  Keep the toolbar live while bounding invalidations.
        now = time.monotonic()
        if now - self._usage_last_invalidation >= self._usage_refresh_interval:
            self._usage_last_invalidation = now
            self._invalidate_toolbar()

    def _invalidate_toolbar(self) -> None:
        if self._session is None or not self._interactive:
            return
        try:
            get_app().invalidate()
        except Exception:
            # The prompt may not be active (for example while stopping).
            pass

    def _usage_snapshot(self, *, force: bool = False) -> dict[str, Any] | None:
        with self._usage_lock:
            provider = self._usage_provider
            cached = dict(self._usage_cache) if self._usage_cache is not None else None
            cache_age = time.monotonic() - self._usage_cache_at
        if provider is None:
            return None
        if cached is not None and not force and cache_age < self._usage_refresh_interval:
            return cached
        value: Any = provider
        try:
            if isinstance(provider, Mapping):
                value = dict(provider)
            else:
                for method_name in ("snapshot", "usage_snapshot", "get_snapshot"):
                    method = getattr(provider, method_name, None)
                    if callable(method):
                        value = method()
                        break
                    if isinstance(method, Mapping):
                        value = method
                        break
                else:
                    if callable(provider):
                        value = provider()
            if not isinstance(value, Mapping):
                return cached
            snapshot = dict(value)
        except Exception:
            return cached
        with self._usage_lock:
            self._usage_cache = snapshot
            self._usage_cache_at = time.monotonic()
        return dict(snapshot)

    def usage_snapshot(self) -> dict[str, Any] | None:
        """Return a defensive usage snapshot for controllers and tests."""
        return self._usage_snapshot(force=True)

    @staticmethod
    def _usage_number(value: Any) -> Any:
        try:
            from decimal import Decimal

            return Decimal(str(value))
        except Exception:
            return None

    @staticmethod
    def _format_usage_cost(value: Any) -> str | None:
        number = CLIConsole._usage_number(value)
        if number is None:
            return None
        try:
            text = format(number, ".4f") if abs(number) < 1 else format(number, ".2f")
        except Exception:
            text = str(value)
        text = text.rstrip("0").rstrip(".") if "." in text else text
        return f"${text}"

    @staticmethod
    def _format_usage_tokens(value: Any) -> str:
        try:
            number = int(value or 0)
        except (TypeError, ValueError):
            return str(value or 0)
        if abs(number) >= 1_000_000:
            return f"{number / 1_000_000:.1f}m".replace(".0m", "m")
        if abs(number) >= 1_000:
            return f"{number / 1_000:.1f}k".replace(".0k", "k")
        return str(number)

    def _usage_model(self, snapshot: Mapping[str, Any]) -> str | None:
        model = snapshot.get("model")
        if model:
            return str(model)
        by_model = snapshot.get("by_model")
        if isinstance(by_model, Mapping) and len(by_model) == 1:
            return str(next(iter(by_model)))
        return self.model

    def usage_summary(self) -> str:
        """Compact cost/token summary; empty when no provider is attached."""
        snapshot = self._usage_snapshot()
        if snapshot is None:
            return ""
        calls = snapshot.get("started_calls")
        if calls is None:
            calls = (snapshot.get("completed_calls") or 0) + (snapshot.get("inflight_calls") or 0)
        try:
            call_count = int(calls or 0)
        except (TypeError, ValueError):
            call_count = 0
        if call_count == 0 and snapshot.get("call_id"):
            call_count = 1
        known_value = snapshot.get("known_cost_usd")
        estimated_value = snapshot.get("estimated_cost_usd")
        # A single record is also a useful provider contract, even when a
        # full UsageLedger snapshot is not available.
        if known_value is None and estimated_value is None and snapshot.get("cost_usd") is not None:
            source = str(snapshot.get("cost_source") or "openrouter").lower()
            if source in {"catalog_estimate", "estimate", "estimated"}:
                estimated_value = snapshot.get("cost_usd")
            else:
                known_value = snapshot.get("cost_usd")
        if known_value is None and estimated_value is None and snapshot.get("cost") is not None:
            known_value = snapshot.get("cost")
        # Empty ledgers expose Decimal(0) for both buckets.  That value is not
        # evidence of a free call and should use the unknown fallback.
        if call_count == 0 and self._usage_number(known_value) == 0:
            known_value = None
        if self._usage_number(estimated_value) == 0:
            estimated_value = None
        known = self._format_usage_cost(known_value)
        estimated = self._format_usage_cost(estimated_value)
        missing = snapshot.get("usage_missing_calls", 0)
        try:
            missing_count = int(missing or 0)
        except (TypeError, ValueError):
            missing_count = 0
        costs: list[str] = []
        if known is not None:
            costs.append(known)
        if estimated is not None:
            costs.append(f"~{estimated} est")
        if missing_count:
            costs.append("cost ?" if not costs else f"? {missing_count}")
        if not costs:
            costs.append("cost ?")
        total_tokens = snapshot.get("total_tokens")
        if total_tokens is None:
            total_tokens = (snapshot.get("prompt_tokens") or 0) + (snapshot.get("completion_tokens") or 0)
        calls = snapshot.get("started_calls")
        if calls is None:
            calls = (snapshot.get("completed_calls") or 0) + (snapshot.get("inflight_calls") or 0)
        parts = [" / ".join(costs), f"{self._format_usage_tokens(total_tokens)} tok", f"{call_count} calls"]
        model = self._usage_model(snapshot)
        if model:
            parts.insert(0, str(model))
        return " · ".join(parts)

    def usage_details(self) -> dict[str, Any]:
        """User-facing status fields, with raw values kept out of the TTY."""
        snapshot = self._usage_snapshot(force=True)
        if snapshot is None:
            return {}
        details: dict[str, Any] = {}
        model = self._usage_model(snapshot)
        if model:
            details["Modèle"] = model
        details["Coût session"] = self.usage_summary()
        details["Tokens"] = snapshot.get("total_tokens", (snapshot.get("prompt_tokens") or 0) + (snapshot.get("completion_tokens") or 0))
        details["Appels"] = snapshot.get("started_calls", snapshot.get("completed_calls", 1 if snapshot.get("call_id") else 0))
        details["En cours"] = snapshot.get("inflight_calls", 0)
        details["Appels sans usage"] = snapshot.get("usage_missing_calls", 0)
        return details

    def _console(self) -> Any:
        # Rich over StringIO would still emit panels and terminal decoration.
        # Pipes and captures stay line-oriented and stable.
        if not _RICH_AVAILABLE or not getattr(self.output, "isatty", lambda: False)():
            return None
        target = sys.stdout if self._uses_stdout else self.output
        return Console(
            file=target,
            force_terminal=self.use_color,
            no_color=not self.use_color,
            highlight=False,
            soft_wrap=False,
        )

    def _width(self, console: Any) -> int:
        try:
            # Rich reports the actual terminal width.  Do not impose a
            # dashboard-sized minimum: the conversation remains useful on a
            # split pane or a narrow CI terminal too.
            return max(1, int(getattr(console, "width", 80)))
        except (TypeError, ValueError):
            return 80

    @staticmethod
    def _clip(value: Any, width: int, *, marker: str = "…") -> str:
        text = " ".join(str(value or "").split())
        if width <= 0:
            return ""
        if _rich_cell_len(text) <= width:
            return text
        marker_width = _rich_cell_len(marker)
        if width <= marker_width:
            result = ""
            visible = 0
            for char in marker:
                char_width = _rich_cell_len(char)
                if visible + char_width > width:
                    break
                result += char
                visible += char_width
            return result
        available = width - marker_width
        result = ""
        visible = 0
        for char in text:
            char_width = _rich_cell_len(char)
            if visible + char_width > available:
                break
            result += char
            visible += char_width
        return result.rstrip() + marker

    @staticmethod
    def _canonical_state(state: Any) -> str:
        value = str(state or "queued").strip().lower()
        return CLIConsole._STATE_ALIASES.get(value, value)

    @staticmethod
    def _take_cells(value: str, width: int) -> str:
        """Return the leading text that fits in ``width`` terminal cells."""
        if width <= 0:
            return ""
        result: list[str] = []
        used = 0
        for char in str(value):
            char_width = _rich_cell_len(char)
            if used + char_width > width:
                break
            result.append(char)
            used += char_width
        return "".join(result)

    @classmethod
    def _pad_cells(cls, value: Any, width: int) -> str:
        text = cls._take_cells(str(value), width)
        return text + (" " * max(0, width - _rich_cell_len(text)))

    def _short_id(self, request_id: str | None) -> str:
        """Return a stable readable request id, extending collisions."""
        identifier = str(request_id or "")
        if not identifier:
            return "------"
        existing = self._request_short_ids.get(identifier)
        if existing:
            return existing
        if len(identifier) <= 6:
            self._request_short_ids[identifier] = identifier
            self._short_id_owners[identifier] = identifier
            return identifier
        for size in (4, 6, 8, 10, 12, len(identifier)):
            candidate = identifier[:size]
            owner = self._short_id_owners.get(candidate)
            if owner is None or owner == identifier:
                self._request_short_ids[identifier] = candidate
                self._short_id_owners[candidate] = identifier
                return candidate
        candidate = identifier
        self._request_short_ids[identifier] = candidate
        self._short_id_owners[candidate] = identifier
        return candidate

    def _state_label(self, state: str | None, *, waiting: bool = False) -> str:
        if waiting:
            return "WAIT"
        canonical = self._canonical_state(state)
        return self._STATE_LABELS.get(canonical, canonical.upper() or "QUEUE")

    def _state_style(self, state: str | None) -> str:
        return self._STATE_STYLES.get(self._canonical_state(state), self._MUTED)

    def _print_tty_line(self, console: Any, value: str, *, style: str | None = None) -> None:
        width = self._width(console)
        clipped = self._clip(value, width, marker="..." if self._ascii else "…")
        console.print(Text(clipped, style=style) if _RICH_AVAILABLE else clipped)

    def _print_tty_wrapped(
        self,
        console: Any,
        value: str,
        *,
        indent: str = "",
        style: str | None = None,
    ) -> None:
        """Print conversation text without silently dropping its tail."""
        width = self._width(console)
        available = max(1, width - _rich_cell_len(indent))
        for source in str(value).splitlines() or [""]:
            chunks = textwrap.wrap(
                source,
                width=available,
                replace_whitespace=False,
                drop_whitespace=True,
                break_long_words=True,
                break_on_hyphens=False,
            ) or [""]
            for chunk in chunks:
                # ``textwrap`` counts code points. Split any resulting chunk
                # again by terminal cells so CJK and emoji cannot overflow a
                # narrow Rich surface.
                while _rich_cell_len(chunk) > available:
                    visible = self._take_cells(chunk, available)
                    if not visible:  # one wide glyph on a one-cell surface
                        visible = chunk[0]
                    line = indent + visible
                    console.print(Text(line, style=style) if _RICH_AVAILABLE else line)
                    chunk = chunk[len(visible) :]
                line = indent + chunk
                # ``textwrap`` counts characters and Rich may add no visible
                # ANSI when NO_COLOR is active.  Keep the hard invariant for
                # test terminals and terminals with unusual Unicode widths.
                if _rich_cell_len(line) > width:
                    line = self._take_cells(line, width)
                console.print(Text(line, style=style) if _RICH_AVAILABLE else line)

    def _glyph(self, utf8: str, ascii_value: str) -> str:
        return ascii_value if self._ascii else utf8

    def _activity_line(self, console: Any, text: str, *, key: str = "global", style: str | None = None) -> None:
        """Emit one deduplicated, low prominence activity annotation."""
        normalized = " ".join(str(text).split())
        if not normalized or self._last_activity.get(key) == normalized:
            return
        self._last_activity[key] = normalized
        self._print_tty_wrapped(console, f"  {self._glyph('·', '-')} {normalized}", style=style or self._MUTED)

    def _conversation_prompt(self, text: str) -> None:
        console = self._console()
        if console is None:
            return
        value = " ".join(str(text).split())
        if not value:
            return
        with self._lock:
            self._print_tty_wrapped(
                console,
                f"Vous  {self._glyph('›', '>')}  {value}",
                style=self._MUTED,
            )

    def _open_stream(self, console: Any, request_id: str) -> None:
        if request_id in self._stream_open:
            self._active_stream = request_id
            return
        # A terminal has one cursor.  Close a previous inline block before a
        # concurrent request starts so fragments can never share a line.
        for previous in tuple(self._stream_open):
            self._close_stream(console, previous)
        self._stream_open.add(request_id)
        self._active_stream = request_id
        self._stream_text.setdefault(request_id, "")
        header = Text(self.name, style=f"bold {self._ACCENT}")
        console.print(header)

    def _append_stream(self, console: Any, request_id: str, text: str) -> None:
        """Append stream fragments to one Orion block.

        A fragment is written without a newline, so a token-heavy provider
        cannot turn the transcript into one line per token.  Newlines from the
        provider are preserved and the final call closes the block.
        """
        fragment = str(text or "")
        if not fragment:
            return
        if self._screen_reader:
            # Inline token output is difficult to follow when a terminal is
            # being spoken. Emit a complete, stable record per update.
            self._print_tty_wrapped(
                console,
                f"{self.name} {self._glyph('·', '-')} STREAM req {self._short_id(request_id)}",
                style=self._PROGRESS,
            )
            self._print_tty_wrapped(console, fragment, indent="  ")
            self._close_stream(console, request_id)
            return
        self._open_stream(console, request_id)
        self._stream_text[request_id] = self._stream_text.get(request_id, "") + fragment
        # Rich's ``end`` is deliberately used here: all fragments stay in the
        # same visual block, while explicit provider newlines still work.
        if not fragment.startswith("\n") and not self._stream_text[request_id].startswith("\n"):
            prefix = "  " if len(self._stream_text[request_id]) == len(fragment) else ""
        else:
            prefix = ""
        rendered = prefix + fragment
        if _RICH_AVAILABLE:
            console.print(Text(rendered), end="")
        else:  # pragma: no cover - Rich is optional at runtime
            print(rendered, file=self.output, end="", flush=True)

    def _close_stream(self, console: Any, request_id: str) -> None:
        if request_id not in self._stream_open:
            return
        # Ensure the next conversation block starts on a fresh line.  The
        # newline itself is not a status marker and therefore stays quiet.
        if _RICH_AVAILABLE:
            console.print()
        else:  # pragma: no cover
            print(file=self.output, flush=True)
        self._stream_open.discard(request_id)
        if self._active_stream == request_id:
            self._active_stream = None

    def _close_active_stream(self, console: Any, *, except_request: str | None = None) -> None:
        """Close the inline stream before writing another terminal block."""
        active = self._active_stream
        if active and active != except_request and active in self._stream_open:
            self._close_stream(console, active)

    def _response_block(self, console: Any, content: str, *, request_id: str | None = None, style: str | None = None) -> None:
        self._close_active_stream(console, except_request=request_id)
        stream_id = request_id if request_id and request_id in self._stream_open else "stream"
        had_stream = stream_id in self._stream_open
        if had_stream:
            self._close_stream(console, stream_id)
        if not had_stream:
            header = Text(self.name, style=f"bold {self._ACCENT}")
            console.print(header)
        self._print_tty_wrapped(console, content, indent="  ", style=style)

    def _request_line(
        self,
        console: Any,
        request_id: str,
        state: str | None,
        text: str = "",
        *,
        waiting: bool = False,
        timestamp: str | None = None,
    ) -> None:
        # Kept as a compatibility helper for integrations that used the
        # private method.  The normal transcript uses conversational wording
        # and leaves canonical state names to /requests and /status.
        normalized = str(text or "demande en cours")
        if state in {"failed", "canceled"}:
            self._activity_line(
                console,
                normalized,
                key=f"request:{request_id}:{state}",
                style=self._ERROR if state == "failed" else self._MUTED,
            )
            return
        if waiting:
            self._activity_line(console, normalized, key=f"request:{request_id}:wait", style=self._RUN)

    def _job_line(self, console: Any, item: Any, index: int, total: int) -> None:
        if isinstance(item, Mapping):
            identifier = item.get("id") or item.get("job_id") or item.get("request_id") or "job"
            state = self._state_label(item.get("status") or item.get("state") or "queued")
            owner = item.get("owner") or item.get("instance") or item.get("team") or "worker"
            objective = item.get("objective") or item.get("label") or item.get("name") or ""
        else:
            identifier, state, owner, objective = "job", "WAIT", "worker", str(item)
        width = self._width(console)
        marker = "..." if self._ascii else "…"
        # The identifier column is semantic: keep its prefix intact even on
        # ASCII terminals instead of spending half the six-cell budget on an
        # ellipsis (``job-1`` is more useful than ``job...``).
        short_identifier = self._clip(identifier, 6, marker="")
        # Jobs are an operator view, not a tree attached to every response.
        # Keep one compact row per job and let the objective wrap naturally.
        prefix = (
            "  "
            + self._pad_cells(short_identifier, 6)
            + " "
            + self._pad_cells(self._clip(state, 6), 6)
            + " "
            + self._pad_cells(self._clip(owner, 16, marker=marker), 16)
            + "  "
        )
        self._print_tty_line(
            console,
            prefix + self._clip(objective, max(1, width - _rich_cell_len(prefix)), marker=marker),
            style=self._MUTED,
        )

    def _build_session(self, history_path: str | Path | None) -> Any:
        # PromptSession attend les abstractions Input/Output de prompt_toolkit,
        # pas des TextIO bruts. Les garder en attribut évite leur destruction
        # prématurée et permet aussi d'injecter un terminal dans les tests.
        self._prompt_input = create_input(self.input_stream)
        self._prompt_output = create_output(self.output)
        key_bindings = KeyBindings()

        @key_bindings.add("enter")
        def submit(event: Any) -> None:
            event.current_buffer.validate_and_handle()

        @key_bindings.add("escape", "enter")
        def newline(event: Any) -> None:
            event.current_buffer.insert_text("\n")

        history: Any = InMemoryHistory()
        if history_path:
            path = Path(history_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            history = FileHistory(str(path))

        style = Style.from_dict(
            {
                "prompt": f"bold {self._ACCENT}",
                "continuation": self._MUTED,
                "bottom-toolbar": "bg:#252525 #b7b7b7",
                "completion-menu.completion": "bg:#252525 #d0d0d0",
                "completion-menu.completion.current": f"bg:{self._ACCENT} #ffffff",
            }
        )
        return PromptSession(
            multiline=True,
            history=history,
            auto_suggest=AutoSuggestFromHistory(),
            completer=WordCompleter(list(_COMMANDS), sentence=True),
            complete_while_typing=False,
            enable_history_search=True,
            key_bindings=key_bindings,
            style=style,
            mouse_support=False,
            input=self._prompt_input,
            output=self._prompt_output,
        )

    def _toolbar(self) -> FormattedText:
        if self._screen_reader:
            return FormattedText([])
        usage = self.usage_summary()
        usage_text = f"  {self._glyph('·', '-')} {usage}  " if usage else ""
        if self._busy:
            fragments = [
                ("class:bottom-toolbar", f"  {self._glyph('·', '-')} travail en cours  "),
                ("class:bottom-toolbar", f"{self._glyph('·', '-')} /status · /stop  "),
            ]
        else:
            fragments = [
                ("class:bottom-toolbar", "  Entrée envoyer  "),
                ("class:bottom-toolbar", f"{self._glyph('·', '-')} Alt+Entrée ligne  "),
                ("class:bottom-toolbar", f"{self._glyph('·', '-')} /help aide  "),
            ]
        if usage_text:
            fragments.append(("class:bottom-toolbar", usage_text))
        return FormattedText(fragments)

    def _print_plain(self, content: str = "", *, end: str = "\n") -> None:
        with self._lock:
            print(content, file=self.output, end=end, flush=True)

    def banner(self) -> None:
        if self._banner_shown or not self.show_banner:
            return
        self._banner_shown = True
        console = self._console()
        if console is None:
            separator = self._glyph("·", "-")
            ready = "pret" if self._ascii else "prêt"
            details = "Evenements - tools - taches durables" if self._ascii else f"Événements {separator} tools {separator} tâches durables"
            self._print_plain(f"\n{self.name} {separator} {ready}")
            self._print_plain(details + "\n")
            return
        width = self._width(console)
        model = self._clip(
            self.model or ("modele inconnu" if self._ascii else "modèle inconnu"),
            max(8, width - len(self.name) - 5),
            marker="..." if self._ascii else "…",
        )
        separator = self._glyph("·", "-")
        line = f"{self.name} {separator} {model}"
        usage = self.usage_summary()
        if usage:
            # Usage remains available to existing provider integrations, but
            # is visually subordinate to the model identity.
            line += f"  {separator} {usage}"
        with self._lock:
            self._print_tty_line(console, line, style=f"bold {self._ACCENT}")

    def read(self, prompt: str = "❯ ") -> str:
        if self._stop_requested.is_set():
            raise EOFError
        if self._ascii:
            prompt = prompt.replace("❯", ">")
        if self._session is None:
            stream = self.input_stream
            self._print_plain(prompt, end="")
            result: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
            self._read_generation += 1
            generation = self._read_generation

            def read_line() -> None:
                try:
                    value = stream.readline()
                except BaseException as exc:
                    try:
                        result.put_nowait(("error", exc))
                    except queue.Full:
                        pass
                    return
                try:
                    result.put_nowait(("value", value))
                except queue.Full:
                    # stop() peut avoir libéré read() avant que le flux ne
                    # réponde. Le thread daemon ne doit jamais rester bloqué.
                    pass

            threading.Thread(target=read_line, name=f"orion-cli-read-{generation}", daemon=True).start()
            while not self._stop_requested.is_set():
                try:
                    kind, value = result.get(timeout=0.05)
                except queue.Empty:
                    continue
                if self._stop_requested.is_set():
                    raise EOFError
                if kind == "error":
                    raise value
                if value == "":
                    raise EOFError
                if isinstance(value, bytes):
                    value = value.decode("utf-8", errors="replace")
                return str(value).rstrip("\r\n")
            raise EOFError
        with patch_stdout(raw=True):
            display_prompt = f"Vous  {self._glyph('›', '>')}  "
            return self._session.prompt(
                FormattedText([("class:prompt", display_prompt)]),
                prompt_continuation=lambda *_: FormattedText(
                    [("class:continuation", self._glyph("│", "|" ) + " ")]
                ),
                bottom_toolbar=self._toolbar,
                reserve_space_for_menu=4,
            )

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        # La réponse arrive souvent depuis un worker. Sans invalidation,
        # prompt_toolkit peut conserver l'ancien texte de la toolbar à l'écran.
        self._invalidate_toolbar()

    def stop(self) -> bool:
        """Demande l'arret et reveille une lecture injectee; idempotent."""
        was_set = self._stop_requested.is_set()
        self._stop_requested.set()
        self.requests.stop()
        if self._session is not None:
            try:
                get_app().exit(exception=EOFError())
            except Exception:
                pass
        return not was_set

    def cancel_active(self) -> dict[str, Any] | None:
        item = self.requests.cancel()
        return None if item is None else self.requests.status(item.request_id)

    def new_request(
        self,
        text: str = "",
        *,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> CLIRequest:
        item = self.requests.create(text, request_id=request_id, correlation_id=correlation_id)
        self._short_id(item.request_id)
        self._last_request_id = item.request_id
        return item

    def update_request(
        self,
        request_id: str,
        state: str,
        *,
        error: str | None = None,
        text: str | None = None,
        seq: int | None = None,
    ) -> dict[str, Any]:
        """Fait progresser une requête et renvoie son snapshot sérialisable.

        Cette façade est destinée aux adaptateurs (CLI, gateway, tests) afin
        qu'ils n'aient pas à dépendre de l'implémentation interne du tracker.
        Elle lève ``KeyError`` pour un identifiant inconnu et ``ValueError``
        pour une transition invalide.
        """
        item = self.requests.update(request_id, state, error=error, text=text, seq=seq)
        snapshot = self.requests.status(item.request_id)
        # L'entrée existe tant que l'appel vient de réussir ; cette assertion
        # garde un type simple pour les intégrateurs et les vérificateurs.
        if snapshot is None:  # pragma: no cover - garde défensive
            raise RuntimeError("Requête CLI introuvable après mise à jour.")
        return snapshot

    def record_fragment(self, request_id: str, text: str, *, seq: int) -> dict[str, Any]:
        item = self.requests.record_fragment(request_id, text, seq=seq)
        snapshot = self.requests.status(item.request_id)
        if snapshot is None:  # pragma: no cover
            raise RuntimeError("Requête CLI introuvable après fragment.")
        return snapshot

    def request_status(self, request_id: str | None = None) -> dict[str, Any] | None:
        """Retourne le snapshot de la requête demandée ou active."""
        return self.requests.status(request_id)

    def render_event(self, event: CLIEvent | Mapping[str, Any]) -> str:
        """Render a normalized event in the conversational TTY transcript.

        The event payload remains canonical; only its TTY projection is
        grouped.  In particular, streaming fragments share one Orion block
        and terminal states do not leak ``RUN``/``DONE`` labels into prose.
        """
        payload = event.as_dict() if isinstance(event, CLIEvent) else dict(event)
        kind = str(payload.get("kind", "event"))
        if self._console() is None:
            return CLIEventRenderer(self.output, mode="plain").render(payload)
        console = self._console()
        request_id = str(payload.get("request_id") or "")
        seq = int(payload.get("seq") or 0)
        if request_id and seq and seq < self._last_rendered_seq.get(request_id, -1):
            return ""
        if request_id and seq:
            self._last_rendered_seq[request_id] = seq
        with self._lock:
            if kind == "session.ready":
                self._banner_shown = False
                self.banner()
                return kind
            if kind.startswith("request.") and request_id:
                state = self._canonical_state(payload.get("state") or kind.split(".", 1)[1])
                text = str(payload.get("text") or payload.get("error") or "")
                if request_id not in self._conversation_requests:
                    self._conversation_requests.add(request_id)
                    if text and state in {"queued", "running"}:
                        self._conversation_prompt(text)
                meta = payload.get("meta") if isinstance(payload.get("meta"), Mapping) else {}
                if state in {"queued", "running"}:
                    activity = meta.get("activity") or meta.get("summary")
                    if self._screen_reader:
                        detail = str(activity or ("en attente" if state == "queued" else "travail en cours"))
                        self._activity_line(
                            console,
                            f"{self._state_label(state)} req {self._short_id(request_id)} - {detail}",
                            key=f"request:{request_id}:{state}",
                            style=self._RUN if state == "running" else self._PROGRESS,
                        )
                    elif activity:
                        self._activity_line(console, str(activity), key=f"request:{request_id}:activity")
                elif state == "streaming":
                    if self._screen_reader:
                        self._print_tty_wrapped(
                            console,
                            f"{self.name} {self._glyph('·', '-')} STREAM req {self._short_id(request_id)}",
                            style=self._PROGRESS,
                        )
                        self._print_tty_wrapped(console, text, indent="  ")
                    else:
                        self._append_stream(console, request_id, text)
                elif state == "succeeded":
                    had_stream = request_id in self._stream_open
                    self._close_stream(console, request_id)
                    if self._screen_reader:
                        self._print_tty_wrapped(
                            console,
                            f"{self.name} {self._glyph('·', '-')} {self._state_label(state)} req {self._short_id(request_id)}",
                            style=self._SUCCESS,
                        )
                    if text:
                        if had_stream:
                            self._print_tty_wrapped(console, text, indent="  ")
                        else:
                            self._response_block(console, text, request_id=request_id)
                    self._emit_usage_note(console, request_id, payload, terminal=True)
                elif state == "failed":
                    self._close_active_stream(console, except_request=request_id)
                    self._close_stream(console, request_id)
                    failure_heading = (
                        f"{self.name} {self._glyph('·', '-')} {self._state_label(state)} req {self._short_id(request_id)}"
                        if self._screen_reader
                        else f"{self.name} {self._glyph('·', '-')} erreur"
                    )
                    self._print_tty_wrapped(console, failure_heading, style=f"bold {self._ERROR}")
                    self._print_tty_wrapped(console, text or "La demande a échoué.", indent="  ", style=self._ERROR)
                    details = f"demande req {self._short_id(request_id)} {self._glyph('·', '-')} /retry {self._short_id(request_id)} {self._glyph('·', '-')} /debug"
                    self._print_tty_wrapped(console, details, indent="  ", style=self._MUTED)
                elif state == "canceled":
                    self._close_active_stream(console, except_request=request_id)
                    self._close_stream(console, request_id)
                    canceled_text = (
                        f"{self._state_label(state)} req {self._short_id(request_id)} - annulation demandée"
                        if self._screen_reader
                        else f"annulation demandée pour req {self._short_id(request_id)}"
                    )
                    self._activity_line(console, canceled_text, key=f"request:{request_id}:canceled")
                return f"{kind} req {self._short_id(request_id)}"
            if kind.startswith("job."):
                meta = payload.get("meta") if isinstance(payload.get("meta"), Mapping) else payload
                # Job updates stay quiet in the normal conversation.  An
                # explicit summary may still be supplied by a controller.
                summary = meta.get("summary") or meta.get("activity")
                if summary:
                    self._activity_line(console, str(summary), key=f"job:{meta.get('id') or meta.get('job_id')}")
                return f"{kind} job"
            text = payload.get("text") or payload.get("error") or kind
            self._activity_line(console, str(text), key=f"event:{kind}:{text}")
            return str(text)

    def _emit_usage_note(
        self,
        console: Any,
        request_id: str,
        payload: Mapping[str, Any],
        *,
        terminal: bool = False,
    ) -> None:
        """Add one compact provider metric annotation when data is present."""
        meta = payload.get("meta") if isinstance(payload.get("meta"), Mapping) else {}
        usage = meta.get("usage_summary") or meta.get("usage")
        if not usage and terminal:
            usage = self.usage_summary()
        if usage:
            self._activity_line(
                console,
                f"req {self._short_id(request_id)} {self._glyph('·', '-')} {usage}",
                key=f"usage:{request_id}",
            )

    def _time_label(self, timestamp: str | None) -> str:
        if not self.show_timestamps:
            return ""
        try:
            moment = datetime.fromisoformat(timestamp) if timestamp else datetime.now().astimezone()
            return moment.astimezone().strftime("%H:%M")
        except (TypeError, ValueError):
            return ""

    def assistant(
        self,
        content: str,
        *,
        intermediate: bool = False,
        timestamp: str | None = None,
    ) -> None:
        content = str(content).strip()
        if not content:
            return
        console = self._console()
        if console is None:
            label = "progression" if intermediate else self.name
            self._print_plain(f"\n{label}> {content}\n")
            return
        with self._lock:
            if intermediate:
                active = self.requests.status()
                request_id = active["request_id"] if active else "stream"
                self._append_stream(console, request_id, content)
            else:
                active = self.requests.status()
                if active is None and self._last_request_id:
                    active = self.requests.status(self._last_request_id)
                request_id = active["request_id"] if active else None
                self._response_block(console, content, request_id=request_id)
                if request_id:
                    usage = self.usage_summary()
                    if usage:
                        self._activity_line(
                            console,
                            f"req {self._short_id(request_id)} {self._glyph('·', '-')} {usage}",
                            key=f"usage:{request_id}",
                        )

    def system(self, content: str) -> None:
        console = self._console()
        if console is None:
            self._print_plain(content)
            return
        with self._lock:
            self._print_tty_wrapped(console, f"Système {self._glyph('·', '-')} {content}", style=self._MUTED)

    def warning(self, content: str) -> None:
        console = self._console()
        if console is None:
            self._print_plain(f"Attention : {content}")
            return
        with self._lock:
            self._activity_line(console, content, key=f"warning:{content}", style=self._RUN)

    def error(self, content: str) -> None:
        console = self._console()
        if console is None:
            self._print_plain(f"Erreur : {content}")
            return
        with self._lock:
            self._print_tty_wrapped(console, f"{self.name} {self._glyph('·', '-')} erreur", style=f"bold {self._ERROR}")
            self._print_tty_wrapped(console, content, indent="  ", style=self._ERROR)

    def clear(self) -> None:
        console = self._console()
        if console is not None:
            console.clear()
        elif getattr(self.output, "isatty", lambda: False)():
            os.system("cls" if os.name == "nt" else "clear")
        else:
            # Un pipe ne doit pas exécuter cls/clear ni recevoir des séquences
            # ANSI dans un journal. On conserve seulement la frontière visuelle.
            self._print_plain("")
        self._banner_shown = False
        self.banner()

    @staticmethod
    def _help_usage(name: str, spec: tuple[int, int, frozenset[str]]) -> str:
        min_args, max_args, _ = spec
        if min_args == max_args == 0:
            arguments = ""
        elif min_args:
            arguments = " <argument>" if min_args == max_args == 1 else " <arguments...>"
        else:
            arguments = " [argument]"
        options = sorted(spec[2])
        suffix = "" if not options else " " + " ".join(f"[--{option}]" for option in options)
        return f"/{name}{arguments}{suffix}"

    @classmethod
    def _help_rows(cls) -> list[tuple[str, str, str]]:
        """Build help from the parser registry so valid commands cannot drift."""
        aliases: dict[str, list[str]] = {}
        for alias, target in _COMMAND_ALIASES.items():
            aliases.setdefault(target, []).append(f"/{alias}")
        rows: list[tuple[str, str, str]] = []
        for command in _COMMANDS:
            name = command.lstrip("/")
            usage = cls._help_usage(name, _COMMAND_SPECS[name])
            description = _COMMAND_DESCRIPTIONS.get(name, "Commande locale")
            alias_text = ", ".join(sorted(aliases.get(name, ())))
            rows.append((usage, description, alias_text))
        return rows

    @staticmethod
    def _help_lines(rows: Sequence[tuple[str, str, str]], width: int | None = None) -> list[str]:
        """Wrap descriptions while preserving every command on narrow TTYs."""
        max_usage = max((len(usage) for usage, _, _ in rows), default=0)
        if width is None:
            width = max_usage + 40
        if width < max_usage + 12:
            lines: list[str] = []

            def add_wrapped(prefix: str, value: str) -> None:
                chunks = textwrap.wrap(
                    value,
                    width=max(1, width - len(prefix)),
                    break_long_words=True,
                    break_on_hyphens=False,
                ) or [""]
                lines.extend(prefix + chunk for chunk in chunks)

            for usage, description, aliases in rows:
                add_wrapped("  ", usage)
                add_wrapped("      ", description)
                if aliases:
                    add_wrapped("      ", f"alias: {aliases}")
            return lines
        description_width = max(12, width - max_usage - 5)
        lines = []
        for usage, description, aliases in rows:
            detail = description + (f" (alias: {aliases})" if aliases else "")
            wrapped = textwrap.wrap(detail, width=description_width) or [""]
            lines.append(f"  {usage:<{max_usage}}  {wrapped[0]}")
            lines.extend(f"  {' ' * max_usage}  {part}" for part in wrapped[1:])
        return lines

    def help(self) -> None:
        rows = self._help_rows()
        console = self._console()
        if console is None:
            self._print_plain("\nCOMMANDES\n" + "\n".join(self._help_lines(rows)))
            return
        groups = {
            "Conversation": {"help", "clear", "exit"},
            "Observation": {"status", "requests", "jobs", "agents", "tasks", "tools"},
            "Contrôle": {"stop", "retry", "debug"},
        }
        by_name = {usage.split()[0].lstrip("/"): (usage, description, aliases) for usage, description, aliases in rows}
        with self._lock:
            self._print_tty_line(console, "Commandes Orion", style=f"bold {self._ACCENT}")
            for group, names in groups.items():
                self._print_tty_line(console, group, style=self._MUTED)
                selected = [by_name[name] for name in _COMMAND_SPECS if name in names]
                for line in self._help_lines(selected, self._width(console)):
                    self._print_tty_line(console, line)
            # Configuration extensions may add registry entries in a future
            # controller.  Keep this renderer exhaustive if that happens.
            grouped_names = set().union(*groups.values())
            extras = [row for name, row in by_name.items() if name not in grouped_names]
            if extras:
                self._print_tty_line(console, "Autres", style=self._MUTED)
                for line in self._help_lines(extras, self._width(console)):
                    self._print_tty_line(console, line)
            self._print_tty_line(console, "Astuce : /help <commande> affiche l’usage et les options.", style=self._MUTED)

    def _legacy_help(self) -> None:
        rows = [
            ("/help", "Afficher les commandes"),
            ("/status", "État du runtime et tâche active"),
            ("/tools", "Tools disponibles pour Orion"),
            ("/tasks", "Dernières tâches durables"),
            ("/agents", "Sous-agents disponibles"),
            ("/jobs", "Travaux délégués récents"),
            ("/clear", "Nettoyer l'écran"),
            ("/stop", "Annuler la requête active"),
            ("/exit", "Arrêter Orion proprement"),
        ]
        console = self._console()
        if console is None:
            self._print_plain("\n" + "\n".join(f"{name:10} {description}" for name, description in rows))
            return
        with self._lock:
            self._print_tty_line(console, " COMMANDES", style=f"bold {self._ACCENT}")
            for command, description in rows:
                self._print_tty_line(console, f"   {command:<12} {description}")

    def status(self, values: Mapping[str, Any]) -> None:
        self._runtime_values = dict(values)
        usage = self.usage_details()
        if usage:
            # Keep provider-owned status keys intact while making usage visible
            # to /status and /debug without requiring a particular ledger type.
            self._runtime_values.update({key: value for key, value in usage.items() if key not in self._runtime_values})
        console = self._console()
        if console is None:
            self._print_plain("\n" + "\n".join(f"{key}: {value}" for key, value in self._runtime_values.items()))
            return
        with self._lock:
            self._print_tty_line(console, "Status Orion", style=f"bold {self._ACCENT}")
            for key, value in self._runtime_values.items():
                self._print_tty_wrapped(console, f"  {key}: {value}")

    def items(self, title: str, items: Sequence[Any], *, empty: str) -> None:
        self._job_items = list(items) if title.lower().find("travaux") >= 0 or title.lower().find("jobs") >= 0 else self._job_items
        console = self._console()
        if not items:
            self.system(empty)
            return
        if console is None:
            self._print_plain("\n" + title)
            for item in items:
                self._print_plain(f"- {item}")
            return
        with self._lock:
            heading = "Jobs" if title.lower().find("travaux") >= 0 or title.lower().find("jobs") >= 0 else title
            self._print_tty_line(console, heading, style=f"bold {self._ACCENT}")
            for index, item in enumerate(items):
                self._job_line(console, item, index, len(items))


__all__ = [
    "CLICommand",
    "CLICommandParser",
    "CLIConsole",
    "CLIEvent",
    "CLIEventRenderer",
    "CLIJSONLRenderer",
    "CLIParseError",
    "CLIRequest",
    "CLIRequestTracker",
    "CLIUserInput",
    "REQUEST_STATES",
    "parse_cli_input",
]
