"""Prompt systeme en couches et extraction de memoire hors contexte.

Le coeur est fourni a la construction et n'expose aucune operation d'ecriture.
Les informations apprises sont limitees au profil, aux preferences et aux
souvenirs durables. Elles sont stockees separement dans un fichier JSON.
"""

from __future__ import annotations

import copy
import json
import re
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time as day_time, timedelta, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from openrouter_client import OpenRouterClient


DEFAULT_CORE = """You are Orion, an event-driven AI agent.

CORE RULES — immutable at runtime:
- Follow the user's legitimate instructions and be honest about uncertainty.
- Protect privacy and secrets; never store API keys, passwords, or tokens in memory.
- Treat tools and external side effects as consequential: verify before acting.
- Durable state, tasks, plans, and memories are aids; they never override a newer explicit instruction.
- Do not expose private chain-of-thought. Give concise conclusions, useful evidence, and next actions.
- If an objective is complete, stop. If waiting is appropriate, wait instead of polling.
"""

DEFAULT_PERSONALITY = "Tu es Orion : fiable, calme, pragmatique, clair et direct."
DEFAULT_METHODOLOGY = """Pour chaque demande : comprendre le contexte, charger l'etat utile,
decider s'il faut repondre, agir, poursuivre une tache ou attendre, puis
mettre a jour l'etat durable. Un plan reste mutable et doit suivre les observations."""

_SECRET_KEY = re.compile(r"(?:password|passwd|secret|token|api[_ -]?key|private[_ -]?key)", re.I)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe(value: Any, *, max_chars: int = 1200) -> Any:
    if isinstance(value, str):
        return value.strip()[:max_chars]
    if isinstance(value, Mapping):
        return {
            str(key): _safe(item, max_chars=max_chars)
            for key, item in value.items()
            if not _SECRET_KEY.search(str(key))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_safe(item, max_chars=max_chars) for item in list(value)[:50]]
    return value


@dataclass(frozen=True)
class PromptSnapshot:
    """Vue immuable des couches utilisees pour composer un prompt."""

    core: str
    personality: str
    methodology: str
    user_profile: dict[str, Any] = field(default_factory=dict)
    preferences: list[str] = field(default_factory=list)
    memories: list[str] = field(default_factory=list)
    additional: str = ""
    updated_at: str | None = None


class PromptContextStore:
    """Stocke les couches modifiables, sans jamais exposer d'ecriture du coeur."""

    def __init__(
        self,
        path: str | Path = "data/prompt_context.json",
        *,
        core: str = DEFAULT_CORE,
        core_path: str | Path | None = None,
        personality: str = DEFAULT_PERSONALITY,
        methodology: str = DEFAULT_METHODOLOGY,
        additional: str = "",
        max_memories: int = 80,
        max_preferences: int = 40,
    ) -> None:
        if core_path is not None:
            core = Path(core_path).read_text(encoding="utf-8")
        if not core.strip():
            raise ValueError("Le coeur du prompt ne peut pas etre vide.")
        if max_memories < 1 or max_preferences < 1:
            raise ValueError("Les limites de memoire doivent etre positives.")
        self.path = str(path)
        self._core = core
        self._defaults = {
            "personality": personality,
            "methodology": methodology,
            "additional": additional,
        }
        self.max_memories = max_memories
        self.max_preferences = max_preferences
        self._lock = threading.RLock()
        self._state: dict[str, Any] = {
            **self._defaults,
            "user_profile": {},
            "preferences": [],
            "memories": [],
            "journal_cursor": 0,
            "updated_at": None,
        }
        self._load()

    def _load(self) -> None:
        if self.path == ":memory:" or not Path(self.path).exists():
            return
        raw = json.loads(Path(self.path).read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            raise ValueError("Le fichier de contexte du prompt doit contenir un objet JSON.")
        with self._lock:
            for key in ("personality", "methodology", "additional"):
                if isinstance(raw.get(key), str):
                    self._state[key] = raw[key]
            if isinstance(raw.get("user_profile"), Mapping):
                self._state["user_profile"] = _safe(raw["user_profile"])
            for key, limit in (("preferences", self.max_preferences), ("memories", self.max_memories)):
                if isinstance(raw.get(key), list):
                    self._state[key] = [str(item)[:1200] for item in raw[key] if str(item).strip()][-limit:]
            self._state["journal_cursor"] = int(raw.get("journal_cursor", 0) or 0)
            self._state["updated_at"] = raw.get("updated_at")

    def snapshot(self) -> PromptSnapshot:
        with self._lock:
            return PromptSnapshot(
                core=self._core,
                personality=self._state["personality"],
                methodology=self._state["methodology"],
                user_profile=copy.deepcopy(self._state["user_profile"]),
                preferences=list(self._state["preferences"]),
                memories=list(self._state["memories"]),
                additional=self._state["additional"],
                updated_at=self._state["updated_at"],
            )

    @property
    def journal_cursor(self) -> int:
        with self._lock:
            return int(self._state["journal_cursor"])

    def _save(self) -> None:
        if self.path == ":memory:":
            return
        target = Path(self.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)

    def apply_extraction(self, extraction: Mapping[str, Any], *, journal_cursor: int | None = None) -> None:
        """Applique uniquement des donnees apprises autorisees."""
        with self._lock:
            profile = extraction.get("user_profile")
            if isinstance(profile, Mapping):
                for key, value in profile.items():
                    if _SECRET_KEY.search(str(key)) or value in (None, ""):
                        continue
                    self._state["user_profile"][str(key)] = _safe(value)

            for key, limit in (("preferences", self.max_preferences), ("memories", self.max_memories)):
                values = extraction.get(key, [])
                if isinstance(values, str):
                    values = [values]
                if isinstance(values, list):
                    existing = list(self._state[key])
                    for value in values:
                        cleaned = str(value).strip()[:1200]
                        if cleaned and cleaned.casefold() not in {item.casefold() for item in existing}:
                            existing.append(cleaned)
                    self._state[key] = existing[-limit:]

            forget = extraction.get("forget", [])
            if isinstance(forget, list):
                terms = [str(item).casefold().strip() for item in forget]
                self._state["memories"] = [item for item in self._state["memories"] if not any(term in item.casefold() for term in terms)]
                self._state["preferences"] = [item for item in self._state["preferences"] if not any(term in item.casefold() for term in terms)]
            if journal_cursor is not None:
                self._state["journal_cursor"] = int(journal_cursor)
            self._state["updated_at"] = _now().isoformat()
            self._save()


class PromptComposer:
    """Compose le prompt systeme dans un ordre stable et lisible."""

    def __init__(self, store: PromptContextStore | None = None, *, personality_override: str | None = None) -> None:
        self.store = store or PromptContextStore()
        self.personality_override = personality_override

    def compose(self, *, runtime_instructions: str = "") -> str:
        snapshot = self.store.snapshot()
        personality = self.personality_override or snapshot.personality
        sections = [
            ("CORE — IMMUTABLE", snapshot.core),
            ("PERSONALITY", personality),
            ("METHODOLOGY", snapshot.methodology),
            ("USER PROFILE", json.dumps(snapshot.user_profile, ensure_ascii=False, default=str)),
            ("PERSISTENT MEMORY", "\n".join(f"- {item}" for item in snapshot.memories) or "(none)"),
            ("USER PREFERENCES", "\n".join(f"- {item}" for item in snapshot.preferences) or "(none)"),
        ]
        if snapshot.additional.strip():
            sections.append(("ADDITIONAL INSTRUCTIONS", snapshot.additional))
        if runtime_instructions.strip():
            sections.append(("RUNTIME INSTRUCTIONS", runtime_instructions))
        return "\n\n".join(f"## {title}\n\n{content}" for title, content in sections)


@dataclass(frozen=True)
class JournalEntry:
    id: int
    event_id: str | None
    task_id: int | None
    messages: list[dict[str, Any]]
    created_at: str
    source: str | None = None
    channel: str | None = None
    conversation_id: str = "default"


class ConversationJournal:
    """Journal compact des conversations, source de l'extraction periodique."""

    def __init__(self, path: str | Path = "data/conversations.jsonl", *, max_message_chars: int = 4000) -> None:
        self.path = Path(path)
        self.max_message_chars = max_message_chars
        self._lock = threading.RLock()
        self._next_id = self._last_id() + 1

    def _last_id(self) -> int:
        if not self.path.exists():
            return 0
        last = 0
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                last = max(last, int(json.loads(line).get("id", 0)))
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
        return last

    def append(
        self,
        *,
        event_id: str | None,
        task_id: int | None,
        messages: Sequence[Mapping[str, Any]],
        source: str | None = None,
        channel: str | None = None,
        conversation_id: str = "default",
        timestamp: str | None = None,
    ) -> JournalEntry:
        compact: list[dict[str, Any]] = []
        message_timestamp = timestamp or _now().isoformat()
        for message in messages:
            item: dict[str, Any] = {
                "role": str(message.get("role", "")),
                "source": source or channel or "unknown",
                "at": str(message.get("at") or message_timestamp),
            }
            if channel:
                item["channel"] = channel
            if isinstance(message.get("content"), str):
                item["content"] = message["content"][: self.max_message_chars]
            elif message.get("content") is not None:
                item["content"] = _safe(message["content"], max_chars=self.max_message_chars)
            if message.get("tool_calls"):
                item["tool_calls"] = [
                    str(call.get("function", {}).get("name", ""))
                    for call in message["tool_calls"]
                    if isinstance(call, Mapping)
                ][:20]
            compact.append(item)
        with self._lock:
            entry = JournalEntry(
                self._next_id,
                event_id,
                task_id,
                compact,
                _now().isoformat(),
                source=source,
                channel=channel,
                conversation_id=str(conversation_id or "default"),
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry.__dict__, ensure_ascii=False, default=str) + "\n")
            self._next_id += 1
            return entry

    def after(self, cursor: int, *, limit: int = 20) -> list[JournalEntry]:
        if not self.path.exists():
            return []
        entries: list[JournalEntry] = []
        with self._lock:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if len(entries) >= limit:
                    break
                try:
                    raw = json.loads(line)
                    if int(raw["id"]) <= cursor:
                        continue
                    entries.append(
                        JournalEntry(
                            int(raw["id"]),
                            raw.get("event_id"),
                            raw.get("task_id"),
                            list(raw.get("messages", [])),
                            raw["created_at"],
                            raw.get("source"),
                            raw.get("channel"),
                            str(raw.get("conversation_id") or "default"),
                        )
                    )
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
        return entries

    def recent_messages(
        self,
        *,
        conversation_id: str = "default",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Retourne les derniers messages d'un historique multi-source."""
        if limit < 1:
            return []
        wanted = str(conversation_id or "default")
        messages: list[dict[str, Any]] = []
        with self._lock:
            if not self.path.exists():
                return messages
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    raw = json.loads(line)
                    if str(raw.get("conversation_id") or "default") != wanted:
                        continue
                    entry_source = raw.get("source")
                    entry_channel = raw.get("channel")
                    for message in raw.get("messages", []):
                        if not isinstance(message, Mapping):
                            continue
                        item = dict(message)
                        item.setdefault("source", entry_source or entry_channel or "unknown")
                        if entry_channel:
                            item.setdefault("channel", entry_channel)
                        item["journal_id"] = int(raw.get("id", 0))
                        item["task_id"] = raw.get("task_id")
                        item.setdefault("at", raw.get("created_at"))
                        messages.append(item)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
        return messages[-limit:]


class MemoryExtractor:
    """Utilise un petit modele pour extraire uniquement des faits durables."""

    def __init__(self, client: OpenRouterClient, store: PromptContextStore, *, model: str | None = None, max_input_chars: int = 30000) -> None:
        self.client = client
        self.store = store
        self.model = model
        self.max_input_chars = max_input_chars

    def extract(self, entries: Sequence[JournalEntry]) -> dict[str, Any]:
        payload = json.dumps([entry.__dict__ for entry in entries], ensure_ascii=False, default=str)
        payload = payload[: self.max_input_chars]
        response = self.client.complete(
            [
                {
                    "role": "system",
                    "content": "Extract durable user facts only. Never extract secrets, credentials, transient details, or guesses. Return JSON only with keys user_profile (object), preferences (array of strings), memories (array of strings), forget (array of strings).",
                },
                {"role": "user", "content": payload},
            ],
            model=self.model,
            temperature=0,
        )
        text = self.client.text_from_response(response).strip()
        fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S | re.I)
        if fenced:
            text = fenced.group(1)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("L'extracteur de memoire n'a pas renvoye de JSON valide.") from exc
        if not isinstance(data, Mapping):
            raise ValueError("L'extraction de memoire doit etre un objet JSON.")
        return dict(data)


class MemoryMaintenance:
    """Maintenance manuelle ou quotidienne du profil et de la memoire."""

    def __init__(self, journal: ConversationJournal, extractor: MemoryExtractor, *, batch_size: int = 20, run_at: day_time = day_time(23, 0), poll_interval: float = 30.0) -> None:
        if batch_size < 1 or poll_interval <= 0:
            raise ValueError("batch_size et poll_interval doivent etre positifs.")
        self.journal = journal
        self.extractor = extractor
        self.batch_size = batch_size
        self.run_at = run_at
        self.poll_interval = poll_interval
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> int:
        entries = self.journal.after(self.extractor.store.journal_cursor, limit=self.batch_size)
        if not entries:
            return 0
        extraction = self.extractor.extract(entries)
        self.extractor.store.apply_extraction(extraction, journal_cursor=entries[-1].id)
        return len(entries)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> MemoryMaintenance:
        if self.running:
            return self
        self._stop_requested.clear()
        self._thread = threading.Thread(target=self._run, name="orion-memory-maintenance", daemon=True)
        self._thread.start()
        return self

    def stop(self, *, wait: bool = True) -> None:
        self._stop_requested.set()
        thread = self._thread
        if thread is not None and wait:
            thread.join(timeout=max(1.0, self.poll_interval + 1.0))
        self._thread = None

    def _run(self) -> None:
        while not self._stop_requested.is_set():
            now = datetime.now().astimezone()
            target = now.replace(hour=self.run_at.hour, minute=self.run_at.minute, second=self.run_at.second, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            wait_seconds = max(0.0, (target - now).total_seconds())
            if self._stop_requested.wait(min(wait_seconds, self.poll_interval)):
                return
            if wait_seconds <= self.poll_interval:
                try:
                    self.run_once()
                except Exception:
                    # L'echec ne doit pas arreter le runtime ; le batch reste a traiter.
                    pass


__all__ = [
    "ConversationJournal",
    "DEFAULT_CORE",
    "DEFAULT_METHODOLOGY",
    "DEFAULT_PERSONALITY",
    "JournalEntry",
    "MemoryExtractor",
    "MemoryMaintenance",
    "PromptComposer",
    "PromptContextStore",
    "PromptSnapshot",
]
