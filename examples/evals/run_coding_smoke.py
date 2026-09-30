"""Run the small coding corpus using the normal Cairn model configuration."""

import asyncio
import os
import sys
from collections import Counter
from pathlib import Path

from dotenv import dotenv_values

from cairn.cli import resolve_cairn_config
from cairn.core.budget import RunBudget
from cairn.evals import EvalResult, EvalRunner, EvalStatus
from cairn.llm.litellm_client import LiteLLMClient
from cairn.observability.sinks import JsonlTraceSink
from cairn.observability.tracer import Tracer

if not __package__:
    # Support the documented file invocation as well as package imports.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from examples.evals.coding_smoke_cases import coding_smoke_cases


def print_result(result: EvalResult) -> None:
    """Print a verdict and its failed-check diagnostics."""
    status = result.status.value.upper()
    line = f"{status:<5} {result.case_name}"
    if result.status in (EvalStatus.FAIL, EvalStatus.ERROR) and result.trace_id:
        line += f" trace={result.trace_id}"
    print(line, flush=True)

    if result.error:
        print(f"  - execution: {result.error}", flush=True)

    for check in result.checks:
        if check.error or not check.passed:
            detail = check.error or check.message or "check failed"
            print(f"  - {check.name}: {detail}", flush=True)


async def main() -> int:
    try:
        config = resolve_cairn_config(os.environ, dotenv_values())
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    runner = EvalRunner(
        lambda: LiteLLMClient(
            model=config["CAIRN_LLM_MODEL"],
            api_key=config["CAIRN_LLM_API_KEY"],
            api_base=config["CAIRN_BASE_URL"],
        ),
        budget=RunBudget(max_steps=20),
        run_timeout_seconds=120,
        check_timeout_seconds=2,
        tracer=Tracer(JsonlTraceSink(Path(".cairn/eval-traces"))),
    )

    print("coding smoke evals\n", flush=True)
    counts: Counter[EvalStatus] = Counter()
    for case, checks in coding_smoke_cases():
        result = await runner.run(case, checks=checks)
        print_result(result)
        counts[result.status] += 1

    print(
        f"\n{counts[EvalStatus.PASS]} passed, "
        f"{counts[EvalStatus.FAIL]} failed, {counts[EvalStatus.ERROR]} errors",
        flush=True,
    )
    return 1 if counts[EvalStatus.FAIL] or counts[EvalStatus.ERROR] else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
