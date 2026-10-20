"""Model router: one entry point for every model call, with fallbacks across providers.

A model spec is `provider:model` (e.g. `openrouter:anthropic/claude-haiku-5.5`), or a bare model
name for the default provider. When a call fails after the provider's own retries, the router
tries the next spec in the request's fallback chain. Falling back costs the prompt cache (a new
model has none), which is why the chain is only used on failure.
"""

from __future__ import annotations

import structlog

from orion.providers.base import (
    Embedder,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ProviderError,
)

log = structlog.get_logger(__name__)


def parse_spec(spec: str, default_provider: str) -> tuple[str, str]:
    provider, sep, model = spec.partition(":")
    if sep and "/" not in provider:
        return provider, model
    return default_provider, spec


class Router:
    name = "router"

    def __init__(
        self,
        providers: dict[str, ModelProvider],
        default_provider: str,
        fallbacks: dict[str, list[str]] | None = None,
    ) -> None:
        if default_provider not in providers:
            raise ValueError(f"unknown default provider {default_provider!r}")
        self._providers = providers
        self._default = default_provider
        self._fallbacks = fallbacks or {}
        for spec in [*self._fallbacks, *(s for chain in self._fallbacks.values() for s in chain)]:
            self._resolve(spec)  # fail at startup, not at the first failure

    async def complete(self, request: ModelRequest) -> ModelResponse:
        chain = [request.model, *self._fallbacks.get(request.model, [])]
        last_error: ProviderError | None = None
        for index, spec in enumerate(chain):
            provider, model = self._resolve(spec)
            try:
                response = await provider.complete(request.model_copy(update={"model": model}))
            except ProviderError as exc:
                last_error = exc
                if index + 1 < len(chain):
                    log.warning(
                        "router.fallback", failed=spec, next=chain[index + 1], error=str(exc)
                    )
                continue
            if index:
                log.warning("router.served_by_fallback", requested=request.model, served=spec)
            return response
        assert last_error is not None
        raise last_error

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        try:
            provider, name = self._resolve(model)
        except ValueError as exc:
            # Memory search falls back to words; an unreachable embedder must not stop Orion.
            raise ProviderError(str(exc), retryable=False) from exc
        if not isinstance(provider, Embedder):
            raise ProviderError(f"provider {provider.name!r} cannot embed", retryable=False)
        return await provider.embed(name, texts)

    def _resolve(self, spec: str) -> tuple[ModelProvider, str]:
        name, model = parse_spec(spec, self._default)
        provider = self._providers.get(name)
        if provider is None:
            raise ValueError(f"unknown provider {name!r} in model spec {spec!r}")
        return provider, model

    async def aclose(self) -> None:
        for provider in self._providers.values():
            close = getattr(provider, "aclose", None)
            if close is not None:
                await close()
