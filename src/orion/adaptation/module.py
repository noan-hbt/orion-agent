"""Learning the owner's ways in the background (reactions, ignored digests) as a module.

The playbook itself and the tools to change it explicitly (`adapt`, `unlearn`…) belong to the
core: the owner can always tell Orion how to behave. This module adds the periodic reflection
that infers rules on its own.
"""

from __future__ import annotations

from orion.adaptation.reflector import Reflector
from orion.adaptation.service import AdaptationSettings, Adapter
from orion.modules.base import Daemon, Module


class AdaptationModule(Module):
    name = "adaptation"

    @property
    def enabled(self) -> bool:
        return self.config.adaptation.enabled

    async def serve(self, daemon: Daemon) -> None:
        cfg = self.config.adaptation
        plate = daemon.plate
        adapter = Adapter(
            plate.journal,
            plate.memory,
            Reflector(
                plate.provider,
                plate.memory,
                self.config.agent.reflection_model or self.config.agent.extractor_model,
            ),
            AdaptationSettings(
                timezone=self.config.agent.timezone,
                digests=tuple(self.config.initiative.digests),
                reflect_after_messages=cfg.reflect_after_messages,
                reflect_daily_after_messages=cfg.reflect_daily_after_messages,
            ),
        )
        daemon.add_service("adaptation", lambda: adapter.run(cfg.tick_seconds))
