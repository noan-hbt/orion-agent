"""Assembly: builds one Orion from its configuration, for every entry point.

The core (journal, memory, guardian, reminders, the playbook) is always there; modules snap on
top (see `orion.modules`). The tool registry and the constitution depend only on which modules
are enabled, so `orion chat` and `orion serve` share one cached prompt prefix on the owner's
thread.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

from orion.adaptation.tools import adaptation_tools
from orion.config import OrionConfig
from orion.context.compiler import ContextCompiler
from orion.core.hlc import HybridClock
from orion.core.journal import SqliteJournal
from orion.guardian.autonomy import Autonomy, autonomy_tools
from orion.guardian.client import GuardianClient, GuardianUnavailableError
from orion.guardian.policy import Guardian
from orion.initiative.timers import TimerProjection, Timers
from orion.initiative.tools import reminder_tools
from orion.memory.extractor import MemoryExtractor
from orion.memory.people import people_tools
from orion.memory.provenance import Provenances, provenance_tools
from orion.memory.semantic import EmbeddingCache, SemanticIndex
from orion.memory.service import MemoryService
from orion.memory.store import MemoryStore
from orion.memory.tools import memory_tools
from orion.modules.base import LateApprover, M, Module, ModuleError, Plate
from orion.modules.registry import load
from orion.providers.base import Embedder, ModelProvider
from orion.resources import read_resource
from orion.runtime import Orion
from orion.tools.base import Tool, ToolRegistry
from orion.tools.builtin import builtin_tools
from orion.voice.module import VoiceModule

__all__ = ["App", "LateApprover", "build_app", "constitution"]


@dataclass
class App:
    plate: Plate
    orion: Orion

    @property
    def config(self) -> OrionConfig:
        return self.plate.config

    @property
    def provider(self) -> ModelProvider:
        return self.plate.provider

    @property
    def journal(self) -> SqliteJournal:
        return self.plate.journal

    @property
    def memory(self) -> MemoryService:
        return self.plate.memory

    @property
    def timers(self) -> Timers:
        return self.plate.timers

    @property
    def approver(self) -> LateApprover:
        return self.plate.approver

    @property
    def modules(self) -> list[Module]:
        return self.plate.modules

    def module(self, kind: type[M]) -> M | None:
        return self.plate.module(kind)


def constitution(modules: list[Module]) -> str:
    """The core constitution, then each enabled module's section, in loading order."""
    parts = [read_resource("constitution.md").strip()]
    parts += [text for text in (m.instructions_text() for m in modules) if text]
    return "\n\n".join(parts) + "\n"


def core_tools(plate: Plate) -> list[Tool]:
    return [
        *builtin_tools(),
        *memory_tools(plate.memory),
        *reminder_tools(plate.timers, calls=plate.module(VoiceModule) is not None),
        *adaptation_tools(plate.memory),
        *people_tools(plate.memory, plate.config.memory.owner_names),
        *provenance_tools(Provenances(plate.journal, plate.memory, plate.timers)),
    ]


@asynccontextmanager
async def build_app(
    config: OrionConfig,
    provider: ModelProvider,
    modules: list[Module] | None = None,
    guardian: GuardianClient | None = None,
) -> AsyncGenerator[App]:
    """`modules` overrides the ones the configuration enables (tests inject fakes), and
    `guardian` the guardian process's socket."""
    enabled = modules if modules is not None else load(config)
    store = MemoryStore(config.memory_path)
    await store.open()
    async with AsyncExitStack() as stack:
        stack.push_async_callback(store.close)
        if guardian is None and config.guardian.enabled:
            guardian = GuardianClient.over_socket(config.guardian.socket)
            stack.push_async_callback(guardian.aclose)
        if guardian is not None:
            try:
                await guardian.health()
            except GuardianUnavailableError as exc:
                raise ModuleError(f"{exc}: start `orion guardian` first") from exc
        journal = await stack.enter_async_context(
            SqliteJournal(config.journal_path, HybridClock(config.instance))
        )
        semantic: SemanticIndex | None = None
        if config.memory.embedding_model and isinstance(provider, Embedder):
            cache = EmbeddingCache(config.embeddings_path)
            await cache.open()
            stack.push_async_callback(cache.close)
            semantic = SemanticIndex(store, provider, cache, config.memory.embedding_model)
        memory = MemoryService(journal, store, semantic)
        autonomy = Autonomy(
            journal,
            config.guardian.autonomable,
            guardian,
            config.guardian.offer_autonomy_after,
            config.agent.timezone,
        )
        await autonomy.start()
        plate = Plate(
            config=config,
            provider=provider,
            journal=journal,
            memory=memory,
            timers=Timers(journal, TimerProjection()),
            approver=LateApprover(),
            modules=enabled,
            guardian=guardian,
            autonomy=autonomy,
        )
        tools = [*core_tools(plate), *autonomy_tools(autonomy)]
        for module in enabled:
            stack.push_async_callback(module.close)
            tools += await module.assemble(plate)

        orion = Orion(
            journal=journal,
            provider=provider,
            registry=ToolRegistry(tools),
            guardian=Guardian(plate.approver, autonomy.allows),
            compiler=ContextCompiler(constitution(enabled), config.agent.max_window_tokens),
            agent=config.agent,
            memory=memory,
            extractor=MemoryExtractor(provider, memory, config.agent.extractor_model),
            summary_model=config.agent.extractor_model,
        )
        await orion.start()
        await plate.timers.sync()
        yield App(plate, orion)
