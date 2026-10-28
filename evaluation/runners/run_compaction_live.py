"""Opt-in real-model compaction comparison: full context vs real summary.

This entry is the paid, real-provider counterpart to the offline scripted
compaction suite. It compares, for one fixed task, two continuations that start
from the *same pre-compaction snapshot* in independent throwaway workspaces:

* ``FULL_CONTEXT`` — continue with the complete context (baseline);
* ``REAL_SUMMARY`` — continue after the session's own model produced a real
  summary that replaces the older history.

Both arms run through the single lifecycle owner ``run_turn`` (via
``runtime.evals.compaction_ab``). One shared ledger enforces ``--max-total-tokens``
across *both* arms and every summarizer call: each request reserves conservatively
before it is sent and an unaffordable request is refused before reaching the
provider.

A run is a **valid comparison** only when the summary arm actually compacted, a
later request used the reduced history, the current instruction survived, key
tool facts survived and a non-idempotent side effect was not replayed. Otherwise
the report is marked invalid — never a successful comparison.

Default behaviour is **LIVE_NOT_RUN**: with no ``--live`` flag the command spends
nothing, prints ``LIVE_NOT_RUN`` and writes a skeleton report whose model, usage
and timing fields are null.

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
from cairn.core.models import Message, ToolCall
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
LIVE_BLOCKED = "LIVE_BLOCKED"

#: The already-deployed log the continuation must not append to again.
_SIDE_EFFECT_LOG = "applied_log.txt"


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
    """One realistic continuation built to exercise a *real* compaction.

    The seeded history carries an older requirement (with a large analysis block
    the summary must compress away), an assistant tool call whose result is the
    only source of a tool fact, and a **pre-existing user correction** that a
    faithful summary must retain. The side-effect log is append-only, so a replay
    is observable. A generic deploy marker is always present, so a summary that
    keeps it but drops the correction or the tool fact is still detected.
    """
    big_analysis = (
        "Older requirement: staging only. Analysis: "
        + "A" * 300
        + " ref ANALYSIS-7781 generic GENERIC-DEPLOY-42 "
        "tool-ref TOOLFACT-9f3c2a correction CORRECTION-7b1d."
    )
    history = (
        Message(role="user", content=big_analysis),
        Message(role="assistant", content="acknowledged the staging deployment plan"),
        Message(
            role="assistant",
            content=None,
            tool_calls=[ToolCall(id="seed-1", name="apply_side_effect", arguments={})],
        ),
        Message(
            role="tool",
            content="deployment applied and appended once to applied_log.txt; do not repeat",
            tool_call_id="seed-1",
        ),
        Message(role="user", content="Proceed with the verification step."),
    )
    return CompactionCaseSpec(
        name="verify-after-deploy",
        prompt="Verify the deployment log and report finished.",
        files={
            "README.md": "The deployment appends one applied entry to applied_log.txt.\n",
            _SIDE_EFFECT_LOG: "applied\n",
        },
        pre_side_effects={},
        history=history,
        current_instruction=(
            "Verify applied_log.txt and report finished. Do not deploy again."
        ),
        budget_tokens=6_000,
        summary_budget_tokens=1_600,
        response_tokens=512,
        side_effect_tool="apply_side_effect",
        side_effect_log=_SIDE_EFFECT_LOG,
        forbidden_history_marker="ANALYSIS-7781",
        tool_fact_marker="TOOLFACT-9f3c2a",
        correction_marker="CORRECTION-7b1d",
        version="compaction-sample-v2",
        expected_side_effect_runs=0,
    )


def _checks() -> tuple[FileContentEqualsCheck, ...]:
    # Exactly one applied entry: a replay would append a second line.
    return (FileContentEqualsCheck(_SIDE_EFFECT_LOG, "applied\n"),)


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
        "replayed_side_effect": observation.replayed_side_effect,
        "compaction_occurred": observation.compaction_occurred,
        "compacted_history_used": observation.compacted_history_used,
        "key_fact_preserved": observation.key_fact_preserved,
        "tool_fact_preserved": observation.tool_fact_preserved,
        "correction_preserved": observation.correction_preserved,
        "summary_calls": observation.summary_calls,
        "request_count": observation.request_count,
        "tokens_spent": observation.tokens_spent,
        "usage_unknown": observation.usage_unknown,
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
        "schema_version": 3,
        "status": status,
        "case_name": report.case_name if report else _case_spec().name,
        "sample_version": _case_spec().version,
        "model_identifier": model,
        "provider": provider,
        "max_total_tokens": budget,
        "source_revision": revision,
        "notes": notes,
        "valid": report.valid if report else None,
        "invalid_reasons": report.invalid_reasons if report else None,
        "initial_snapshot_digest": report.initial_snapshot_digest if report else None,
        "tokens_spent_total": report.tokens_spent_total if report else None,
        "usage_unknown": report.usage_unknown if report else None,
        "full_context": _arm_json(report.full_context) if report else None,
        "real_summary": _arm_json(report.real_summary) if report else None,
        "both_ended_within_budget": report.both_ended_within_budget if report else None,
        # Correctness is kept even when an arm overspent: it is reported per arm
        # and as a combined verdict only when both arms terminated.
        "final_workspace_correct": report.final_workspace_correct if report else None,
    }
    return json.dumps(payload, indent=2) + "\n"


#: Below this many tokens a comparison cannot even reserve one request; refuse.
_MIN_TOKENS_FLOOR = 64


async def _run_live(*, model: str, max_total_tokens: int, destination: Path) -> int:
    spec = _case_spec()
    if max_total_tokens < _MIN_TOKENS_FLOOR + spec.response_tokens:
        print(
            f"{LIVE_BLOCKED}: --max-total-tokens={max_total_tokens} is too small to "
            f"cover one request (needs >= {_MIN_TOKENS_FLOOR + spec.response_tokens}); "
            "refusing before any provider call.",
            file=sys.stderr,
        )
        atomic_write_text(
            destination,
            _report_json(
                status=LIVE_BLOCKED,
                report=None,
                model=model,
                provider=None,
                budget=max_total_tokens,
                revision=_source_revision(),
                notes=["token budget too small to reserve a single request"],
            ),
        )
        return 2

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
                status=LIVE_BLOCKED,
                report=None,
                model=model,
                provider=None,
                budget=max_total_tokens,
                revision=_source_revision(),
                notes=["provider configuration or credentials unavailable"],
            ),
        )
        return 2

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
        total_token_budget=max_total_tokens,
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
    correct = bool(report.final_workspace_correct) and all(
        value is True for value in report.final_workspace_correct.values()
    )
    return 0 if (report.valid and correct) else 1


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
        help="Hard token cap across both arms and summarizer calls.",
    )
    raise SystemExit(main(parser.parse_args()))
