"""Prompt systeme en couches et extraction de memoire hors contexte.

Le coeur est fourni a la construction et n'expose aucune operation d'ecriture.
Les informations apprises sont limitees au profil, aux preferences et aux
souvenirs durables. Elles sont stockees separement dans un fichier JSON.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import os
import re
import threading
import time
import uuid
from collections import deque
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, time as day_time, timedelta, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

from context_assembler import ContextAssembler, redact_value

if TYPE_CHECKING:
    from openrouter_client import OpenRouterClient


DEFAULT_CORE = """You are Orion, an event-driven AI agent.

CORE RULES — immutable at runtime:
- Follow the user's legitimate instructions and be honest about uncertainty.
- Protect privacy and secrets; never store API keys, passwords, or tokens in memory.
- Treat tools and external side effects as consequential: verify before acting.
- Durable state, tasks, plans, and memories are optional continuity aids; use them only when persistence is useful, and never let them override a newer explicit instruction.
- Do not expose private chain-of-thought. Give concise conclusions, useful evidence, and next actions.
- Write like a real conversation: by default answer very briefly and directly, usually in one or two short sentences. Do not pad, restate, recap, add headings, or enumerate unless it materially helps. Be longer and detailed only when the user asks for it or the subject genuinely requires important context, precision, or safety.
- If an objective is complete, stop. If waiting is appropriate, wait instead of polling.
"""

DEFAULT_PERSONALITY = "Tu es Orion : fiable, calme, pragmatique, clair et direct."
DEFAULT_METHODOLOGY = """Pour chaque demande : comprendre le contexte, charger l'etat utile,
decider s'il faut repondre, agir, poursuivre une tache ou attendre. N'ecris un
etat durable que si la demande cree une continuite utile (tache, attente,
preference, memoire ou objectif durable) ; une demande ephemere n'impose aucune
persistance. Un plan reste mutable et doit suivre les observations."""

_SECRET_KEY = re.compile(
    r"(?:password|passwd|secret|token|credential|authorization|cookie|"
    r"api[_ -]?key|private[_ -]?key|access[_ -]?key|bearer)",
    re.I,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe(value: Any, *, max_chars: int = 1200) -> Any:
    value = redact_value(value)
    if isinstance(value, str):
        return value.strip()[:max_chars]
    if isinstance(value, Mapping):
        return {
            str(key): _safe(item, max_chars=max_chars) for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_safe(item, max_chars=max_chars) for item in list(value)[:50]]
    return value


class _InterprocessFileLock:
    """Small crash-safe advisory lock that works on Windows and POSIX.

    The lock file is intentionally persistent.  Kernel advisory locks are
    released automatically if a process dies, unlike O_EXCL sentinel files
    which can strand writers after a crash.
    """

    def __init__(self, path: Path, *, timeout: float = 30.0) -> None:
        self.path = path
        self.timeout = float(timeout)
        self._handle: Any | None = None

    def __enter__(self) -> _InterprocessFileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._handle = handle
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise TimeoutError(f"timed out acquiring file lock: {self.path}")
                time.sleep(0.01)

    def __exit__(self, *_: Any) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


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
        self._state: dict[str, Any] = self._fresh_state()
        target = None if self.path == ":memory:" else Path(self.path)
        self._process_lock_path = (
            None if target is None else target.with_name(f".{target.name}.write.lock")
        )
        self._load()

    def _fresh_state(self) -> dict[str, Any]:
        return {
            **self._defaults,
            "user_profile": {},
            "preferences": [],
            "memories": [],
            "memory_metadata": {
                "user_profile": {},
                "preferences": {},
                "memories": {},
            },
            "journal_cursor": 0,
            "updated_at": None,
        }

    def _refresh_from_disk_locked(self) -> None:
        if self.path == ":memory:":
            return
        target = Path(self.path)
        if not target.exists():
            self._state = self._fresh_state()
            return
        raw = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            raise ValueError(
                "Le fichier de contexte du prompt doit contenir un objet JSON."
            )
        state = self._fresh_state()
        for key in ("personality", "methodology", "additional"):
            if isinstance(raw.get(key), str):
                state[key] = raw[key]
        if isinstance(raw.get("user_profile"), Mapping):
            state["user_profile"] = _safe(raw["user_profile"])
        for key, limit in (
            ("preferences", self.max_preferences),
            ("memories", self.max_memories),
        ):
            if isinstance(raw.get(key), list):
                state[key] = [
                    str(item)[:1200] for item in raw[key] if str(item).strip()
                ][-limit:]
        metadata = raw.get("memory_metadata")
        if isinstance(metadata, Mapping):
            for key in ("user_profile", "preferences", "memories"):
                value = metadata.get(key)
                if isinstance(value, Mapping):
                    state["memory_metadata"][key] = {
                        str(meta_key): dict(meta_value)
                        for meta_key, meta_value in value.items()
                        if isinstance(meta_value, Mapping)
                    }
            state["memory_metadata"]["user_profile"] = {
                key: value
                for key, value in state["memory_metadata"]["user_profile"].items()
                if key in state["user_profile"]
            }
            for key in ("preferences", "memories"):
                retained = {item.casefold() for item in state[key]}
                state["memory_metadata"][key] = {
                    meta_key: meta_value
                    for meta_key, meta_value in state["memory_metadata"][key].items()
                    if meta_key in retained
                }
        state["journal_cursor"] = int(raw.get("journal_cursor", 0) or 0)
        state["updated_at"] = raw.get("updated_at")
        self._state = state

    def _load(self) -> None:
        if self.path == ":memory:":
            return
        assert self._process_lock_path is not None
        with self._lock:
            with _InterprocessFileLock(self._process_lock_path):
                self._refresh_from_disk_locked()

    def snapshot(self) -> PromptSnapshot:
        with self._lock:
            if self._process_lock_path is not None:
                with _InterprocessFileLock(self._process_lock_path):
                    self._refresh_from_disk_locked()
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
            if self._process_lock_path is not None:
                with _InterprocessFileLock(self._process_lock_path):
                    self._refresh_from_disk_locked()
            return int(self._state["journal_cursor"])

    def _save(self) -> None:
        if self.path == ":memory:":
            return
        target = Path(self.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps(self._state, ensure_ascii=False, indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def apply_extraction(
        self, extraction: Mapping[str, Any], *, journal_cursor: int | None = None
    ) -> None:
        """Applique uniquement des donnees apprises autorisees."""
        process_lock = (
            _InterprocessFileLock(self._process_lock_path)
            if self._process_lock_path is not None
            else nullcontext()
        )
        with self._lock, process_lock:
            if self._process_lock_path is not None:
                self._refresh_from_disk_locked()
            metadata = self._state.setdefault(
                "memory_metadata",
                {"user_profile": {}, "preferences": {}, "memories": {}},
            )
            observed_at = str(extraction.get("_observed_at") or _now().isoformat())
            provenance = str(extraction.get("_provenance") or "memory_extractor")

            def meta_for(item: Mapping[str, Any] | None = None) -> dict[str, Any]:
                item = item or {}
                result = {
                    "observed_at": str(item.get("observed_at") or observed_at),
                    "provenance": str(item.get("provenance") or provenance),
                }
                semantic_key = item.get("key")
                if semantic_key is not None and str(semantic_key).strip():
                    result["key"] = str(semantic_key).strip()[:160]
                return result

            def forget_terms(value: Any) -> list[str]:
                if isinstance(value, str):
                    value = [value]
                if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
                    return []
                return [
                    str(item).casefold().strip()
                    for item in value
                    if str(item).casefold().strip()
                ]

            raw_forget = extraction.get("forget", [])
            category_forget: dict[str, list[str]] = {
                "user_profile": [],
                "preferences": [],
                "memories": [],
            }
            if isinstance(raw_forget, Mapping):
                for key in category_forget:
                    category_forget[key] = forget_terms(raw_forget.get(key, []))
            else:
                shared = forget_terms(raw_forget)
                for key in category_forget:
                    category_forget[key] = list(shared)

            profile_meta = metadata.setdefault("user_profile", {})
            for key in list(self._state["user_profile"]):
                value = self._state["user_profile"][key]
                searchable = f"{key} {json.dumps(value, ensure_ascii=False, default=str)}".casefold()
                if any(term in searchable for term in category_forget["user_profile"]):
                    self._state["user_profile"].pop(key, None)
                    profile_meta.pop(str(key), None)

            def remove_list_matches(bucket: str, terms: Sequence[str]) -> None:
                bucket_meta = metadata.setdefault(bucket, {})
                retained: list[str] = []
                for item in self._state[bucket]:
                    item_key = item.casefold()
                    semantic_key = str(bucket_meta.get(item_key, {}).get("key") or "").casefold()
                    if any(term in item_key or (semantic_key and term in semantic_key) for term in terms):
                        bucket_meta.pop(item_key, None)
                        continue
                    retained.append(item)
                self._state[bucket] = retained

            remove_list_matches("preferences", category_forget["preferences"])
            remove_list_matches("memories", category_forget["memories"])

            profile = extraction.get("user_profile")
            if isinstance(profile, Mapping):
                for key, value in profile.items():
                    if _SECRET_KEY.search(str(key)):
                        continue
                    profile_key = str(key)
                    item_meta: Mapping[str, Any] | None = None
                    actual_value = value
                    if isinstance(value, Mapping) and "value" in value:
                        item_meta = value
                        actual_value = value.get("value")
                    if actual_value in (None, ""):
                        self._state["user_profile"].pop(profile_key, None)
                        profile_meta.pop(profile_key, None)
                        continue
                    self._state["user_profile"][profile_key] = _safe(actual_value)
                    profile_meta[profile_key] = meta_for(item_meta)

            for key, limit in (
                ("preferences", self.max_preferences),
                ("memories", self.max_memories),
            ):
                values = extraction.get(key, [])
                if isinstance(values, str):
                    values = [values]
                if isinstance(values, list):
                    existing = list(self._state[key])
                    bucket_meta = metadata.setdefault(key, {})
                    for value in values:
                        item_meta: Mapping[str, Any] | None = value if isinstance(value, Mapping) else None
                        raw_value = (
                            value.get("value", value.get("content", value.get("fact", "")))
                            if isinstance(value, Mapping)
                            else value
                        )
                        cleaned = str(raw_value).strip()[:1200]
                        if not cleaned:
                            continue
                        semantic_key = (
                            str(value.get("key") or "").strip().casefold()
                            if isinstance(value, Mapping)
                            else ""
                        )
                        explicit_supersedes: list[str] = []
                        if isinstance(value, Mapping):
                            for relation in ("supersedes", "contradicts"):
                                explicit_supersedes.extend(forget_terms(value.get(relation, [])))

                        retained: list[str] = []
                        for old in existing:
                            old_key = old.casefold()
                            old_semantic_key = str(
                                bucket_meta.get(old_key, {}).get("key") or ""
                            ).casefold()
                            replace = bool(
                                semantic_key
                                and old_semantic_key
                                and semantic_key == old_semantic_key
                                and old_key != cleaned.casefold()
                            ) or any(term in old_key for term in explicit_supersedes)
                            if replace:
                                bucket_meta.pop(old_key, None)
                            else:
                                retained.append(old)
                        existing = retained
                        if cleaned and cleaned.casefold() not in {
                            item.casefold() for item in existing
                        }:
                            existing.append(cleaned)
                        bucket_meta[cleaned.casefold()] = meta_for(item_meta)
                    self._state[key] = existing[-limit:]
                    retained_keys = {item.casefold() for item in self._state[key]}
                    metadata[key] = {
                        meta_key: meta_value
                        for meta_key, meta_value in bucket_meta.items()
                        if meta_key in retained_keys
                    }
            if journal_cursor is not None:
                self._state["journal_cursor"] = max(
                    int(self._state.get("journal_cursor", 0) or 0), int(journal_cursor)
                )
            self._state["updated_at"] = _now().isoformat()
            self._save()


class PromptComposer:
    """Compose le prompt systeme dans un ordre stable et lisible."""

    def __init__(
        self,
        store: PromptContextStore | None = None,
        *,
        personality_override: str | None = None,
        context_mode: str = "contract",
        max_chars: int = 12000,
        max_tokens: int = 3000,
    ) -> None:
        if context_mode not in {"contract", "legacy"}:
            raise ValueError("context_mode must be contract or legacy")
        if max_chars < 1 or max_tokens < 1:
            raise ValueError("prompt policy limits must be positive")
        self.store = store or PromptContextStore()
        self.personality_override = personality_override
        self.context_mode = context_mode
        self.max_chars = int(max_chars)
        self.max_tokens = int(max_tokens)

    def compose(self, *, runtime_instructions: str = "") -> str:
        snapshot = self.store.snapshot()
        personality = self.personality_override or snapshot.personality
        if self.context_mode == "contract":
            sections = [
                ("CORE POLICY", snapshot.core),
                ("PERSONALITY", personality),
                ("METHODOLOGY", snapshot.methodology),
            ]
            if runtime_instructions.strip():
                sections.append(("RUNTIME POLICY", runtime_instructions))
            text = "\n\n".join(
                f"## {title}\n\n{content}" for title, content in sections
            )
            return ContextAssembler._clip_text(text, self.max_chars)
        sections = [
            ("CORE — IMMUTABLE", snapshot.core),
            ("PERSONALITY", personality),
            ("METHODOLOGY", snapshot.methodology),
            (
                "USER PROFILE",
                json.dumps(snapshot.user_profile, ensure_ascii=False, default=str),
            ),
            (
                "PERSISTENT MEMORY",
                "\n".join(f"- {item}" for item in snapshot.memories) or "(none)",
            ),
            (
                "USER PREFERENCES",
                "\n".join(f"- {item}" for item in snapshot.preferences) or "(none)",
            ),
        ]
        if snapshot.additional.strip():
            sections.append(("ADDITIONAL INSTRUCTIONS", snapshot.additional))
        if runtime_instructions.strip():
            sections.append(("RUNTIME INSTRUCTIONS", runtime_instructions))
        return "\n\n".join(f"## {title}\n\n{content}" for title, content in sections)

    def evidence(
        self,
        *,
        request: Any = None,
        event: Any = None,
        task: Any = None,
        loaded_state: Mapping[str, Any] | None = None,
        history: Sequence[Any] = (),
        notifications: Sequence[Any] = (),
        tool_observations: Sequence[Any] = (),
        reflection: Any = None,
        source: str = "runtime",
        max_chars: int = 48000,
    ) -> str:
        """Return one bounded ORION_EVIDENCE_V1 JSON bundle."""
        snapshot = self.store.snapshot()
        event_value = event.to_dict() if hasattr(event, "to_dict") else event
        task_value = task.to_dict() if hasattr(task, "to_dict") else task
        data = {
            "request": {} if request is None else request,
            "event": {} if event_value is None else event_value,
            "task": {} if task_value is None else task_value,
            "loaded_state": dict(loaded_state or {}),
            "profile": snapshot.user_profile,
            "preferences": snapshot.preferences,
            "memories": snapshot.memories,
            "history": list(history),
            "waiting_subagents": list(notifications),
            "tool_observations": list(tool_observations),
            "reflection": reflection,
        }
        assembler = ContextAssembler(
            total_max_chars=max_chars,
            total_max_tokens=max(100, max_chars // 4),
            output_reserve_tokens=1,
        )
        return (
            "BEGIN_ORION_EVIDENCE\n"
            + assembler.evidence_envelope(data, source=source, max_chars=max_chars)
            + "\nEND_ORION_EVIDENCE"
        )


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

    def __init__(
        self,
        path: str | Path = "data/conversations.jsonl",
        *,
        max_message_chars: int = 4000,
        recent_cache_messages: int = 2048,
    ) -> None:
        self.path = Path(path)
        self.max_message_chars = max_message_chars
        self.recent_cache_messages = max(1, int(recent_cache_messages))
        self._lock = threading.RLock()
        self._event_index: dict[tuple[str, str], JournalEntry] = {}
        self._recent_cache: dict[str, deque[dict[str, Any]]] = {}
        self._indexed_size = 0
        self._next_id = 1
        self._process_lock_path = self.path.with_name(f".{self.path.name}.write.lock")
        with _InterprocessFileLock(self._process_lock_path):
            self._rebuild_indexes()

    def _rebuild_indexes(self) -> None:
        """Scan the legacy JSONL once, then serve hot recent/dedupe lookups in O(1)."""
        self._event_index.clear()
        self._recent_cache.clear()
        max_id = 0
        if not self.path.exists():
            self._indexed_size = 0
            self._next_id = 1
            return
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        raw = json.loads(line)
                        entry = JournalEntry(
                            int(raw.get("id", 0) or 0),
                            raw.get("event_id"),
                            raw.get("task_id"),
                            list(raw.get("messages", [])),
                            raw.get("created_at") or _now().isoformat(),
                            raw.get("source"),
                            raw.get("channel"),
                            str(raw.get("conversation_id") or "default"),
                        )
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    max_id = max(max_id, entry.id)
                    if entry.event_id is not None and str(entry.event_id).strip():
                        self._event_index[
                            (entry.conversation_id, str(entry.event_id))
                        ] = entry
                    self._cache_entry_messages(entry)
            self._indexed_size = self.path.stat().st_size
        except OSError:
            self._indexed_size = 0
        self._next_id = max_id + 1

    def _last_id(self) -> int:
        """Backward-compatible last-id helper backed by the hot index state."""
        with self._lock:
            with _InterprocessFileLock(self._process_lock_path):
                self._refresh_indexes_if_changed()
                return max(0, self._next_id - 1)

    def _refresh_indexes_if_changed(self) -> None:
        """Keep compatibility with external appenders without rescanning on normal use."""
        try:
            size = self.path.stat().st_size if self.path.exists() else 0
        except OSError:
            size = self._indexed_size
        if size != self._indexed_size:
            self._rebuild_indexes()

    def _cache_entry_messages(self, entry: JournalEntry) -> None:
        if str(entry.source or "").startswith("subagent:"):
            return
        cache = self._recent_cache.setdefault(
            entry.conversation_id, deque(maxlen=self.recent_cache_messages)
        )
        for message in entry.messages:
            if not isinstance(message, Mapping):
                continue
            item = dict(message)
            item.setdefault("sender", self._sender_label(item.get("role")))
            item.setdefault(
                "source",
                entry.source
                if entry.source is not None
                else (entry.channel or "unknown"),
            )
            if entry.channel:
                item.setdefault("channel", entry.channel)
            item["journal_id"] = entry.id
            item["task_id"] = entry.task_id
            item.setdefault("at", entry.created_at)
            cache.append(item)

    @classmethod
    def _compact_messages(
        cls,
        messages: Sequence[Mapping[str, Any]],
        *,
        max_message_chars: int,
        source: str | None,
        channel: str | None,
        timestamp: str,
    ) -> list[dict[str, Any]]:
        """Persist conversational content while keeping tool protocol links usable."""
        compact: list[dict[str, Any]] = []
        for message in messages:
            sender = " ".join(
                str(message.get("sender") or cls._sender_label(message.get("role"))).split()
            )[:80]
            item: dict[str, Any] = {
                "role": str(message.get("role", "")),
                "sender": sender or cls._sender_label(message.get("role")),
                "source": source or channel or "unknown",
                "at": str(message.get("at") or timestamp),
            }
            if channel:
                item["channel"] = channel
            if isinstance(message.get("content"), str):
                item["content"] = message["content"][:max_message_chars]
            elif message.get("content") is not None:
                item["content"] = _safe(message["content"], max_chars=max_message_chars)
            if message.get("tool_calls"):
                calls: list[dict[str, Any]] = []
                for call in message["tool_calls"]:
                    if not isinstance(call, Mapping):
                        continue
                    function = call.get("function")
                    function = function if isinstance(function, Mapping) else {}
                    normalized_call: dict[str, Any] = {
                        "id": str(call.get("id") or ""),
                        "type": str(call.get("type") or "function"),
                        "function": {
                            "name": str(function.get("name") or call.get("name") or "")[:160],
                            "arguments": str(function.get("arguments") or "")[:1200],
                        },
                    }
                    calls.append(normalized_call)
                    if len(calls) >= 20:
                        break
                if calls:
                    item["tool_calls"] = calls
            if message.get("tool_call_id") is not None:
                item["tool_call_id"] = str(message.get("tool_call_id"))[:200]
            if message.get("name") is not None:
                item["name"] = str(message.get("name"))[:160]
            compact.append(item)
        return compact

    @staticmethod
    def _messages_are_incomplete(messages: Sequence[Mapping[str, Any]]) -> bool:
        interrupted = False
        has_final_assistant = False
        for message in messages:
            role = str(message.get("role") or "")
            content = str(message.get("content") or "")
            if "RUN a été interrompu avant sa réponse finale" in content:
                interrupted = True
            if role == "assistant" and content.strip():
                has_final_assistant = True
        return interrupted or not has_final_assistant

    @classmethod
    def _should_supersede(
        cls,
        existing: Sequence[Mapping[str, Any]],
        incoming: Sequence[Mapping[str, Any]],
    ) -> bool:
        return cls._messages_are_incomplete(existing) and not cls._messages_are_incomplete(incoming)

    def _replace_jsonl_entry(self, old: JournalEntry, new: JournalEntry) -> None:
        """Atomically replace one incomplete attempt with a later successful attempt."""
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with self.path.open("r", encoding="utf-8") as source_handle, temporary.open(
                "w", encoding="utf-8"
            ) as target_handle:
                for line in source_handle:
                    keep = True
                    try:
                        raw = json.loads(line)
                        keep = not (
                            int(raw.get("id", 0) or 0) == old.id
                            and str(raw.get("conversation_id") or "default")
                            == old.conversation_id
                        )
                    except (TypeError, ValueError, json.JSONDecodeError):
                        pass
                    if keep:
                        target_handle.write(line if line.endswith("\n") else line + "\n")
                target_handle.write(
                    json.dumps(new.__dict__, ensure_ascii=False, default=str) + "\n"
                )
                target_handle.flush()
                os.fsync(target_handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

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
        message_timestamp = timestamp or _now().isoformat()
        compact = self._compact_messages(
            messages,
            max_message_chars=self.max_message_chars,
            source=source,
            channel=channel,
            timestamp=message_timestamp,
        )
        with self._lock:
            # ID allocation, event de-duplication and the append itself are one
            # interprocess critical section.  Every writer refreshes its local
            # index after taking the lock, so two Orion instances cannot assign
            # the same cursor id from stale in-memory state.
            with _InterprocessFileLock(self._process_lock_path):
                self._refresh_indexes_if_changed()
                normalized_conversation = str(conversation_id or "default")
                # Les retries d'un même événement sont fréquents (redémarrage,
                # livraison au moins une fois). Retourner l'entrée existante rend
                # l'opération idempotente sans réécrire les anciens JSONL.
                if event_id is not None and str(event_id).strip():
                    existing = self._event_index.get(
                        (normalized_conversation, str(event_id))
                    )
                    if existing is not None:
                        if self._should_supersede(existing.messages, compact):
                            entry = JournalEntry(
                                self._next_id,
                                event_id,
                                task_id,
                                compact,
                                _now().isoformat(),
                                source=source,
                                channel=channel,
                                conversation_id=normalized_conversation,
                            )
                            self._replace_jsonl_entry(existing, entry)
                            self._rebuild_indexes()
                            return entry
                        return existing
                entry = JournalEntry(
                    self._next_id,
                    event_id,
                    task_id,
                    compact,
                    _now().isoformat(),
                    source=source,
                    channel=channel,
                    conversation_id=normalized_conversation,
                )
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(entry.__dict__, ensure_ascii=False, default=str)
                        + "\n"
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
                self._next_id += 1
                if entry.event_id is not None and str(entry.event_id).strip():
                    self._event_index[(normalized_conversation, str(entry.event_id))] = (
                        entry
                    )
                self._cache_entry_messages(entry)
                try:
                    self._indexed_size = self.path.stat().st_size
                except OSError:
                    pass
                return entry

    def _find_event(self, event_id: str, conversation_id: str) -> JournalEntry | None:
        self._refresh_indexes_if_changed()
        return self._event_index.get((conversation_id, event_id))

    @staticmethod
    def _sender_label(role: Any) -> str:
        """Expose un expéditeur explicite dans le journal envoyé au modèle."""
        labels = {
            "user": "user",
            "assistant": "orion",
            "tool": "tool",
            "system": "system",
        }
        return labels.get(str(role or "").lower(), str(role or "unknown"))

    def after(self, cursor: int, *, limit: int = 20) -> list[JournalEntry]:
        if not self.path.exists():
            return []
        entries: list[JournalEntry] = []
        with self._lock:
            with _InterprocessFileLock(self._process_lock_path):
                with self.path.open("r", encoding="utf-8") as handle:
                    for line in handle:
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
        messages: deque[dict[str, Any]] = deque(maxlen=limit)
        with self._lock:
            with _InterprocessFileLock(self._process_lock_path):
                self._refresh_indexes_if_changed()
                cached = self._recent_cache.get(wanted)
                if cached is not None and limit <= self.recent_cache_messages:
                    return [dict(item) for item in list(cached)[-limit:]]
                if not self.path.exists():
                    return []
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    try:
                        raw = json.loads(line)
                        if str(raw.get("conversation_id") or "default") != wanted:
                            continue
                        entry_source = raw.get("source")
                        entry_channel = raw.get("channel")
                        if str(entry_source or "").startswith("subagent:"):
                            # Les échanges internes restent consultables via les
                            # jobs, pas comme une conversation utilisateur.
                            continue
                        for message in raw.get("messages", []):
                            if not isinstance(message, Mapping):
                                continue
                            item = dict(message)
                            item.setdefault(
                                "sender",
                                self._sender_label(item.get("role")),
                            )
                            # Préserver la provenance portée par chaque message
                            # lorsqu'elle existe, et retomber sur celle de l'entrée.
                            item.setdefault(
                                "source",
                                entry_source
                                if entry_source is not None
                                else (entry_channel or "unknown"),
                            )
                            if entry_channel:
                                item.setdefault("channel", entry_channel)
                            item["journal_id"] = int(raw.get("id", 0))
                            item["task_id"] = raw.get("task_id")
                            item.setdefault("at", raw.get("created_at"))
                            messages.append(item)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
        return list(messages)


class SQLiteConversationJournal(ConversationJournal):
    """Backend SQLite optionnel, compatible avec le journal JSONL historique.

    Le schéma conserve le même objet ``JournalEntry`` et les mêmes méthodes
    ``append``/``after``/``recent_messages``.  ``migrate_jsonl`` permet une
    migration idempotente (les identifiants et les doublons sont préservés).
    """

    def __init__(
        self,
        path: str | Path = "data/conversations.sqlite3",
        *,
        max_message_chars: int = 4000,
    ) -> None:
        self.path = Path(path)
        self.max_message_chars = max_message_chars
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("""CREATE TABLE IF NOT EXISTS journal (
            id INTEGER PRIMARY KEY, event_id TEXT, task_id INTEGER,
            messages TEXT NOT NULL, created_at TEXT NOT NULL, source TEXT,
            channel TEXT, conversation_id TEXT NOT NULL DEFAULT 'default',
            UNIQUE(event_id, conversation_id)
        )""")
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_journal_conversation_id_id "
            "ON journal(conversation_id, id DESC)"
        )
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @staticmethod
    def _entry(row: sqlite3.Row) -> JournalEntry:
        return JournalEntry(
            int(row["id"]),
            row["event_id"],
            row["task_id"],
            json.loads(row["messages"]),
            row["created_at"],
            row["source"],
            row["channel"],
            row["conversation_id"] or "default",
        )

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
        normalized = str(conversation_id or "default")
        items = self._compact_messages(
            messages,
            max_message_chars=self.max_message_chars,
            source=source,
            channel=channel,
            timestamp=timestamp or _now().isoformat(),
        )
        with self._lock:
            created_at = _now().isoformat()
            # The connection context commits even when returning early from the
            # duplicate branch, and rolls back on any exception.  A duplicate
            # retry must never strand a write transaction that blocks another
            # journal instance.
            with self._db:
                cur = self._db.execute(
                    "INSERT OR IGNORE INTO journal(event_id,task_id,messages,created_at,source,channel,conversation_id) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        event_id,
                        task_id,
                        json.dumps(items, ensure_ascii=False),
                        created_at,
                        source,
                        channel,
                        normalized,
                    ),
                )
                if event_id and cur.rowcount == 0:
                    row = self._db.execute(
                        "SELECT * FROM journal WHERE event_id=? AND conversation_id=?",
                        (str(event_id), normalized),
                    ).fetchone()
                    if row:
                        existing = self._entry(row)
                        if not self._should_supersede(existing.messages, items):
                            return existing
                        # Give a recovered successful attempt a fresh cursor id.
                        # Consumers that already advanced past the failed attempt
                        # will therefore still observe the successful state.
                        highest = int(
                            self._db.execute(
                                "SELECT COALESCE(MAX(id), 0) FROM journal"
                            ).fetchone()[0]
                        )
                        replacement_id = max(highest, existing.id) + 1
                        self._db.execute("DELETE FROM journal WHERE id=?", (existing.id,))
                        self._db.execute(
                            "INSERT INTO journal(id,event_id,task_id,messages,created_at,source,channel,conversation_id) "
                            "VALUES(?,?,?,?,?,?,?,?)",
                            (
                                replacement_id,
                                event_id,
                                task_id,
                                json.dumps(items, ensure_ascii=False),
                                created_at,
                                source,
                                channel,
                                normalized,
                            ),
                        )
                        return JournalEntry(
                            replacement_id,
                            event_id,
                            task_id,
                            items,
                            created_at,
                            source,
                            channel,
                            normalized,
                        )
                return JournalEntry(
                    cur.lastrowid,
                    event_id,
                    task_id,
                    items,
                    created_at,
                    source,
                    channel,
                    normalized,
                )

    def after(self, cursor: int, *, limit: int = 20) -> list[JournalEntry]:
        if limit < 1:
            return []
        with self._lock:
            return [
                self._entry(r)
                for r in self._db.execute(
                    "SELECT * FROM journal WHERE id>? ORDER BY id LIMIT ?",
                    (int(cursor), int(limit)),
                ).fetchall()
            ]

    def recent_messages(
        self, *, conversation_id: str = "default", limit: int = 20
    ) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        wanted = str(conversation_id or "default")
        # Fetch newest rows first and stop as soon as enough messages are
        # collected. This avoids materializing an entire long-lived thread.
        newest_first: list[dict[str, Any]] = []
        before_id: int | None = None
        page_size = min(256, max(16, int(limit)))
        with self._lock:
            while len(newest_first) < limit:
                if before_id is None:
                    rows = self._db.execute(
                        "SELECT * FROM journal WHERE conversation_id=? "
                        "AND (source IS NULL OR substr(source,1,9) != 'subagent:') "
                        "ORDER BY id DESC LIMIT ?",
                        (wanted, page_size),
                    ).fetchall()
                else:
                    rows = self._db.execute(
                        "SELECT * FROM journal WHERE conversation_id=? AND id<? "
                        "AND (source IS NULL OR substr(source,1,9) != 'subagent:') "
                        "ORDER BY id DESC LIMIT ?",
                        (wanted, before_id, page_size),
                    ).fetchall()
                if not rows:
                    break
                before_id = int(rows[-1]["id"])
                for row in rows:
                    parsed = json.loads(row["messages"])
                    for message in reversed(parsed):
                        if not isinstance(message, Mapping):
                            continue
                        item = dict(message)
                        item.setdefault("sender", self._sender_label(item.get("role")))
                        item.setdefault("journal_id", int(row["id"]))
                        item.setdefault("task_id", row["task_id"])
                        item.setdefault(
                            "source", row["source"] or row["channel"] or "unknown"
                        )
                        item.setdefault("at", row["created_at"])
                        if row["channel"]:
                            item.setdefault("channel", row["channel"])
                        newest_first.append(item)
                        if len(newest_first) >= limit:
                            break
                    if len(newest_first) >= limit:
                        break
                if len(rows) < page_size:
                    break
        newest_first.reverse()
        return newest_first

    @classmethod
    def migrate_jsonl(
        cls,
        jsonl_path: str | Path,
        sqlite_path: str | Path,
        *,
        max_message_chars: int = 4000,
    ) -> int:
        target = cls(sqlite_path, max_message_chars=max_message_chars)
        count = 0
        source = Path(jsonl_path)
        if not source.exists():
            return 0
        with target._lock:
            for line in source.read_text(encoding="utf-8").splitlines():
                try:
                    raw = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(raw, Mapping) or not isinstance(
                    raw.get("messages"), list
                ):
                    continue
                conv = str(raw.get("conversation_id") or "default")
                event = raw.get("event_id")
                exists = target._db.execute(
                    "SELECT 1 FROM journal WHERE id=? OR (event_id IS NOT NULL AND event_id=? AND conversation_id=?)",
                    (int(raw.get("id", 0) or 0), event, conv),
                ).fetchone()
                if exists:
                    continue
                target._db.execute(
                    "INSERT OR IGNORE INTO journal(id,event_id,task_id,messages,created_at,source,channel,conversation_id) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        raw.get("id"),
                        event,
                        raw.get("task_id"),
                        json.dumps(raw["messages"], ensure_ascii=False),
                        raw.get("created_at") or _now().isoformat(),
                        raw.get("source"),
                        raw.get("channel"),
                        conv,
                    ),
                )
                count += 1
            target._db.commit()
        return count


class MemoryExtractor:
    """Utilise un petit modele pour extraire uniquement des faits durables."""

    def __init__(
        self,
        client: OpenRouterClient,
        store: PromptContextStore,
        *,
        model: str | None = None,
        max_input_chars: int = 30000,
    ) -> None:
        self.client = client
        self.store = store
        self.model = model
        self.max_input_chars = max_input_chars

    def _prepare_batch(
        self, entries: Sequence[JournalEntry]
    ) -> tuple[str, int, Sequence[JournalEntry]]:
        """Serialize the largest *prefix* that fits; never silently skip old entries."""
        selected: list[JournalEntry] = []
        values: list[Any] = []
        for entry in entries:
            raw = entry.__dict__
            candidate = [*values, raw]
            encoded = json.dumps(candidate, ensure_ascii=False, default=str)
            if len(encoded) <= self.max_input_chars:
                selected.append(entry)
                values.append(raw)
                continue
            if selected:
                break

            # One journal entry can itself exceed the extractor budget. Process
            # that entry atomically with a bounded representation so the cursor
            # advances by exactly one entry rather than jumping over a prefix.
            allowance = max(1, self.max_input_chars - 2)
            compacted = ContextAssembler.compact_value(raw, max_chars=allowance)
            encoded = json.dumps([compacted], ensure_ascii=False, default=str)
            while len(encoded) > self.max_input_chars and allowance > 1:
                allowance = max(1, allowance - max(1, allowance // 8))
                compacted = ContextAssembler.compact_value(raw, max_chars=allowance)
                encoded = json.dumps([compacted], ensure_ascii=False, default=str)
            if len(encoded) > self.max_input_chars:
                raise ValueError("max_input_chars is too small for one journal entry")
            selected.append(entry)
            values.append(compacted)
            break

        return (
            json.dumps(values, ensure_ascii=False, default=str),
            len(selected),
            selected,
        )

    def extract_batch(
        self, entries: Sequence[JournalEntry]
    ) -> tuple[dict[str, Any], int]:
        payload, processed_count, selected = self._prepare_batch(entries)
        if processed_count < 1:
            return {}, 0
        response = self.client.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "Extract durable user facts only. Never extract secrets, credentials, "
                        "transient details, or guesses. "
                        "Return JSON only with keys user_profile, preferences, memories, forget. "
                        "preferences/memories may contain strings or objects {value,key,supersedes,contradicts}; "
                        "use key when a newer fact deterministically replaces an older fact, and "
                        "list the exact older text in supersedes when replacing a legacy string fact. "
                        "forget may be an array applying globally or an object keyed by "
                        "user_profile/preferences/memories."
                    ),
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
            raise ValueError(
                "L'extracteur de memoire n'a pas renvoye de JSON valide."
            ) from exc
        if not isinstance(data, Mapping):
            raise ValueError("L'extraction de memoire doit etre un objet JSON.")
        result = dict(data)
        first = selected[0]
        last = selected[-1]
        result.setdefault(
            "_provenance",
            f"journal:{first.id}-{last.id}",
        )
        result.setdefault("_observed_at", last.created_at)
        return result, processed_count

    def extract(self, entries: Sequence[JournalEntry]) -> dict[str, Any]:
        """Backward-compatible single extraction call."""
        result, _ = self.extract_batch(entries)
        return result


class MemoryMaintenance:
    """Maintenance manuelle ou quotidienne du profil et de la memoire."""

    def __init__(
        self,
        journal: ConversationJournal,
        extractor: MemoryExtractor,
        *,
        batch_size: int = 20,
        min_entries: int | None = None,
        run_at: day_time = day_time(23, 0),
        poll_interval: float = 30.0,
        max_batches_per_run: int = 8,
        tail_max_age: float = 3600.0,
    ) -> None:
        selected_min_entries = batch_size if min_entries is None else int(min_entries)
        if (
            batch_size < 1
            or selected_min_entries < 1
            or selected_min_entries > batch_size
            or poll_interval <= 0
            or max_batches_per_run < 1
            or tail_max_age < 0
        ):
            raise ValueError(
                "memory maintenance limits must be positive and min_entries <= batch_size."
            )
        self.journal = journal
        self.extractor = extractor
        self.batch_size = batch_size
        self.min_entries = selected_min_entries
        self.run_at = run_at
        self.poll_interval = poll_interval
        self.max_batches_per_run = int(max_batches_per_run)
        self.tail_max_age = float(tail_max_age)
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._run_lock = threading.Lock()
        self._process_lock_path = self._memory_lock_path()

    def _memory_lock_path(self) -> Path | None:
        state_path = str(self.extractor.store.path)
        if state_path == ":memory:":
            return None
        path = Path(state_path)
        return path.with_name(f".{path.name}.memory.lock")

    def _acquire_process_lock(self) -> int | None:
        """Reserve une extraction pour une seule instance Orion.

        Le fichier est créé de manière atomique afin que plusieurs processus
        partageant le même journal ne lancent pas le même appel LLM. Un
        verrou abandonné est récupéré après une durée généreuse couvrant un
        appel réseau lent.
        """
        path = self._process_lock_path
        if path is None:
            return -1
        path.parent.mkdir(parents=True, exist_ok=True)
        stale_after = max(300.0, self.poll_interval * 10.0)
        try:
            descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > stale_after:
                    path.unlink()
                    return self._acquire_process_lock()
            except FileNotFoundError:
                return self._acquire_process_lock()
            return None
        os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
        return descriptor

    def _release_process_lock(self, descriptor: int | None) -> None:
        if descriptor in {-1, None}:
            return
        try:
            os.close(descriptor)
        finally:
            if self._process_lock_path is not None:
                try:
                    self._process_lock_path.unlink()
                except FileNotFoundError:
                    pass

    def _tail_is_due(self, entries: Sequence[JournalEntry]) -> bool:
        if not entries:
            return False
        if self.tail_max_age <= 0:
            return True
        try:
            created = datetime.fromisoformat(str(entries[0].created_at).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age = (_now() - created.astimezone(timezone.utc)).total_seconds()
            return age >= self.tail_max_age
        except (TypeError, ValueError):
            # Legacy rows without a parseable timestamp must not be stranded
            # forever merely because they cannot satisfy an age comparison.
            return True

    def run_once(self) -> int:
        if not self._run_lock.acquire(blocking=False):
            return 0
        descriptor: int | None = None
        try:
            descriptor = self._acquire_process_lock()
            if descriptor is None:
                return 0
            processed_total = 0
            for _ in range(self.max_batches_per_run):
                entries = self.journal.after(
                    self.extractor.store.journal_cursor, limit=self.batch_size
                )
                if not entries:
                    break
                if len(entries) < self.min_entries and not self._tail_is_due(entries):
                    break

                extract_batch = getattr(self.extractor, "extract_batch", None)
                if callable(extract_batch):
                    extraction, processed_count = extract_batch(entries)
                else:
                    extraction = self.extractor.extract(entries)
                    processed_count = len(entries)
                processed_count = int(processed_count)
                if processed_count < 1 or processed_count > len(entries):
                    raise ValueError("memory extractor returned an invalid processed count")
                last_processed = entries[processed_count - 1]
                self.extractor.store.apply_extraction(
                    extraction, journal_cursor=last_processed.id
                )
                processed_total += processed_count
            return processed_total
        finally:
            self._release_process_lock(descriptor)
            self._run_lock.release()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> MemoryMaintenance:
        if self.running:
            return self
        self._stop_requested.clear()
        self._thread = threading.Thread(
            target=self._run, name="orion-memory-maintenance", daemon=True
        )
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
            target = now.replace(
                hour=self.run_at.hour,
                minute=self.run_at.minute,
                second=self.run_at.second,
                microsecond=0,
            )
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
    "SQLiteConversationJournal",
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
