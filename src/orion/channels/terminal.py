"""Terminal channel: a minimal REPL for development and administration."""

from __future__ import annotations

import asyncio

from orion.guardian.policy import ActionProposal
from orion.providers.base import Usage
from orion.runtime import OWNER_THREAD, Orion, TurnResult

THREAD_ID = OWNER_THREAD

HELP = """Commandes : /stats (coût et cache de la session), /quit. Le reste part à Orion."""


class TerminalApprover:
    """Asks the owner on the terminal before any action that needs approval."""

    async def approve(self, proposal: ActionProposal) -> bool:
        answer = await asyncio.to_thread(
            input, f"\n⚠ Orion veut exécuter {proposal.describe()} — autoriser ? [o/N] "
        )
        return answer.strip().lower() in {"o", "oui", "y", "yes"}


def _format_stats(usage: Usage, turns: int) -> str:
    return (
        f"{turns} tour(s) · {usage.prompt_tokens} tokens en entrée dont {usage.cached_tokens} "
        f"depuis le cache ({usage.cache_hit_ratio:.0%}) · {usage.completion_tokens} en sortie · "
        f"{usage.cost_usd:.5f} $"
    )


async def run_terminal(orion: Orion) -> None:
    await orion.start()
    print(f"Orion est là. {HELP}")
    session = Usage()
    turns = 0
    while True:
        try:
            line = await asyncio.to_thread(input, "\ntoi > ")
        except EOFError:
            print()
            return
        text = line.strip()
        if not text:
            continue
        if text in {"/quit", "/exit"}:
            return
        if text == "/stats":
            print(_format_stats(session, turns))
            continue
        result: TurnResult = await orion.handle_owner_message(THREAD_ID, text, "terminal")
        session = session + result.usage
        turns += 1
        print(f"\norion > {result.text.strip() or '(pas de réponse)'}")
        if result.reason.value != "completed":
            print(f"   [arrêt : {result.reason.value} {result.detail}]".rstrip())
        report = await orion.consolidate(result)
        if report and report.applied:
            print(f"   [mémoire : {', '.join(report.applied)}]")
