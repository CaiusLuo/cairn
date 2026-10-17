"""Explicit finite real-provider pilot; uses configured provider without fallback.

Invocation authorizes up to 24 runs per repetition (maximum three). Defaults
are one repetition, eight Agent steps/run, and no network/tool approvals.
"""

import argparse
import asyncio
import hashlib
import os
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

from cairn.core.budget import RunBudget
from cairn.core.context import ContextBudget
from cairn.infra.config import (
    default_provider,
    load_project_providers,
    resolve_provider_api_key,
)
from cairn.infra.llm.litellm_client import LiteLLMClient
from cairn.infra.llm.token_counter import LiteLLMTokenCounter
from cairn.runtime.evals.intelligence.dataset import VERSION, dataset, dataset_digest
from cairn.runtime.evals.intelligence.experiment import run_experiment
from cairn.runtime.evals.intelligence.models import (
    ExperimentConfig,
    ExperimentReport,
    ExperimentRun,
)
from cairn.runtime.evals.intelligence.reporting import persist
from cairn.runtime.evals.models import EvalSuiteConfig


def source_revision() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def progress(run: ExperimentRun) -> None:
    print(
        f"{run.order_index + 1:02d} {run.arm.value} {run.case_name} r{run.repetition}: {run.result.status.value.upper()} trace={run.result.trace_id or 'unavailable'}",
        flush=True,
    )


async def main(directory: Path, *, repetitions: int = 1, seed: int = 230028) -> int:
    # Never overwrite a previous pilot or silently resume with different data.
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("Pilot destination must be empty")
    cases = dataset()
    settings = EvalSuiteConfig(
        run_budget=RunBudget(max_steps=8),
        context_budget=ContextBudget(max_tokens=16384, response_tokens=2048),
        run_timeout_seconds=120,
        check_timeout_seconds=5,
    )
    config = ExperimentConfig(
        dataset_version=VERSION,
        dataset_sha256=dataset_digest(cases),
        source_revision=source_revision(),
        execution_kind="real",
        model_identifier="unavailable",
        case_names=tuple(spec.item.case.name for spec in cases),
        repetitions=repetitions,
        seed=seed,
        settings=settings,
        sandbox="macos-sandbox-exec"
        if sys.platform == "darwin"
        else "linux-bubblewrap"
        if sys.platform == "linux"
        else "unsupported",
    )
    try:
        env_values = dotenv_values(Path(".env"))
        project = load_project_providers()
        provider = default_provider(project, os.environ, env_values)
        model_id = provider.config.model_config[0].model_ids[0]
        config.model_identifier = model_id
        config.provider_name = provider.name
        config.provider_endpoint_sha256 = hashlib.sha256(
            provider.config.base_url.encode()
        ).hexdigest()
        api_key = resolve_provider_api_key(provider.config, os.environ, env_values)
    except (OSError, ValueError):
        persist(
            ExperimentReport(
                config=config, state="real_eval_blocked", real_provider_status="BLOCKED"
            ),
            directory,
        )
        print(
            "REAL_EVAL_BLOCKED: provider configuration or credentials unavailable.",
            flush=True,
        )
        return 2
    print(
        f"Real pilot: model={model_id} provider={provider.name}; {len(cases) * 4 * repetitions} runs; {settings.run_budget.max_steps} steps/run; request cap={settings.context_budget.max_tokens}, output cap={settings.context_budget.response_tokens}; seed={seed}. No expansion or fallback.",
        flush=True,
    )
    report = await run_experiment(
        cases,
        config,
        lambda: LiteLLMClient(
            model=model_id,
            api_key=api_key,
            api_base=provider.config.base_url,
            max_output_tokens=settings.context_budget.response_tokens,
        ),
        directory=directory,
        token_counter=LiteLLMTokenCounter(model_id),
        secret_env_keys=frozenset(
            named.config.api_key_env for named in project.providers or (provider,)
        ),
        on_result=progress,
    )
    print(
        f"Real-provider status: {report.real_provider_status}; completed runs: {len(report.runs)}; report: {directory / 'findings.md'}",
        flush=True,
    )
    return (
        0
        if report.real_provider_status == "PASS"
        else 2
        if report.real_provider_status == "BLOCKED"
        else 1
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--seed", type=int, default=230028)
    args = parser.parse_args()
    raise SystemExit(
        asyncio.run(main(args.report_dir, repetitions=args.repetitions, seed=args.seed))
    )
