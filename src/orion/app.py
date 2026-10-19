"""Assembly: builds one Orion from its configuration, for every entry point.

The tool registry is the same whatever the channel, so `orion chat` and `orion serve` share
one cached prompt prefix on the owner's thread.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from orion.adaptation.tools import adaptation_tools
from orion.config import OrionConfig
from orion.context.compiler import ContextCompiler
from orion.core.hlc import HybridClock
from orion.core.journal import SqliteJournal
from orion.delegation.claude_code import ClaudeCodeAgent
from orion.delegation.jobs import JobService, delegation_tools
from orion.guardian.policy import ActionProposal, Approver, DenyAllApprover, Guardian
from orion.initiative.timers import TimerProjection, Timers
from orion.initiative.tools import reminder_tools
from orion.memory.extractor import MemoryExtractor
from orion.memory.service import MemoryService
from orion.memory.store import MemoryStore
from orion.memory.tools import memory_tools
from orion.providers.base import ModelProvider
from orion.resources import read_resource
from orion.runtime import Orion
from orion.tools.base import ToolRegistry
from orion.tools.builtin import builtin_tools
from orion.voice.tools import PhoneLink, voice_tools


class LateApprover:
    """Approver chosen after assembly (the iMessage channel needs Orion to exist first).
    Until one is set, everything that needs approval is refused."""

    def __init__(self) -> None:
        self.target: Approver = DenyAllApprover()

    async def approve(self, proposal: ActionProposal) -> bool:
        return await self.target.approve(proposal)


@dataclass
class App:
    config: OrionConfig
    provider: ModelProvider
    journal: SqliteJournal
    memory: MemoryService
    timers: Timers
    orion: Orion
    approver: LateApprover
    jobs: JobService | None = None
    phone: PhoneLink | None = None


@asynccontextmanager
async def build_app(config: OrionConfig, provider: ModelProvider) -> AsyncGenerator[App]:
    store = MemoryStore(config.memory_path)
    await store.open()
    try:
        async with SqliteJournal(config.journal_path, HybridClock(config.instance)) as journal:
            memory = MemoryService(journal, store)
            timers = Timers(journal, TimerProjection())
            approver = LateApprover()
            phone = PhoneLink()
            jobs: JobService | None = None
            if config.delegation.enabled:
                d = config.delegation
                agent = ClaudeCodeAgent(
                    d.binary, d.model, d.timeout_seconds, d.max_budget_usd, d.allow_bash
                )
                jobs = JobService(journal, agent, config.workspaces_path)
                await jobs.recover()
            orion = Orion(
                journal=journal,
                provider=provider,
                registry=ToolRegistry(
                    [
                        *builtin_tools(),
                        *memory_tools(memory),
                        *reminder_tools(timers, calls=config.voice.enabled),
                        *adaptation_tools(memory),
                        *(delegation_tools(jobs) if jobs else []),
                        *(voice_tools(phone) if config.voice.enabled else []),
                    ]
                ),
                guardian=Guardian(approver),
                compiler=ContextCompiler(
                    read_resource("constitution.md"), config.agent.max_window_tokens
                ),
                agent=config.agent,
                memory=memory,
                extractor=MemoryExtractor(provider, memory, config.agent.extractor_model),
            )
            await orion.start()
            await timers.sync()
            yield App(config, provider, journal, memory, timers, orion, approver, jobs, phone)
    finally:
        await store.close()
