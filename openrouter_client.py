"""Client Python robuste pour l'API OpenRouter.

Le module expose :class:`OpenRouterClient`, utilisable directement pour des
conversations ou comme moteur d'un agent avec appel d'outils.

Dépendance : ``httpx``.
La clé API est lue depuis ``OPENROUTER_API_KEY`` si elle n'est pas fournie au
constructeur.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
import uuid
from collections.abc import AsyncGenerator, Callable, Generator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

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
            raise OpenRouterConfigurationError("retry_backoff doit être positif ou nul.")

        self.api_key = resolved_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.default_params = dict(default_params or {})

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
        self._last_response: dict[str, Any] | None = None

        if system_prompt:
            self._messages.append({"role": "system", "content": system_prompt})

    # ------------------------------------------------------------------
    # Gestion du cycle de vie et du transport HTTP
    # ------------------------------------------------------------------
    def _sync_client(self) -> Any:
        if self._client is None:
            self._client = httpx.Client(base_url=self.base_url, headers=self._headers, timeout=self.timeout)
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
        return backoff * (2**retry_number)

    @staticmethod
    def _error_from_response(response: Any) -> OpenRouterAPIError:
        try:
            payload = response.json()
        except Exception:
            payload = response.text

        error_data = payload.get("error", payload) if isinstance(payload, dict) else payload
        if isinstance(error_data, dict):
            message = str(error_data.get("message", error_data))
        else:
            message = str(error_data)
        request_id = response.headers.get("x-request-id") or response.headers.get("x-openrouter-request-id")
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
    ) -> dict[str, Any]:
        client = self._sync_client()
        for attempt in range(self.max_retries + 1):
            try:
                response = client.request(
                    method, path.lstrip("/"), params=params, json=json_body
                )
            except httpx.TimeoutException as exc:
                if attempt >= self.max_retries:
                    raise OpenRouterTimeoutError("La requête OpenRouter a expiré.") from exc
                time.sleep(self.retry_backoff * (2**attempt))
                continue
            except httpx.HTTPError as exc:
                if attempt >= self.max_retries:
                    raise OpenRouterTransportError(f"Erreur réseau OpenRouter : {exc}") from exc
                time.sleep(self.retry_backoff * (2**attempt))
                continue

            if response.status_code in RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                time.sleep(self._retry_delay(response, attempt, self.retry_backoff))
                continue
            if response.is_error:
                raise self._error_from_response(response)
            try:
                payload = response.json()
            except ValueError as exc:
                raise OpenRouterError("OpenRouter a renvoyé une réponse JSON invalide.") from exc
            if not isinstance(payload, dict):
                raise OpenRouterError("OpenRouter a renvoyé un JSON inattendu.")
            return payload

        raise OpenRouterTransportError("La requête OpenRouter a échoué après plusieurs tentatives.")

    async def _request_json_async(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        client = self._async_http_client()
        for attempt in range(self.max_retries + 1):
            try:
                response = await client.request(
                    method, path.lstrip("/"), params=params, json=json_body
                )
            except httpx.TimeoutException as exc:
                if attempt >= self.max_retries:
                    raise OpenRouterTimeoutError("La requête OpenRouter a expiré.") from exc
                await asyncio.sleep(self.retry_backoff * (2**attempt))
                continue
            except httpx.HTTPError as exc:
                if attempt >= self.max_retries:
                    raise OpenRouterTransportError(f"Erreur réseau OpenRouter : {exc}") from exc
                await asyncio.sleep(self.retry_backoff * (2**attempt))
                continue

            if response.status_code in RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                await asyncio.sleep(self._retry_delay(response, attempt, self.retry_backoff))
                continue
            if response.is_error:
                raise self._error_from_response(response)
            try:
                payload = response.json()
            except ValueError as exc:
                raise OpenRouterError("OpenRouter a renvoyé une réponse JSON invalide.") from exc
            if not isinstance(payload, dict):
                raise OpenRouterError("OpenRouter a renvoyé un JSON inattendu.")
            return payload

        raise OpenRouterTransportError("La requête OpenRouter a échoué après plusieurs tentatives.")

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
        payload.update({"model": model or self.model, "messages": [dict(m) for m in messages]})
        if tools is not None:
            payload["tools"] = [dict(tool) for tool in tools]
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if parallel_tool_calls is not None:
            payload["parallel_tool_calls"] = parallel_tool_calls
        payload.update(params)
        return payload

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
        return self._request_json(
            "POST",
            "chat/completions",
            json_body=self._payload(
                messages,
                model=model,
                tools=tools,
                tool_choice=tool_choice,
                parallel_tool_calls=parallel_tool_calls,
                **params,
            ),
        )

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
        return await self._request_json_async(
            "POST",
            "chat/completions",
            json_body=self._payload(
                messages,
                model=model,
                tools=tools,
                tool_choice=tool_choice,
                parallel_tool_calls=parallel_tool_calls,
                **params,
            ),
        )

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
        payload = self._payload(
            messages,
            model=model,
            tools=tools,
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
            stream=True,
            **params,
        )
        client = self._sync_client()

        for attempt in range(self.max_retries + 1):
            try:
                with client.stream("POST", "chat/completions", json=payload) as response:
                    if response.status_code in RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                        time.sleep(self._retry_delay(response, attempt, self.retry_backoff))
                        continue
                    if response.is_error:
                        response.read()
                        raise self._error_from_response(response)
                    for line in response.iter_lines():
                        if not line or line.startswith(":"):
                            continue
                        data = line[5:].strip() if line.startswith("data:") else line.strip()
                        if data == "[DONE]":
                            return
                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError as exc:
                            raise OpenRouterError(f"Événement SSE OpenRouter invalide : {data}") from exc
                        if isinstance(event, dict):
                            yield event
                    return
            except httpx.TimeoutException as exc:
                if attempt >= self.max_retries:
                    raise OpenRouterTimeoutError("Le flux OpenRouter a expiré.") from exc
                time.sleep(self.retry_backoff * (2**attempt))
            except httpx.HTTPError as exc:
                if attempt >= self.max_retries:
                    raise OpenRouterTransportError(f"Erreur réseau OpenRouter : {exc}") from exc
                time.sleep(self.retry_backoff * (2**attempt))

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
        payload = self._payload(
            messages,
            model=model,
            tools=tools,
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
            stream=True,
            **params,
        )
        client = self._async_http_client()

        for attempt in range(self.max_retries + 1):
            try:
                async with client.stream("POST", "chat/completions", json=payload) as response:
                    if response.status_code in RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                        await asyncio.sleep(self._retry_delay(response, attempt, self.retry_backoff))
                        continue
                    if response.is_error:
                        await response.aread()
                        raise self._error_from_response(response)
                    async for line in response.aiter_lines():
                        if not line or line.startswith(":"):
                            continue
                        data = line[5:].strip() if line.startswith("data:") else line.strip()
                        if data == "[DONE]":
                            return
                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError as exc:
                            raise OpenRouterError(f"Événement SSE OpenRouter invalide : {data}") from exc
                        if isinstance(event, dict):
                            yield event
                    return
            except httpx.TimeoutException as exc:
                if attempt >= self.max_retries:
                    raise OpenRouterTimeoutError("Le flux OpenRouter a expiré.") from exc
                await asyncio.sleep(self.retry_backoff * (2**attempt))
            except httpx.HTTPError as exc:
                if attempt >= self.max_retries:
                    raise OpenRouterTransportError(f"Erreur réseau OpenRouter : {exc}") from exc
                await asyncio.sleep(self.retry_backoff * (2**attempt))

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
            raise OpenRouterConfigurationError("Le rôle du message ne peut pas être vide.")
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
            raise OpenRouterConfigurationError("Un outil doit avoir un nom et un handler appelable.")
        if dedupe_window < 0:
            raise OpenRouterConfigurationError("dedupe_window doit être positif ou nul.")
        schema = dict(parameters or {"type": "object", "properties": {}})
        if schema.get("type") != "object":
            raise OpenRouterConfigurationError("Le schéma des paramètres doit être un objet JSON.")
        self._tools[name] = RegisteredTool(
            name=name,
            description=description,
            parameters=schema,
            handler=handler,
            strict=strict,
            side_effect=side_effect,
            dedupe_window=float(dedupe_window),
        )
        return self

    def unregister_tool(self, name: str) -> None:
        self._tools.pop(name, None)

    def tool_definitions(self) -> list[dict[str, Any]]:
        """Retourne les définitions JSON envoyées au modèle."""
        return [tool.definition for tool in self._tools.values()]

    def get_registered_tool(self, name: str) -> RegisteredTool | None:
        """Retourne les métadonnées locales d'un outil enregistré."""
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
            raise OpenRouterError("Réponse OpenRouter sans message assistant exploitable.") from exc
        if not isinstance(message, Mapping):
            raise OpenRouterError("Le message assistant renvoyé par OpenRouter est invalide.")
        return dict(message)

    @staticmethod
    def text_from_message(message: Mapping[str, Any]) -> str:
        """Extrait le texte d'un message, y compris le contenu multimodal."""
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
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
        return list(calls) if isinstance(calls, Sequence) and not isinstance(calls, (str, bytes)) else []

    @staticmethod
    def _serialize_tool_result(value: Any) -> str:
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)

    def _run_tool_sync(self, call: Mapping[str, Any], *, raise_tool_errors: bool) -> Message:
        function = call.get("function", {})
        if not isinstance(function, Mapping):
            function = {}
        name = function.get("name") or call.get("name")
        tool_id = str(call.get("id") or f"call_{uuid.uuid4().hex}")
        tool = self._tools.get(str(name))
        try:
            if tool is None:
                raise ToolExecutionError(f"Outil inconnu demandé par le modèle : {name}")
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
        return {"role": "tool", "tool_call_id": tool_id, "name": str(name), "content": content}

    async def _run_tool_async(self, call: Mapping[str, Any], *, raise_tool_errors: bool) -> Message:
        function = call.get("function", {})
        if not isinstance(function, Mapping):
            function = {}
        name = function.get("name") or call.get("name")
        tool_id = str(call.get("id") or f"call_{uuid.uuid4().hex}")
        tool = self._tools.get(str(name))
        try:
            if tool is None:
                raise ToolExecutionError(f"Outil inconnu demandé par le modèle : {name}")
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
        return {"role": "tool", "tool_call_id": tool_id, "name": str(name), "content": content}

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
            raise OpenRouterConfigurationError("Le prompt doit être une chaîne non vide.")
        if max_tool_rounds < 0:
            raise OpenRouterConfigurationError("max_tool_rounds doit être positif ou nul.")
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
                self._run_tool_sync(call, raise_tool_errors=raise_tool_errors) for call in calls
            )

        raise AgentLoopLimitError("La boucle agentique s'est terminée sans réponse finale.")

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
            raise OpenRouterConfigurationError("Le prompt doit être une chaîne non vide.")
        if max_tool_rounds < 0:
            raise OpenRouterConfigurationError("max_tool_rounds doit être positif ou nul.")
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
                    *(self._run_tool_async(call, raise_tool_errors=raise_tool_errors) for call in calls)
                )
                self._messages.extend(results)
            else:
                for call in calls:
                    self._messages.append(
                        await self._run_tool_async(call, raise_tool_errors=raise_tool_errors)
                    )

        raise AgentLoopLimitError("La boucle agentique s'est terminée sans réponse finale.")

    def stream_run(self, prompt: str, **kwargs: Any) -> Generator[str, None, None]:
        """Diffuse une réponse texte simple et met à jour l'historique.

        Cette méthode ne gère pas l'exécution automatique des outils pendant le
        flux ; utilisez :meth:`run` ou :meth:`run_async` pour un agent outillé.
        """
        if not isinstance(prompt, str) or not prompt.strip():
            raise OpenRouterConfigurationError("Le prompt doit être une chaîne non vide.")
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
    "OpenRouterAPIError",
    "OpenRouterClient",
    "OpenRouterConfigurationError",
    "OpenRouterError",
    "OpenRouterTimeoutError",
    "OpenRouterTransportError",
    "RegisteredTool",
    "ToolExecutionError",
]
