"""Run the small coding corpus using the normal Cairn model configuration."""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import dotenv_values

from cairn.config import (
    ConfigLayout,
    default_provider,
    load_project_providers,
    resolve_context_budget,
    resolve_provider_api_key,
)
from cairn.core.budget import RunBudget
from cairn.evals import EvalResult, EvalRunner, EvalStatus, EvalSuiteRunner
from cairn.llm.litellm_client import LiteLLMClient
from cairn.llm.token_counter import LiteLLMTokenCounter
from cairn.observability.sinks import JsonlTraceSink
from cairn.observability.tracer import Tracer
from cairn.terminal.output import confirm_provider_access, print_provider_selection

if not __package__:
    # Support the documented file invocation as well as package imports.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from examples.evals.coding_smoke_cases import coding_smoke_suite


def print_result(result: EvalResult) -> None:
    """Print a verdict and its failed-check diagnostics."""
    status = result.status.value.upper()
    line = f"{status:<5} {result.case_name}"
    if result.status in (EvalStatus.FAIL, EvalStatus.ERROR) and result.trace_id:
        line += f" trace={result.trace_id}"
    print(line, flush=True)

    if result.error:
        category = (
            f" [{result.failure_category.value}]" if result.failure_category else ""
        )
        print(f"  - execution{category}: {result.error}", flush=True)

    for check in result.checks:
        if check.error is not None or not check.passed:
            detail = check.error or check.message or "check failed"
            identity = f"check #{check.check_index} " if check.check_index else ""
            identity += check.name
            if check.check_type:
                identity += f" ({check.check_type})"
            if check.target_path is not None:
                identity += f" target={check.target_path!r}"
            if check.failure_category:
                identity += f" [{check.failure_category.value}]"
            print(f"  - {identity}: {detail}", flush=True)


async def main(destination: Path) -> int:
    try:
        env_file_values = dotenv_values()
        project = load_project_providers()
        provider = default_provider(project, os.environ, env_file_values)
        group = provider.config.model_config[0]
        if project.layout is not ConfigLayout.ENV and (
            confirm_provider_access(provider, group, switching=False) is not True
        ):
            raise ValueError("Project model provider was not approved for this run.")
        api_key = resolve_provider_api_key(provider.config, os.environ, env_file_values)
        context_budget = resolve_context_budget(os.environ, env_file_values)
    except (OSError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    print_provider_selection(provider, group)
    model_id = group.model_ids[0]
    runner = EvalRunner(
        lambda: LiteLLMClient(
            model=model_id,
            api_key=api_key,
            api_base=provider.config.base_url,
            max_output_tokens=context_budget.response_tokens,
        ),
        budget=RunBudget(max_steps=20),
        run_timeout_seconds=120,
        check_timeout_seconds=2,
        tracer=Tracer(JsonlTraceSink(Path(".cairn/eval-traces"))),
        context_budget=context_budget,
        token_counter=LiteLLMTokenCounter(model_id),
        secret_env_keys=frozenset(
            named.config.api_key_env for named in project.providers or (provider,)
        ),
    )

    print("coding smoke evals\n", flush=True)
    try:
        report = await EvalSuiteRunner(runner).run(
            coding_smoke_suite(), destination=destination, on_result=print_result
        )
    except OSError:
        print("Could not persist the suite report.", file=sys.stderr)
        return 2

    print(
        f"\n{report.counts.passed} passed, "
        f"{report.counts.failed} failed, {report.counts.errors} errors",
        flush=True,
    )
    print(f"Report: {destination}", flush=True)
    return 1 if report.counts.failed or report.counts.errors else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report", type=Path, required=True, help="JSON report destination"
    )
    raise SystemExit(asyncio.run(main(parser.parse_args().report)))
