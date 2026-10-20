"""`uv run python -m evals [--model M] [--case NAME] [--repeat N] [--offline]`."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import structlog

from evals.cases import CASES
from evals.harness import Outcome, evaluate, model_judge, run_case
from orion.cli import _provider  # pyright: ignore[reportPrivateUsage]
from orion.config import load_config

REPORTS = Path(__file__).parent / "reports"


async def main(args: argparse.Namespace) -> int:
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR))
    config = load_config(Path(args.config))
    provider = _provider(config)
    if provider is None:
        return 3
    model = args.model or config.agent.model
    judge = model_judge(provider, args.judge or config.agent.verifier_model)
    cases = [
        c
        for c in CASES
        if (not args.case or args.case in c.name) and not (args.offline and c.live_web)
    ]
    outcomes: list[Outcome] = []
    try:
        for case in cases:
            for _ in range(args.repeat):
                observation = await run_case(case, config, provider, args.model)
                outcome = await evaluate(case, observation, judge)
                outcomes.append(outcome)
                mark = "ok  " if outcome.passed else "FAIL"
                print(
                    f"{mark} {case.name:<28} {observation.seconds:5.1f}s "
                    f"${observation.cost:.4f}  {' | '.join(outcome.failures)}"
                )
                if not outcome.passed:
                    print(f"     « {observation.last[:300]} »")
    finally:
        await provider.aclose()
    passed = sum(o.passed for o in outcomes)
    cost = sum(o.observation.cost for o in outcomes)
    print(f"\n{model}: {passed}/{len(outcomes)} passed, ${cost:.4f}")
    REPORTS.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report = REPORTS / f"{stamp}-{model.replace('/', '_')}.json"
    report.write_text(
        json.dumps(
            {
                "model": model,
                "passed": passed,
                "total": len(outcomes),
                "cost_usd": cost,
                "results": [
                    {
                        "case": o.case,
                        "passed": o.passed,
                        "failures": o.failures,
                        "replies": o.observation.replies,
                        "tools": o.observation.tools,
                        "reminders": [r.isoformat() for r in o.observation.reminders],
                        "seconds": round(o.observation.seconds, 2),
                        "cost_usd": o.observation.cost,
                    }
                    for o in outcomes
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"report: {report}")
    return 0 if passed == len(outcomes) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="python -m evals")
    parser.add_argument("--config", default="orion.toml")
    parser.add_argument("--model", help="the model to evaluate (default: agent.model)")
    parser.add_argument("--judge", help="the judge model (default: agent.verifier_model)")
    parser.add_argument("--case", help="only cases whose name contains this")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--offline", action="store_true", help="skip cases reading the web")
    sys.exit(asyncio.run(main(parser.parse_args())))
