"""Run the versioned coding-behavior suite with the normal Cairn model config.

This is the graded-behavior counterpart to ``run_coding_smoke.py``: same model
configuration, same ``EvalRunner``/``EvalSuiteRunner`` lifecycle, but it selects
the versioned behavioral acceptance set (``graded-behavior-v1``) instead of the
frozen exact-content smoke corpus. It does not reimplement the runner or the
provider wiring — it imports the existing ``print_result`` and mirrors the
smoke runner's configuration resolution.

A real provider run needs separate authorization; ``--help`` is offline. Offline
validation lives in ``tests/runtime/evals/test_coding_behavior_cases.py``.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import dotenv_values

from cairn.core.budget import RunBudget
from cairn.infra.config import (
    ConfigLayout,
    default_provider,
    load_project_providers,
    resolve_context_budget,
    resolve_provider_api_key,
)
from cairn.infra.llm.litellm_client import LiteLLMClient
from cairn.infra.llm.token_counter import LiteLLMTokenCounter
from cairn.infra.observability.sinks import JsonlTraceSink
from cairn.infra.observability.tracer import Tracer
from cairn.infra.sandbox import available as sandbox_available
from cairn.runtime.evals import EvalRunner, EvalSuiteRunner
from cairn.terminal.output import confirm_provider_access, print_provider_selection

if not __package__:
    # Support the documented file invocation as well as package imports.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evaluation.cases.coding_smoke_behavior import behavior_suite
from evaluation.runners.run_coding_smoke import print_result


async def main(destination: Path) -> int:
    # Behavior grading executes model-generated code, so it must run inside the
    # execution sandbox. Block a real run up front if none is available; never
    # fall back to grading generated code on the host.
    if not sandbox_available():
        print(
            "BLOCKED: execution sandbox (bubblewrap) unavailable; "
            "refusing to grade generated code on the host.",
            file=sys.stderr,
        )
        return 2
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
        check_timeout_seconds=10,
        tracer=Tracer(JsonlTraceSink(Path(".cairn/eval-traces"))),
        context_budget=context_budget,
        token_counter=LiteLLMTokenCounter(model_id),
        secret_env_keys=frozenset(
            named.config.api_key_env for named in project.providers or (provider,)
        ),
        trust_local_shell=True,
    )

    print("coding behavior evals (graded-behavior-v1)\n", flush=True)
    try:
        report = await EvalSuiteRunner(runner).run(
            behavior_suite(), destination=destination, on_result=print_result
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
