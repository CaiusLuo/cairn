"""Opt-in real-model compaction comparison: full context vs real summary.

This entry is the paid, real-provider counterpart to the offline scripted
compaction suite. It compares, for one fixed task, two continuations that start
from the *same pre-compaction snapshot* in independent throwaway workspaces:

* ``FULL_CONTEXT`` — continue with the complete context (baseline);
* ``REAL_SUMMARY`` — continue after the session's own model produced a real
  summary that replaces the older history.

Both arms run through the single lifecycle owner ``run_turn`` (via
``runtime.evals.compaction_ab``). The comparison is only meaningful when both
contexts fit the budget; correctness (independent final-workspace checks) is
reported separately from within-budget termination, and unknown observations stay
null.

Default behaviour is **LIVE_NOT_RUN**: with no ``--live`` flag the command spends
nothing, prints ``LIVE_NOT_RUN`` and writes a skeleton report whose model, usage
and timing fields are null. A real run requires ``--live`` plus an explicit
``--model`` and ``--max-total-tokens`` budget, so a paid experiment can never
start by accident.

Run offline help:

    uv run python evaluation/runners/run_compaction_live.py --help
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

from cairn.core.budget import RunBudget
from cairn.infra.config import (
    default_provider,
    load_project_providers,
    resolve_provider_api_key,
)
from cairn.infra.llm.compaction_summarizer import LLMCompactionSummarizer
from cairn.infra.llm.litellm_client import LiteLLMClient
from cairn.infra.llm.token_counter import LiteLLMTokenCounter
from cairn.runtime.artifacts import atomic_write_text
from cairn.runtime.evals.checks import FileContentEqualsCheck
from cairn.runtime.evals.compaction_ab import (
    ArmObservation,
    CompactionCaseSpec,
    ComparisonReport,
    compare_case,
)

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

LIVE_NOT_RUN = "LIVE_NOT_RUN"


def _source_revision() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def _case_spec() -> CompactionCaseSpec:
    """One fixed continuation task: write a file, then report.

    The pre-snapshot carries an already-applied side effect; a continuation must
    not replay it. The task is small and deterministic so the only difference
    between arms is the continuation context.
    """
    return CompactionCaseSpec(
        name="write-after-summary",
        prompt="Create output.txt containing 'done', then confirm.",
        files={
            "README.md": "Write output.txt with the word done.\n",
            "applied.txt": "already applied before this continuation\n",
        },
        current_instruction="Create output.txt containing done and report finished.",
        pre_side_effects={"applied.txt": "already applied before this continuation\n"},
        older_history=(
            "please prepare the workspace for the write task",
            "the workspace is ready and applied.txt was already written",
        ),
        budget_tokens=12_000,
        response_tokens=1_024,
    )


def _checks() -> tuple[FileContentEqualsCheck, ...]:
    return (FileContentEqualsCheck("output.txt", "done\n"),)


def _arm_json(observation: ArmObservation) -> dict[str, object]:
    return {
        "arm": observation.arm.value,
        "ended_within_budget": observation.ended_within_budget,
        "failure": observation.failure.value,
        "answer": observation.answer,
        "checks_passed": observation.checks_passed,
        "check_results": dict(observation.check_results),
        "instruction_reached_model": observation.instruction_reached_model,
        "side_effect_runs": observation.side_effect_runs,
        "summary_calls": observation.summary_calls,
        "model_identifier": observation.model_identifier,
        "model_version": observation.model_version,
        "provider_input_tokens": observation.provider_input_tokens,
        "provider_output_tokens": observation.provider_output_tokens,
        "elapsed_seconds": observation.elapsed_seconds,
        "error": observation.error,
    }


def _report_json(
    *,
    status: str,
    report: ComparisonReport | None,
    model: str | None,
    provider: str | None,
    budget: int | None,
    revision: str | None,
    notes: list[str],
) -> str:
    payload: dict[str, object] = {
        "schema_version": 1,
        "status": status,
        "case_name": report.case_name if report else _case_spec().name,
        "model_identifier": model,
        "provider": provider,
        "max_total_tokens": budget,
        "source_revision": revision,
        "notes": notes,
        "full_context": _arm_json(report.full_context) if report else None,
        "real_summary": _arm_json(report.real_summary) if report else None,
        "both_ended_within_budget": report.both_ended_within_budget if report else None,
        "final_workspace_correct": report.final_workspace_correct if report else None,
    }
    return json.dumps(payload, indent=2) + "\n"


async def _run_live(*, model: str, max_total_tokens: int, destination: Path) -> int:
    env_file_values = dotenv_values()
    try:
        project = load_project_providers()
        provider = default_provider(project, os.environ, env_file_values)
        api_key = resolve_provider_api_key(provider.config, os.environ, env_file_values)
    except (OSError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        atomic_write_text(
            destination,
            _report_json(
                status="LIVE_BLOCKED",
                report=None,
                model=model,
                provider=None,
                budget=max_total_tokens,
                revision=_source_revision(),
                notes=["provider configuration or credentials unavailable"],
            ),
        )
        return 2

    spec = _case_spec()
    counter = LiteLLMTokenCounter(model)

    def llm_factory() -> LiteLLMClient:
        return LiteLLMClient(
            model=model,
            api_key=api_key,
            api_base=provider.config.base_url,
            max_output_tokens=spec.response_tokens,
        )

    report = await compare_case(
        spec,
        llm_factory=llm_factory,
        checks=_checks(),
        budget=RunBudget(max_steps=10),
        run_timeout_seconds=180.0,
        check_timeout_seconds=10.0,
        token_counter=counter,
        summarizer_factory=LLMCompactionSummarizer,
    )
    atomic_write_text(
        destination,
        _report_json(
            status="LIVE_RUN",
            report=report,
            model=model,
            provider=provider.name,
            budget=max_total_tokens,
            revision=_source_revision(),
            notes=report.notes,
        ),
    )
    print(f"Report: {destination}", flush=True)
    # Exit non-zero only when the run itself failed, never for a failed check.
    return 0 if report.both_ended_within_budget else 1


def main(args: argparse.Namespace) -> int:
    destination = args.report
    if not args.live:
        atomic_write_text(
            destination,
            _report_json(
                status=LIVE_NOT_RUN,
                report=None,
                model=args.model,
                provider=None,
                budget=args.max_total_tokens,
                revision=_source_revision(),
                notes=[
                    "No paid experiment started. Pass --live --model <id> "
                    "--max-total-tokens <n> to authorize a real run."
                ],
            ),
        )
        print(f"{LIVE_NOT_RUN}: wrote skeleton report to {destination}")
        return 0
    if not args.model or not args.max_total_tokens:
        print("A real run requires --model and --max-total-tokens.", file=sys.stderr)
        return 2
    return asyncio.run(
        _run_live(
            model=args.model,
            max_total_tokens=args.max_total_tokens,
            destination=destination,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Authorize a real paid provider run (default: LIVE_NOT_RUN only).",
    )
    parser.add_argument("--model", type=str, default=None, help="Provider model id.")
    parser.add_argument(
        "--max-total-tokens",
        type=int,
        default=None,
        help="Hard token budget for the comparison; unknown means not authorized.",
    )
    raise SystemExit(main(parser.parse_args()))
