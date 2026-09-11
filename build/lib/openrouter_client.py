"""Client Python robuste pour l'API OpenRouter.

Le module expose :class:`OpenRouterClient`, utilisable directement pour des
conversations ou comme moteur d'un agent avec appel d'outils.

Dépendance : ``httpx``.
La clé API est lue depuis ``OPENROUTER_API_KEY`` si elle n'est pas fournie au
constructeur.
"""

from __future__ import annotations

import asyncio
import copy
import contextvars
import inspect
import json
import os
import time
import uuid
from collections.abc import AsyncGenerator, Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import threading
from typing import Any
from budgets import BudgetTracker, CircuitBreaker, RateLimiter, jittered_backoff

try:
    import httpx
except ImportError:  # pragma: no cover - message traité à l'instanciation
    httpx = None  # type: ignore[assignment]

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dépendance déclarée dans requirements.txt
    load_dotenv = None  # type: ignore[assignment]

if load_dotenv is not None:
    load_dotenv()


DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "~openai/gpt-latest"
RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

Message = dict[str, Any]
ToolHandler = Callable[..., Any]


_usage_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "orion_llm_usage_context", default={}
)


@contextmanager
def usage_context(
    *,
    request_id: str | None = None,
    correlation_id: str | None = None,
    stage: str = "other",
    parent_call_id: str | None = None,
):
    """Attach Orion correlation metadata to calls made in this context."""
    values = dict(_usage_context.get())
    for key, value in {
        "request_id": request_id,
        "correlation_id": correlation_id,
        "stage": stage,
        "parent_call_id": parent_call_id,
    }.items():
        if value is not None:
            values[key] = value
    token = _usage_context.set(values)
    try:
        yield
    finally:
        _usage_context.reset(token)


@dataclass(slots=True)
class LLMUsageRecord:
    """One logical provider call and its billing/usage metadata."""

    call_id: str
    request_id: str | None = None
    correlation_id: str | None = None
    parent_call_id: str | None = None
    stage: str = "other"
    model: str | None = None
    provider_id: str | None = None
    provider_request_id: str | None = None
    attempt: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    latency_ms: float | None = None
    status: str = "inflight"
    streamed: bool = False
    usage_complete: bool = False
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: Decimal | None = None
    cost_source: str = "unavailable"
    prompt_details: dict[str, Any] | None = None
    completion_details: dict[str, Any] | None = None
    cost_details: dict[str, Any] | None = None
    error_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            key: value
            for key, value in {
                "call_id": self.call_id,
                "request_id": self.request_id,
                "correlation_id": self.correlation_id,
                "parent_call_id": self.parent_call_id,
                "stage": self.stage,
                "model": self.model,
                "provider_id": self.provider_id,
                "provider_request_id": self.provider_request_id,
                "attempt": self.attempt,
                "started_at": self.started_at.isoformat(),
                "finished_at": self.finished_at.isoformat()
                if self.finished_at
                else None,
                "latency_ms": self.latency_ms,
                "status": self.status,
                "streamed": self.streamed,
                "usage_complete": self.usage_complete,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
                "cost_usd": str(self.cost_usd) if self.cost_usd is not None else None,
                "cost_source": self.cost_source,
                "prompt_details": dict(self.prompt_details)
                if self.prompt_details
                else None,
                "completion_details": dict(self.completion_details)
                if self.completion_details
                else None,
                "cost_details": dict(self.cost_details) if self.cost_details else None,
                "error_type": self.error_type,
            }.items()
        }
        return result


class UsageLedger:
    """Thread-safe, idempotent session aggregate for LLM usage records."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[str, LLMUsageRecord] = {}
        self._subscribers: list[Callable[[dict[str, Any]], Any]] = []
        self._last_update_at: datetime | None = None

    def subscribe(
        self, callback: Callable[[dict[str, Any]], Any]
    ) -> Callable[[], None]:
        with self._lock:
            if callback not in self._subscribers:
                self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def _snapshot_locked(self) -> dict[str, Any]:
        records = list(self._records.values())
        completed = [item for item in records if item.status == "succeeded"]

        def totals(items: list[LLMUsageRecord]) -> tuple[int, int, int]:
            return (
                sum(item.prompt_tokens or 0 for item in items),
                sum(item.completion_tokens or 0 for item in items),
                sum(item.total_tokens or 0 for item in items),
            )

        prompt, completion, total = totals(completed)
        known = sum(
            (
                item.cost_usd
                for item in completed
                if item.cost_usd is not None and item.cost_source == "openrouter"
            ),
            Decimal("0"),
        )
        estimated = sum(
            (
                item.cost_usd
                for item in completed
                if item.cost_usd is not None and item.cost_source == "catalog_estimate"
            ),
            Decimal("0"),
        )
        by_model: dict[str, dict[str, Any]] = {}
        by_stage: dict[str, dict[str, Any]] = {}
        for item in completed:
            model = item.model or "unknown"
            for target, key in ((by_model, model), (by_stage, item.stage or "other")):
                row = target.setdefault(
                    key,
                    {
                        "calls": 0,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                        "known_cost_usd": Decimal("0"),
                    },
                )
                row["calls"] += 1
                row["prompt_tokens"] += item.prompt_tokens or 0
                row["completion_tokens"] += item.completion_tokens or 0
                row["total_tokens"] += item.total_tokens or 0
                if item.cost_usd is not None and item.cost_source == "openrouter":
                    row["known_cost_usd"] += item.cost_usd
        return {
            "started_calls": len(records),
            "inflight_calls": sum(item.status == "inflight" for item in records),
            "completed_calls": len(completed),
            "failed_calls": sum(
                item.status in {"failed", "canceled"} for item in records
            ),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
            "known_cost_usd": known,
            "estimated_cost_usd": estimated,
            "usage_missing_calls": sum(
                not item.usage_complete or item.cost_usd is None for item in completed
            ),
            "by_model": by_model,
            "by_stage": by_stage,
            "last_update_at": self._last_update_at.isoformat()
            if self._last_update_at
            else None,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    @property
    def records(self) -> tuple[LLMUsageRecord, ...]:
        with self._lock:
            return tuple(replace(item) for item in self._records.values())

    def ingest(self, record: LLMUsageRecord, *, event: str | None = None) -> None:
        with self._lock:
            stored = replace(
                record,
                prompt_details=dict(record.prompt_details)
                if record.prompt_details
                else None,
                completion_details=dict(record.completion_details)
                if record.completion_details
                else None,
                cost_details=dict(record.cost_details) if record.cost_details else None,
            )
            existing = self._records.get(stored.call_id)
            if (
                existing is not None
                and existing.status != "inflight"
                and stored.status == "inflight"
            ):
                return
            self._records[stored.call_id] = stored
            self._last_update_at = datetime.now(timezone.utc)
            event_name = event or (
                "usage.started" if stored.status == "inflight" else "usage.updated"
            )
            payload = {
                "type": event_name,
                "call_id": stored.call_id,
                "request_id": stored.request_id,
                "record": stored.to_dict(),
                "snapshot": self._snapshot_locked(),
            }
            subscribers = tuple(self._subscribers)
        for callback in subscribers:
            try:
                callback(payload)
            except Exception:
                continue

    record = ingest


class OpenRouterError(Exception):
    """Erreur de base du client OpenRouter."""


class OpenRouterConfigurationError(OpenRouterError):
    """Configuration locale invalide ou dépendance manquante."""


class OpenRouterTransportError(OpenRouterError):
    """Erreur réseau avant réception d'une réponse HTTP valide."""


class OpenRouterTimeoutError(OpenRouterTransportError):
    """La requête a dépassé le délai configuré."""


class OpenRouterAPIError(OpenRouterError):
    """Réponse HTTP d'erreur renvoyée par OpenRouter."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error: Any = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error = error
        self.request_id = request_id


class ToolExecutionError(OpenRouterError):
    """Une fonction locale appelée par le modèle n'a pas pu être exécutée."""


class AgentLoopLimitError(OpenRouterError):
    """Le modèle a dépassé le nombre maximal de tours d'outils."""


@dataclass(slots=True)
class RegisteredTool:
    """Définition OpenAI/OpenRouter associée à une fonction Python locale."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    strict: bool | None = None
    side_effect: bool = False
    dedupe_window: float = 86400.0

    @property
    def definition(self) -> dict[str, Any]:
        function: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }
        if self.strict is not None:
            function["strict"] = self.strict
        return {"type": "function", "function": function}


class OpenRouterClient:
    """Client synchrone/asynchrone pour les appels de chat OpenRouter.

    La classe conserve un historique de conversation et sait exécuter une
    boucle agentique : modèle -> outils locaux -> résultats -> modèle.
    Les fonctions d'outils reçoivent leurs arguments JSON sous forme de
    paramètres nommés, par exemple ``def get_weather(city: str) -> dict``.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        system_prompt: str | None = None,
        site_url: str | None = None,
        site_name: str | None = None,
        timeout: float | Any = 60.0,
        max_retries: int = 2,
        retry_backoff: float = 0.5,
        headers: Mapping[str, str] | None = None,
        default_params: Mapping[str, Any] | None = None,
        usage_observer: Callable[[dict[str, Any]], Any] | None = None,
        usage_ledger: UsageLedger | None = None,
        budget: BudgetTracker | None = None,
        rate_limit: float | None = None,
    ) -> None:
        if httpx is None:
            raise OpenRouterConfigurationError(
                "La dépendance 'httpx' est requise : pip install httpx"
            )

        resolved_key = api_key or os.getenv("OPENROUTER_API_KEY")
        if not resolved_key:
            raise OpenRouterConfigurationError(
                "Fournissez api_key ou définissez OPENROUTER_API_KEY."
            )
        if not model:
            raise OpenRouterConfigurationError("Le modèle ne peut pas être vide.")
        if max_retries < 0:
            raise OpenRouterConfigurationError("max_retries doit être positif ou nul.")
        if retry_backoff < 0:
            raise OpenRouterConfigurationError(
                "retry_backoff doit être positif ou nul."
            )

        self.api_key = resolved_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.budget = budget
        self._circuit = CircuitBreaker()
        self._rate_limiter = RateLimiter(rate_limit)
        self.default_params = dict(default_params or {})
        self.usage_ledger = usage_ledger or UsageLedger()
        if usage_observer is not None:
            self.usage_ledger.subscribe(usage_observer)

        request_headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if site_url:
            request_headers["HTTP-Referer"] = site_url
        if site_name:
            request_headers["X-OpenRouter-Title"] = site_name
        request_headers.update(headers or {})
        self._headers = request_headers

        self._client: Any = None
        self._async_client: Any = None
        self._messages: list[Message] = []
        self._tools: dict[str, RegisteredTool] = {}
        self._tools_lock = threading.RLock()
        self._tool_definitions_json: str | None = None
        self._last_response: dict[str, Any] | None = None

        if system_prompt:
            self._messages.append({"role": "system", "content": system_prompt})

    @property
    def usage_records(self) -> tuple[LLMUsageRecord, ...]:
        """Records observed by this client, in call creation order."""
        return self.usage_ledger.records

    def subscribe_usage(
        self, callback: Callable[[dict[str, Any]], Any]
    ) -> Callable[[], None]:
        return self.usage_ledger.subscribe(callback)

    def usage_snapshot(self) -> dict[str, Any]:
        return self.usage_ledger.snapshot()

    def usage_context(self, **kwargs: Any):
        return usage_context(**kwargs)

    def _start_usage(
        self, *, model: str | None, streamed: bool = False
    ) -> LLMUsageRecord:
        context = _usage_context.get()
        record = LLMUsageRecord(
            call_id=uuid.uuid4().hex,
            request_id=context.get("request_id"),
            correlation_id=context.get("correlation_id"),
            parent_call_id=context.get("parent_call_id"),
            stage=str(context.get("stage") or "other"),
            model=model or self.model,
            streamed=streamed,
        )
        self.usage_ledger.ingest(record, event="usage.started")
        return record

    @staticmethod
    def _decimal(value: Any) -> Decimal | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return None

    @staticmethod
    def _int(value: Any) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _finish_usage(
        self,
        record: LLMUsageRecord,
        payload: Mapping[str, Any] | None = None,
        *,
        headers: Mapping[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        if record.status != "inflight":
            return
        record.finished_at = datetime.now(timezone.utc)
        record.latency_ms = max(
            0.0, (record.finished_at - record.started_at).total_seconds() * 1000
        )
        if record.attempt < 1:
            record.attempt = 1
        if error is not None:
            record.status = "failed"
            record.error_type = type(error).__name__
            request_id = getattr(error, "request_id", None)
            if request_id:
                record.provider_request_id = str(request_id)
        else:
            body = payload or {}
            usage = body.get("usage") if isinstance(body, Mapping) else None
            if not isinstance(usage, Mapping):
                usage = {}
            record.provider_id = (
                str(body.get("id")) if body.get("id") is not None else None
            )
            effective_model = body.get("model")
            if effective_model:
                record.model = str(effective_model)
            prompt = self._int(usage.get("prompt_tokens"))
            completion = self._int(usage.get("completion_tokens"))
            total = self._int(usage.get("total_tokens"))
            record.prompt_tokens = prompt
            record.completion_tokens = completion
            record.total_tokens = total
            record.usage_complete = bool(usage) and any(
                value is not None for value in (prompt, completion, total)
            )
            for key, attr in (
                ("prompt_tokens_details", "prompt_details"),
                ("completion_tokens_details", "completion_details"),
                ("cost_details", "cost_details"),
            ):
                value = usage.get(key)
                if isinstance(value, Mapping):
                    setattr(record, attr, dict(value))
            cost = usage.get("cost", body.get("cost"))
            record.cost_usd = self._decimal(cost)
            if record.cost_usd is not None:
                record.cost_source = "openrouter"
            if headers:
                record.provider_request_id = (
                    str(
                        headers.get("x-request-id")
                        or headers.get("x-openrouter-request-id")
                    )
                    if (
                        headers.get("x-request-id")
                        or headers.get("x-openrouter-request-id")
                    )
                    else record.provider_request_id
                )
            record.status = "succeeded"
        self.usage_ledger.ingest(record, event="usage.updated")

    def _fail_usage(self, record: LLMUsageRecord, error: BaseException) -> None:
        self._finish_usage(record, error=error)

    # ------------------------------------------------------------------
    # Gestion du cycle de vie et du transport HTTP
    # ------------------------------------------------------------------
    def _sync_client(self) -> Any:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url, headers=self._headers, timeout=self.timeout
            )
        return self._client

    def _async_http_client(self) -> Any:
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(
                base_url=self.base_url, headers=self._headers, timeout=self.timeout
            )
        return self._async_client

    def close(self) -> None:
        """Ferme le client HTTP synchrone."""
        if self._client is not None:
            self._client.close()
            self._client = None

    async def aclose(self) -> None:
        """Ferme le client HTTP asynchrone."""
        if self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None
        self.close()

    def __enter__(self) -> OpenRouterClient:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    async def __aenter__(self) -> OpenRouterClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    @staticmethod
    def _retry_delay(response: Any, retry_number: int, backoff: float) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        return jittered_backoff(backoff, retry_number)

    @staticmethod
    def _error_from_response(response: Any) -> OpenRouterAPIError:
        try:
            payload = response.json()
        except Exception:
            payload = response.text

        error_data = (
            payload.get("error", payload) if isinstance(payload, dict) else payload
        )
        if isinstance(error_data, dict):
            # OpenRouter often wraps the useful provider diagnostic in
            # ``error.metadata.raw`` while keeping ``error.message`` at the
            # unhelpful value ``Provider returned error``.  Preserve the
            # structured object on the exception, but also expose the useful
            # parts in ``str(exc)`` so a Telegram/CLI error is actionable.
            parts: list[str] = []
            message_value = error_data.get("message")
            if message_value:
                parts.append(str(message_value))
            for key in ("code", "type"):
                value = error_data.get(key)
                if value and str(value) not in parts:
                    parts.append(f"{key}={value}")
            metadata = error_data.get("metadata")
            if isinstance(metadata, Mapping):
                provider = metadata.get("provider_name") or metadata.get("provider")
                if provider:
                    parts.append(f"provider={provider}")
                raw = metadata.get("raw") or metadata.get("error")
                if raw:
                    if isinstance(raw, Mapping):
                        raw = json.dumps(raw, ensure_ascii=False, default=str)
                    raw_text = str(raw)
                    # Avoid duplicating the generic message, while bounding
                    # an unexpectedly large provider response.
                    if raw_text and raw_text not in parts:
                        parts.append(raw_text[:2000])
            message = " · ".join(parts) or str(error_data)
        else:
            message = str(error_data)
        request_id = response.headers.get("x-request-id") or response.headers.get(
            "x-openrouter-request-id"
        )
        if request_id:
            message = f"{message} [request_id={request_id}]"
        return OpenRouterAPIError(
            f"OpenRouter HTTP {response.status_code}: {message}",
            status_code=response.status_code,
            error=error_data,
            request_id=request_id,
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        usage_record: LLMUsageRecord | None = None,
    ) -> dict[str, Any]:
        client = self._sync_client()
        if self.budget is not None:
            self.budget.check()
        if not self._circuit.allow():
            raise OpenRouterTransportError("Circuit breaker OpenRouter ouvert.")
        self._rate_limiter.wait()
        for attempt in range(self.max_retries + 1):
            if usage_record is not None:
                usage_record.attempt = attempt + 1
            try:
                response = client.request(
                    method, path.lstrip("/"), params=params, json=json_body
                )
            except httpx.TimeoutException as exc:
                self._circuit.failure()
                if attempt >= self.max_retries:
                    raise OpenRouterTimeoutError(
                        "La requête OpenRouter a expiré."
                    ) from exc
                time.sleep(jittered_backoff(self.retry_backoff, attempt))
                continue
            except httpx.HTTPError as exc:
                self._circuit.failure()
                if attempt >= self.max_retries:
                    raise OpenRouterTransportError(
                        f"Erreur réseau OpenRouter : {exc}"
                    ) from exc
                time.sleep(jittered_backoff(self.retry_backoff, attempt))
                continue

            if (
                response.status_code in RETRYABLE_STATUS_CODES
                and attempt < self.max_retries
            ):
                time.sleep(self._retry_delay(response, attempt, self.retry_backoff))
                continue
            if response.is_error:
                self._circuit.failure()
                raise self._error_from_response(response)
            try:
                payload = response.json()
            except ValueError as exc:
                raise OpenRouterError(
                    "OpenRouter a renvoyé une réponse JSON invalide."
                ) from exc
            if not isinstance(payload, dict):
                raise OpenRouterError("OpenRouter a renvoyé un JSON inattendu.")
            self._circuit.success()
            if self.budget is not None:
                self.budget.record(calls=1)
            if usage_record is not None:
                usage_record.provider_request_id = response.headers.get(
                    "x-request-id"
                ) or response.headers.get("x-openrouter-request-id")
            return payload

        raise OpenRouterTransportError(
            "La requête OpenRouter a échoué après plusieurs tentatives."
        )

    async def _request_json_async(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        usage_record: LLMUsageRecord | None = None,
    ) -> dict[str, Any]:
        client = self._async_http_client()
        if self.budget is not None:
            self.budget.check()
        if not self._circuit.allow():
            raise OpenRouterTransportError("Circuit breaker OpenRouter ouvert.")
        # RateLimiter is deliberately synchronous but bounded; yielding here
        # avoids blocking the event loop while preserving the same pacing.
        await asyncio.sleep(self._rate_limiter.reserve())
        for attempt in range(self.max_retries + 1):
            if usage_record is not None:
                usage_record.attempt = attempt + 1
            try:
                response = await client.request(
                    method, path.lstrip("/"), params=params, json=json_body
                )
            except httpx.TimeoutException as exc:
                self._circuit.failure()
                if attempt >= self.max_retries:
                    raise OpenRouterTimeoutError(
                        "La requête OpenRouter a expiré."
                    ) from exc
                await asyncio.sleep(jittered_backoff(self.retry_backoff, attempt))
                continue
            except httpx.HTTPError as exc:
                self._circuit.failure()
                if attempt >= self.max_retries:
                    raise OpenRouterTransportError(
                        f"Erreur réseau OpenRouter : {exc}"
                    ) from exc
                await asyncio.sleep(jittered_backoff(self.retry_backoff, attempt))
                continue

            if (
                response.status_code in RETRYABLE_STATUS_CODES
                and attempt < self.max_retries
            ):
                await asyncio.sleep(
                    self._retry_delay(response, attempt, self.retry_backoff)
                )
                continue
            if response.is_error:
                self._circuit.failure()
                raise self._error_from_response(response)
            try:
                payload = response.json()
            except ValueError as exc:
                raise OpenRouterError(
                    "OpenRouter a renvoyé une réponse JSON invalide."
                ) from exc
            if not isinstance(payload, dict):
                raise OpenRouterError("OpenRouter a renvoyé un JSON inattendu.")
            self._circuit.success()
            if self.budget is not None:
                self.budget.record(calls=1)
            if usage_record is not None:
                usage_record.provider_request_id = response.headers.get(
                    "x-request-id"
                ) or response.headers.get("x-openrouter-request-id")
            return payload

        raise OpenRouterTransportError(
            "La requête OpenRouter a échoué après plusieurs tentatives."
        )

    # ------------------------------------------------------------------
    # Appels API bas niveau
    # ------------------------------------------------------------------
    def _payload(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: Any = None,
        parallel_tool_calls: bool | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        if not messages:
            raise OpenRouterConfigurationError("messages ne peut pas être vide.")
        payload: dict[str, Any] = dict(self.default_params)
        payload.update(
            {
                "model": model or self.model,
                "messages": [self._normalize_message(m) for m in messages],
            }
        )
        normalized_tools = [dict(tool) for tool in tools] if tools else []
        if normalized_tools:
            payload["tools"] = normalized_tools
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice
            if parallel_tool_calls is not None:
                payload["parallel_tool_calls"] = parallel_tool_calls
        payload.update(params)
        # ``tools=[]`` and tool controls without tools are rejected by some
        # OpenAI-compatible providers.  This also removes stale values from
        # default_params when a final text-only turn is requested.
        if not normalized_tools:
            payload.pop("tools", None)
            payload.pop("tool_choice", None)
            payload.pop("parallel_tool_calls", None)
        return payload

    @staticmethod
    def _normalize_message(message: Mapping[str, Any]) -> Message:
        """Return an OpenAI-compatible copy of one outbound message.

        Provider adapters differ in how strictly they validate message
        fields.  In particular, a ``role=tool`` message accepts
        ``tool_call_id`` and ``content``; the optional ``name`` field is a
        legacy function-calling field and causes HTTP 400 on some routes.
        Keep the internal history untouched and normalize only the outbound
        copy.  Assistant tool calls are copied defensively so non-string
        argument objects cannot become invalid JSON payloads.
        """
        normalized = dict(message)
        if normalized.get("role") == "tool":
            content = normalized.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, default=str)
            result: Message = {
                "role": "tool",
                "tool_call_id": str(normalized.get("tool_call_id", "")),
                "content": content,
            }
            return result

        calls = normalized.get("tool_calls")
        if (
            normalized.get("role") == "assistant"
            and isinstance(calls, Sequence)
            and not isinstance(calls, (str, bytes))
        ):
            normalized_calls: list[dict[str, Any]] = []
            for call in calls:
                if not isinstance(call, Mapping):
                    continue
                normalized_call = dict(call)
                function = normalized_call.get("function")
                if isinstance(function, Mapping):
                    normalized_function = dict(function)
                    arguments = normalized_function.get("arguments")
                    if arguments is not None and not isinstance(arguments, str):
                        normalized_function["arguments"] = json.dumps(
                            arguments, ensure_ascii=False, default=str
                        )
                    normalized_call["function"] = normalized_function
                normalized_calls.append(normalized_call)
            normalized["tool_calls"] = normalized_calls
        return normalized

    @staticmethod
    def _compatibility_fallback_payload(
        payload: Mapping[str, Any], error: OpenRouterAPIError
    ) -> dict[str, Any] | None:
        """Drop optional tool controls after a provider-side HTTP 400.

        ``parallel_tool_calls`` is part of the Chat Completions contract but
        is not implemented by every provider behind OpenRouter.  A request
        rejected for that optional field is safe to retry without it: the
        model can still return one or more calls and Orion will execute them
        according to its local policy.  Restrict this fallback to HTTP 400
        responses and only when the field was actually sent.
        """
        if error.status_code != 400 or "parallel_tool_calls" not in payload:
            return None
        detail = str(error).lower()
        compatibility_hint = any(
            token in detail
            for token in (
                "parallel_tool_calls",
                "parallel tool",
                "unsupported",
                "not support",
                "unrecognized",
                "unknown field",
                "additional propert",
                "provider returned error",
            )
        )
        if not compatibility_hint:
            return None
        fallback = dict(payload)
        fallback.pop("parallel_tool_calls", None)
        return fallback

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: Any = None,
        parallel_tool_calls: bool | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        """Effectue un appel non-streaming à ``chat/completions``."""
        record = self._start_usage(model=model)
        try:
            request_payload = self._payload(
                messages,
                model=model,
                tools=tools,
                tool_choice=tool_choice,
                parallel_tool_calls=parallel_tool_calls,
                **params,
            )
            try:
                payload = self._request_json(
                    "POST",
                    "chat/completions",
                    json_body=request_payload,
                    usage_record=record,
                )
            except OpenRouterAPIError as exc:
                fallback_payload = self._compatibility_fallback_payload(
                    request_payload, exc
                )
                if fallback_payload is None:
                    raise
                try:
                    payload = self._request_json(
                        "POST",
                        "chat/completions",
                        json_body=fallback_payload,
                        usage_record=record,
                    )
                except OpenRouterAPIError as fallback_error:
                    raise fallback_error from exc
                # ``_request_json`` reports transport attempts, while this is
                # a second logical payload after a compatibility downgrade.
                record.attempt = max(record.attempt, 2)
        except BaseException as exc:
            self._fail_usage(record, exc)
            raise
        self._finish_usage(record, payload)
        return payload

    # Alias explicite pour les utilisateurs habitués au SDK OpenAI.
    chat_completion = complete

    async def complete_async(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: Any = None,
        parallel_tool_calls: bool | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        """Version asynchrone de :meth:`complete`."""
        record = self._start_usage(model=model)
        try:
            request_payload = self._payload(
                messages,
                model=model,
                tools=tools,
                tool_choice=tool_choice,
                parallel_tool_calls=parallel_tool_calls,
                **params,
            )
            try:
                payload = await self._request_json_async(
                    "POST",
                    "chat/completions",
                    json_body=request_payload,
                    usage_record=record,
                )
            except OpenRouterAPIError as exc:
                fallback_payload = self._compatibility_fallback_payload(
                    request_payload, exc
                )
                if fallback_payload is None:
                    raise
                try:
                    payload = await self._request_json_async(
                        "POST",
                        "chat/completions",
                        json_body=fallback_payload,
                        usage_record=record,
                    )
                except OpenRouterAPIError as fallback_error:
                    raise fallback_error from exc
                record.attempt = max(record.attempt, 2)
        except BaseException as exc:
            self._fail_usage(record, exc)
            raise
        self._finish_usage(record, payload)
        return payload

    async_chat_completion = complete_async

    def list_models(self) -> dict[str, Any]:
        """Retourne le catalogue de modèles OpenRouter."""
        return self._request_json("GET", "models")

    async def list_models_async(self) -> dict[str, Any]:
        """Version asynchrone de :meth:`list_models`."""
        return await self._request_json_async("GET", "models")

    def stream_chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: Any = None,
        parallel_tool_calls: bool | None = None,
        **params: Any,
    ) -> Generator[dict[str, Any], None, None]:
        """Diffuse les événements JSON du flux SSE OpenRouter."""
        record = self._start_usage(model=model, streamed=True)
        try:
            payload = self._payload(
                messages,
                model=model,
                tools=tools,
                tool_choice=tool_choice,
                parallel_tool_calls=parallel_tool_calls,
                stream=True,
                **params,
            )
            stream_options = payload.get("stream_options")
            if not isinstance(stream_options, Mapping):
                stream_options = {}
            payload["stream_options"] = {**dict(stream_options), "include_usage": True}
            client = self._sync_client()
            if self.budget is not None:
                self.budget.check()
            if not self._circuit.allow():
                raise OpenRouterTransportError("Circuit breaker OpenRouter ouvert.")
            self._rate_limiter.wait()

            final_event: Mapping[str, Any] | None = None
            emitted = False
            for attempt in range(self.max_retries + 1):
                record.attempt = attempt + 1
                try:
                    with client.stream(
                        "POST", "chat/completions", json=payload
                    ) as response:
                        if (
                            response.status_code in RETRYABLE_STATUS_CODES
                            and attempt < self.max_retries
                        ):
                            time.sleep(
                                self._retry_delay(response, attempt, self.retry_backoff)
                            )
                            continue
                        if response.is_error:
                            response.read()
                            self._circuit.failure()
                            raise self._error_from_response(response)
                        record.provider_request_id = response.headers.get(
                            "x-request-id"
                        ) or response.headers.get("x-openrouter-request-id")
                        for line in response.iter_lines():
                            if not line or line.startswith(":"):
                                continue
                            data = (
                                line[5:].strip()
                                if line.startswith("data:")
                                else line.strip()
                            )
                            if data == "[DONE]":
                                self._circuit.success()
                                if self.budget is not None:
                                    self.budget.record(calls=1)
                                self._finish_usage(record, final_event or {})
                                return
                            try:
                                event = json.loads(data)
                            except json.JSONDecodeError as exc:
                                raise OpenRouterError(
                                    f"Événement SSE OpenRouter invalide : {data}"
                                ) from exc
                            if isinstance(event, dict):
                                final_event = event
                                emitted = True
                                yield event
                        self._circuit.success()
                        if self.budget is not None:
                            self.budget.record(calls=1)
                        self._finish_usage(record, final_event or {})
                        return
                except httpx.TimeoutException as exc:
                    self._circuit.failure()
                    if emitted or attempt >= self.max_retries:
                        raise OpenRouterTimeoutError(
                            "Le flux OpenRouter a expiré."
                        ) from exc
                    time.sleep(jittered_backoff(self.retry_backoff, attempt))
                except httpx.HTTPError as exc:
                    self._circuit.failure()
                    if emitted or attempt >= self.max_retries:
                        raise OpenRouterTransportError(
                            f"Erreur réseau OpenRouter : {exc}"
                        ) from exc
                    time.sleep(jittered_backoff(self.retry_backoff, attempt))
        except BaseException as exc:
            self._fail_usage(record, exc)
            raise

    def stream_text(
        self, messages: Sequence[Mapping[str, Any]], **kwargs: Any
    ) -> Generator[str, None, None]:
        """Diffuse uniquement les fragments de texte de la réponse."""
        for event in self.stream_chat(messages, **kwargs):
            try:
                delta = event["choices"][0].get("delta", {})
            except (KeyError, IndexError, TypeError):
                continue
            content = delta.get("content") if isinstance(delta, dict) else None
            if isinstance(content, str):
                yield content

    async def stream_chat_async(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: Any = None,
        parallel_tool_calls: bool | None = None,
        **params: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Version asynchrone du flux SSE."""
        record = self._start_usage(model=model, streamed=True)
        try:
            payload = self._payload(
                messages,
                model=model,
                tools=tools,
                tool_choice=tool_choice,
                parallel_tool_calls=parallel_tool_calls,
                stream=True,
                **params,
            )
            stream_options = payload.get("stream_options")
            if not isinstance(stream_options, Mapping):
                stream_options = {}
            payload["stream_options"] = {**dict(stream_options), "include_usage": True}
            client = self._async_http_client()
            if self.budget is not None:
                self.budget.check()
            if not self._circuit.allow():
                raise OpenRouterTransportError("Circuit breaker OpenRouter ouvert.")
            await asyncio.sleep(self._rate_limiter.reserve())

            final_event: Mapping[str, Any] | None = None
            emitted = False
            for attempt in range(self.max_retries + 1):
                record.attempt = attempt + 1
                try:
                    async with client.stream(
                        "POST", "chat/completions", json=payload
                    ) as response:
                        if (
                            response.status_code in RETRYABLE_STATUS_CODES
                            and attempt < self.max_retries
                        ):
                            await asyncio.sleep(
                                self._retry_delay(response, attempt, self.retry_backoff)
                            )
                            continue
                        if response.is_error:
                            await response.aread()
                            self._circuit.failure()
                            raise self._error_from_response(response)
                        record.provider_request_id = response.headers.get(
                            "x-request-id"
                        ) or response.headers.get("x-openrouter-request-id")
                        async for line in response.aiter_lines():
                            if not line or line.startswith(":"):
                                continue
                            data = (
                                line[5:].strip()
                                if line.startswith("data:")
                                else line.strip()
                            )
                            if data == "[DONE]":
                                self._circuit.success()
                                if self.budget is not None:
                                    self.budget.record(calls=1)
                                self._finish_usage(record, final_event or {})
                                return
                            try:
                                event = json.loads(data)
                            except json.JSONDecodeError as exc:
                                raise OpenRouterError(
                                    f"Événement SSE OpenRouter invalide : {data}"
                                ) from exc
                            if isinstance(event, dict):
                                final_event = event
                                emitted = True
                                yield event
                        self._circuit.success()
                        if self.budget is not None:
                            self.budget.record(calls=1)
                        self._finish_usage(record, final_event or {})
                        return
                except httpx.TimeoutException as exc:
                    self._circuit.failure()
                    if emitted or attempt >= self.max_retries:
                        raise OpenRouterTimeoutError(
                            "Le flux OpenRouter a expiré."
                        ) from exc
                    await asyncio.sleep(jittered_backoff(self.retry_backoff, attempt))
                except httpx.HTTPError as exc:
                    self._circuit.failure()
                    if emitted or attempt >= self.max_retries:
                        raise OpenRouterTransportError(
                            f"Erreur réseau OpenRouter : {exc}"
                        ) from exc
                    await asyncio.sleep(jittered_backoff(self.retry_backoff, attempt))
        except BaseException as exc:
            self._fail_usage(record, exc)
            raise

    async def stream_text_async(
        self, messages: Sequence[Mapping[str, Any]], **kwargs: Any
    ) -> AsyncGenerator[str, None]:
        """Diffuse uniquement les fragments de texte, en asynchrone."""
        async for event in self.stream_chat_async(messages, **kwargs):
            try:
                delta = event["choices"][0].get("delta", {})
            except (KeyError, IndexError, TypeError):
                continue
            content = delta.get("content") if isinstance(delta, dict) else None
            if isinstance(content, str):
                yield content

    # ------------------------------------------------------------------
    # Historique et outils pour agent
    # ------------------------------------------------------------------
    @property
    def history(self) -> list[Message]:
        """Copie superficielle de l'historique courant."""
        return [dict(message) for message in self._messages]

    @property
    def last_response(self) -> dict[str, Any] | None:
        """Dernière réponse brute reçue du modèle."""
        return dict(self._last_response) if self._last_response else None

    def add_message(self, role: str, content: Any, **extra: Any) -> None:
        """Ajoute un message à la conversation courante."""
        if not role:
            raise OpenRouterConfigurationError(
                "Le rôle du message ne peut pas être vide."
            )
        message: Message = {"role": role, "content": content}
        message.update(extra)
        self._messages.append(message)

    def clear_history(self, *, keep_system: bool = True) -> None:
        """Efface l'historique, en conservant éventuellement le message système."""
        if keep_system and self._messages and self._messages[0].get("role") == "system":
            self._messages = [self._messages[0]]
        else:
            self._messages = []
        self._last_response = None

    def register_tool(
        self,
        name: str,
        handler: ToolHandler,
        *,
        description: str,
        parameters: Mapping[str, Any] | None = None,
        strict: bool | None = None,
        side_effect: bool = False,
        dedupe_window: float = 86400.0,
    ) -> OpenRouterClient:
        """Enregistre une fonction locale utilisable par l'agent.

        ``side_effect=True`` indique que l'outil peut envoyer, modifier ou
        supprimer quelque chose. Le runtime peut alors appliquer sa politique
        de déduplication avant de l'exécuter.
        """
        if not name or not callable(handler):
            raise OpenRouterConfigurationError(
                "Un outil doit avoir un nom et un handler appelable."
            )
        if dedupe_window < 0:
            raise OpenRouterConfigurationError(
                "dedupe_window doit être positif ou nul."
            )
        schema = copy.deepcopy(dict(parameters or {"type": "object", "properties": {}}))
        if schema.get("type") != "object":
            raise OpenRouterConfigurationError(
                "Le schéma des paramètres doit être un objet JSON."
            )
        with self._tools_lock:
            self._tools[name] = RegisteredTool(
                name=name,
                description=description,
                parameters=schema,
                handler=handler,
                strict=strict,
                side_effect=side_effect,
                dedupe_window=float(dedupe_window),
            )
            self._tool_definitions_json = None
        return self

    def unregister_tool(self, name: str) -> None:
        with self._tools_lock:
            removed = self._tools.pop(name, None)
            if removed is not None:
                self._tool_definitions_json = None

    def tool_definitions(self) -> list[dict[str, Any]]:
        """Retourne une copie isolée des définitions JSON envoyées au modèle.

        La représentation sérialisée est construite une seule fois par version
        du registre puis invalidée à chaque register/unregister. Désérialiser le
        cache fournit à chaque appel des objets mutables indépendants : un
        appelant peut modifier son résultat sans corrompre les paramètres des
        :class:`RegisteredTool` ni les appels suivants.
        """
        with self._tools_lock:
            cached = self._tool_definitions_json
            if cached is None:
                cached = json.dumps(
                    [tool.definition for tool in self._tools.values()],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                self._tool_definitions_json = cached
            return json.loads(cached)

    def get_registered_tool(self, name: str) -> RegisteredTool | None:
        """Retourne les métadonnées locales d'un outil enregistré."""
        with self._tools_lock:
            return self._tools.get(name)

    def execute_tool_call(
        self,
        call: Mapping[str, Any],
        *,
        raise_tool_errors: bool = True,
    ) -> Message:
        """Exécute publiquement un tool call reçu par un orchestrateur externe.

        Cette méthode permet à un runtime personnalisé de piloter lui-même la
        boucle LLM tout en réutilisant les tools enregistrés sur ce client.
        """
        return self._run_tool_sync(call, raise_tool_errors=raise_tool_errors)

    @staticmethod
    def _assistant_message(response: Mapping[str, Any]) -> Message:
        try:
            message = response["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise OpenRouterError(
                "Réponse OpenRouter sans message assistant exploitable."
            ) from exc
        if not isinstance(message, Mapping):
            raise OpenRouterError(
                "Le message assistant renvoyé par OpenRouter est invalide."
            )
        return dict(message)

    @staticmethod
    def text_from_message(message: Mapping[str, Any]) -> str:
        """Extrait le texte d'un message, y compris le contenu multimodal."""
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, Sequence) and not isinstance(
            content, (str, bytes, bytearray)
        ):
            parts = []
            for item in content:
                if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            return "".join(parts)
        return ""

    @classmethod
    def text_from_response(cls, response: Mapping[str, Any]) -> str:
        return cls.text_from_message(cls._assistant_message(response))

    @staticmethod
    def _tool_calls(message: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        calls = message.get("tool_calls", [])
        return (
            list(calls)
            if isinstance(calls, Sequence) and not isinstance(calls, (str, bytes))
            else []
        )

    @staticmethod
    def _serialize_tool_result(value: Any) -> str:
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)

    def _run_tool_sync(
        self, call: Mapping[str, Any], *, raise_tool_errors: bool
    ) -> Message:
        function = call.get("function", {})
        if not isinstance(function, Mapping):
            function = {}
        name = function.get("name") or call.get("name")
        tool_id = str(call.get("id") or f"call_{uuid.uuid4().hex}")
        with self._tools_lock:
            tool = self._tools.get(str(name))
        try:
            if tool is None:
                raise ToolExecutionError(
                    f"Outil inconnu demandé par le modèle : {name}"
                )
            raw_arguments = function.get("arguments", call.get("arguments", {}))
            if isinstance(raw_arguments, str):
                arguments = json.loads(raw_arguments or "{}")
            else:
                arguments = raw_arguments
            if not isinstance(arguments, Mapping):
                raise ToolExecutionError(f"Arguments invalides pour l'outil {name}.")
            result = tool.handler(**dict(arguments))
            if inspect.isawaitable(result):
                raise ToolExecutionError(
                    f"L'outil {name} est asynchrone ; utilisez run_async()."
                )
            content = self._serialize_tool_result(result)
        except Exception as exc:
            if raise_tool_errors:
                if isinstance(exc, ToolExecutionError):
                    raise
                raise ToolExecutionError(f"Échec de l'outil {name}: {exc}") from exc
            content = self._serialize_tool_result({"error": str(exc)})
        return {
            "role": "tool",
            "tool_call_id": tool_id,
            "name": str(name),
            "content": content,
        }

    async def _run_tool_async(
        self, call: Mapping[str, Any], *, raise_tool_errors: bool
    ) -> Message:
        function = call.get("function", {})
        if not isinstance(function, Mapping):
            function = {}
        name = function.get("name") or call.get("name")
        tool_id = str(call.get("id") or f"call_{uuid.uuid4().hex}")
        with self._tools_lock:
            tool = self._tools.get(str(name))
        try:
            if tool is None:
                raise ToolExecutionError(
                    f"Outil inconnu demandé par le modèle : {name}"
                )
            raw_arguments = function.get("arguments", call.get("arguments", {}))
            if isinstance(raw_arguments, str):
                arguments = json.loads(raw_arguments or "{}")
            else:
                arguments = raw_arguments
            if not isinstance(arguments, Mapping):
                raise ToolExecutionError(f"Arguments invalides pour l'outil {name}.")
            result = tool.handler(**dict(arguments))
            if inspect.isawaitable(result):
                result = await result
            content = self._serialize_tool_result(result)
        except Exception as exc:
            if raise_tool_errors:
                if isinstance(exc, ToolExecutionError):
                    raise
                raise ToolExecutionError(f"Échec de l'outil {name}: {exc}") from exc
            content = self._serialize_tool_result({"error": str(exc)})
        return {
            "role": "tool",
            "tool_call_id": tool_id,
            "name": str(name),
            "content": content,
        }

    def run(
        self,
        prompt: str,
        *,
        model: str | None = None,
        max_tool_rounds: int = 8,
        tool_choice: Any = None,
        parallel_tool_calls: bool | None = None,
        raise_tool_errors: bool = False,
        **params: Any,
    ) -> str:
        """Exécute une conversation agentique et retourne la réponse finale."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise OpenRouterConfigurationError(
                "Le prompt doit être une chaîne non vide."
            )
        if max_tool_rounds < 0:
            raise OpenRouterConfigurationError(
                "max_tool_rounds doit être positif ou nul."
            )
        self.add_message("user", prompt)

        for round_number in range(max_tool_rounds + 1):
            response = self.complete(
                self._messages,
                model=model,
                tools=self.tool_definitions() or None,
                tool_choice=tool_choice,
                parallel_tool_calls=parallel_tool_calls,
                **params,
            )
            self._last_response = response
            assistant = self._assistant_message(response)
            self._messages.append(assistant)
            calls = self._tool_calls(assistant)
            if not calls:
                return self.text_from_message(assistant)
            if round_number == max_tool_rounds:
                raise AgentLoopLimitError(
                    f"Le modèle a dépassé max_tool_rounds={max_tool_rounds}."
                )
            self._messages.extend(
                self._run_tool_sync(call, raise_tool_errors=raise_tool_errors)
                for call in calls
            )

        raise AgentLoopLimitError(
            "La boucle agentique s'est terminée sans réponse finale."
        )

    async def run_async(
        self,
        prompt: str,
        *,
        model: str | None = None,
        max_tool_rounds: int = 8,
        tool_choice: Any = None,
        parallel_tool_calls: bool | None = None,
        execute_tools_concurrently: bool = True,
        raise_tool_errors: bool = False,
        **params: Any,
    ) -> str:
        """Version asynchrone de :meth:`run`, compatible avec des outils async."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise OpenRouterConfigurationError(
                "Le prompt doit être une chaîne non vide."
            )
        if max_tool_rounds < 0:
            raise OpenRouterConfigurationError(
                "max_tool_rounds doit être positif ou nul."
            )
        self.add_message("user", prompt)

        for round_number in range(max_tool_rounds + 1):
            response = await self.complete_async(
                self._messages,
                model=model,
                tools=self.tool_definitions() or None,
                tool_choice=tool_choice,
                parallel_tool_calls=parallel_tool_calls,
                **params,
            )
            self._last_response = response
            assistant = self._assistant_message(response)
            self._messages.append(assistant)
            calls = self._tool_calls(assistant)
            if not calls:
                return self.text_from_message(assistant)
            if round_number == max_tool_rounds:
                raise AgentLoopLimitError(
                    f"Le modèle a dépassé max_tool_rounds={max_tool_rounds}."
                )
            if execute_tools_concurrently:
                results = await asyncio.gather(
                    *(
                        self._run_tool_async(call, raise_tool_errors=raise_tool_errors)
                        for call in calls
                    )
                )
                self._messages.extend(results)
            else:
                for call in calls:
                    self._messages.append(
                        await self._run_tool_async(
                            call, raise_tool_errors=raise_tool_errors
                        )
                    )

        raise AgentLoopLimitError(
            "La boucle agentique s'est terminée sans réponse finale."
        )

    def stream_run(self, prompt: str, **kwargs: Any) -> Generator[str, None, None]:
        """Diffuse une réponse texte simple et met à jour l'historique.

        Cette méthode ne gère pas l'exécution automatique des outils pendant le
        flux ; utilisez :meth:`run` ou :meth:`run_async` pour un agent outillé.
        """
        if not isinstance(prompt, str) or not prompt.strip():
            raise OpenRouterConfigurationError(
                "Le prompt doit être une chaîne non vide."
            )
        self.add_message("user", prompt)
        fragments: list[str] = []
        for fragment in self.stream_text(
            self._messages,
            tools=self.tool_definitions() or None,
            **kwargs,
        ):
            fragments.append(fragment)
            yield fragment
        self._messages.append({"role": "assistant", "content": "".join(fragments)})


__all__ = [
    "AgentLoopLimitError",
    "LLMUsageRecord",
    "OpenRouterAPIError",
    "OpenRouterClient",
    "OpenRouterConfigurationError",
    "OpenRouterError",
    "OpenRouterTimeoutError",
    "OpenRouterTransportError",
    "RegisteredTool",
    "ToolExecutionError",
    "UsageLedger",
    "usage_context",
]
