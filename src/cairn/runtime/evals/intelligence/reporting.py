"""Sanitized experimental artifacts and comparisons, without inferred usage."""

import json
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Any

from cairn.runtime.artifacts import atomic_write_text
from cairn.runtime.evals.intelligence.models import Arm, ExperimentReport, ExperimentRun
from cairn.runtime.evals.models import EvalStatus

# Missing observations stay null; means disclose their available sample count.
MEASUREMENTS = (
    "tool_calls",
    "read_file_calls",
    "bash_calls",
    "bash_discovery_calls",
    "graph_query_calls",
    "tool_error_events",
    "tool_denied_events",
    "tool_nonzero_results",
    "bash_timeout_results",
    "provider_requests",
    "provider_responses",
    "provider_failures",
    "repeated_read_file_calls",
    "provider_input_tokens",
    "provider_output_tokens",
    "request_tokens_total",
    "agent_steps_used",
    "elapsed_seconds",
    "agent_elapsed_seconds",
    "fixture_setup_seconds",
    "graph_build_refresh_seconds",
    "memory_retrieval_freshness_seconds",
    "memory_fixture_seconds",
    "context_trimmed_turns",
    "context_trimmed_messages",
    "memory_candidates_checked",
    "memory_selected_events",
    "memory_included_events",
    "memory_budget_drop_events",
    "memory_size_drop_events",
    "memory_read_errors",
)

LIMITATIONS = (
    "Synthetic Python AST repositories; results do not establish general repository or production quality.",
    "Offline fake-LLM tests validate the harness only. One repetition establishes no statistical significance.",
    "Provider sampling defaults are unchanged; seeded scheduling controls order, not model randomness.",
    "Request counts include full serialized tool schemas and admitted memory. Provider usage is separate; no dollar cost is inferred.",
    "Discovery evidence means a ground-truth relevant path was returned by read_file or repo_graph, not that the model understood it. Bash discovery and repeats are unavailable beyond explicit utility/read-file classifiers.",
    "Bash discovery counts calls containing rg/grep/find/ls/tree tokens; this heuristic is not complete semantic search intent.",
    "Memory counts are retrieval/admission events and can count an entry repeatedly. Stale recall denominator is observed non-fresh candidates; no eligible denominator means null.",
    "Memory fixtures are human-authorized, host-allowlisted facts, with exact independent source checks; arbitrary statement truth still requires human judgment.",
    "Tools run locally with no OS sandbox and no approval handler, so a shell is refused. Host Git/Memory setup uses trusted code, outside evaluated model authority.",
    "End-to-end timing includes fixture setup, deterministic checks and cleanup; Agent timing and feature/setup overhead are separate. Memory timing includes an extra bounded measurement freshness check.",
    "Actual provider usage may be partial after provider failure; request totals count constructed requests, even if transport fails. Completed responses are recorded separately.",
    "EvalRunner reports ERROR on budget exhaustion even when every workspace check passes. Overall task-success and final-workspace check success are reported separately; finishing within the step budget is part of the former.",
    "Traces retain IDs/counts/timings only. Model text, tool output, check scripts, raw exceptions and credentials are excluded.",
)


def write_json(destination: Path, data: dict[str, Any]) -> None:
    """Replace one experiment record atomically; readers never see partial JSON."""
    atomic_write_text(
        destination,
        json.dumps(data, indent=2, ensure_ascii=True, allow_nan=False) + "\n",
    )


def _observations(runs: list[ExperimentRun], field: str) -> dict[str, Any]:
    values = [
        getattr(run.metrics, field)
        for run in runs
        if getattr(run.metrics, field) is not None
    ]
    return {
        "available": len(values),
        "total": sum(values) if values else None,
        "mean": mean(values) if values else None,
    }


def _arm_summary(runs: list[Any]) -> dict[str, Any]:
    """Aggregate one arm's runs into the reported measurement block."""
    counts = Counter(run.result.status.value for run in runs)
    categories = Counter(
        run.result.failure_category.value for run in runs if run.result.failure_category
    )
    included = sum(run.metrics.memory_included_events for run in runs)
    checked_runs = [run for run in runs if run.result.checks]
    checks_passed = sum(
        all(check.passed and check.error is None for check in run.result.checks)
        for run in checked_runs
    )
    nonfresh = sum(
        count
        for run in runs
        for state, count in run.metrics.memory_freshness_counts.items()
        if state != "fresh"
    )
    return {
        "runs": len(runs),
        "pass": counts["pass"],
        "fail": counts["fail"],
        "error": counts["error"],
        "task_success_rate": counts["pass"] / len(runs) if runs else None,
        "workspace_checks_available": len(checked_runs),
        "workspace_checks_passed": checks_passed,
        "workspace_check_success_rate": checks_passed / len(checked_runs)
        if checked_runs
        else None,
        "failure_categories": dict(categories),
        "failed_check_ids": dict(
            Counter(
                check.name
                for run in runs
                for check in run.result.checks
                if not check.passed
            )
        ),
        "measurements": {field: _observations(runs, field) for field in MEASUREMENTS},
        "memory_freshness_counts": dict(
            Counter(
                state
                for run in runs
                for state, count in run.metrics.memory_freshness_counts.items()
                for _ in range(count)
            )
        ),
        "stale_recall_rate": sum(
            run.metrics.memory_ineligible_included_events for run in runs
        )
        / nonfresh
        if nonfresh
        else None,
        "relevant_memory_precision": sum(
            run.metrics.memory_relevant_included_events for run in runs
        )
        / included
        if included
        else None,
        "relevant_paths_observed": sum(
            len(run.metrics.direct_discovery_evidence) for run in runs
        ),
        "bash_relevant_file_discovery": None,
        "repeated_bash_file_exploration": None,
    }


def _measurement_deltas(run: Any, baseline: Any) -> dict[str, Any]:
    """Per-measurement difference, null when either side has no observation."""
    deltas: dict[str, Any] = {}
    for field in MEASUREMENTS:
        current, original = (
            getattr(run.metrics, field),
            getattr(baseline.metrics, field),
        )
        deltas[field] = (
            current - original if current is not None and original is not None else None
        )
    return deltas


def _case_difference(run: Any, baseline: Any) -> dict[str, Any]:
    """One non-baseline run compared against its baseline counterpart."""
    return {
        "case": run.case_name,
        "arm": run.arm.value,
        "repetition": run.repetition,
        "run_id": run.run_id,
        "baseline_run_id": baseline.run_id,
        "status": run.result.status.value,
        "baseline_status": baseline.result.status.value,
        "failure_category": run.result.failure_category.value
        if run.result.failure_category
        else None,
        "pass_delta": int(run.result.status is EvalStatus.PASS)
        - int(baseline.result.status is EvalStatus.PASS),
        "differences": _measurement_deltas(run, baseline),
    }


def _matched_cohort(report: ExperimentReport) -> set[tuple[str, int]]:
    """Case/repetition pairs where every arm is present."""
    groups: dict[tuple[str, int], set[Arm]] = {}
    for run in report.runs:
        groups.setdefault((run.case_name, run.repetition), set()).add(run.arm)
    return {key for key, present in groups.items() if present == set(Arm)}


def comparison(report: ExperimentReport, *, matched: bool = True) -> dict[str, Any]:
    arms = {
        arm.value: _arm_summary([run for run in report.runs if run.arm is arm])
        for arm in Arm
    }
    baselines = {
        (run.case_name, run.repetition): run
        for run in report.runs
        if run.arm is Arm.BASELINE
    }
    differences = []
    for run in report.runs:
        baseline = baselines.get((run.case_name, run.repetition))
        if run.arm is Arm.BASELINE or baseline is None:
            continue
        differences.append(_case_difference(run, baseline))
    result = {
        "schema_version": 1,
        "execution_kind": report.config.execution_kind,
        "state": report.state,
        "config": report.config.model_dump(mode="json"),
        "arms": arms,
        "per_case_against_baseline": differences,
        "limitations": list(LIMITATIONS),
    }
    if matched:
        cohort = _matched_cohort(report)
        subset = report.model_copy(
            update={
                "runs": [
                    run
                    for run in report.runs
                    if (run.case_name, run.repetition) in cohort
                ]
            }
        )
        result["matched_case_repetitions"] = [
            {"case": name, "repetition": repetition}
            for name, repetition in sorted(cohort)
        ]
        result["matched_arms"] = comparison(subset, matched=False)["arms"]
    return result


def findings(report: ExperimentReport, summary: dict[str, Any]) -> str:
    config = report.config
    lines = [
        "# Repository intelligence pilot",
        "",
        f"Execution: **{config.execution_kind}**; state: **{report.state}**; real-provider status: **{report.real_provider_status or 'unavailable'}**.",
        f"Model: `{config.model_identifier}`; provider: `{config.provider_name or 'unavailable'}`; seed: `{config.seed}`; repetitions: {config.repetitions}.",
        f"Dataset: `{config.dataset_version}` / `{config.dataset_sha256}`; code: `{config.source_revision or 'unavailable'}`.",
        f"Budget: {config.settings.run_budget.max_steps} steps/run, {config.settings.context_budget.max_tokens} request tokens including {config.settings.context_budget.response_tokens} response reserve; run/check/tool timeouts {config.settings.run_timeout_seconds}/{config.settings.check_timeout_seconds}/{config.tool_timeout_seconds}s.",
        f"Sandbox: {config.sandbox}; network: {config.network_policy}.",
        "",
        "| Arm | Runs | PASS / FAIL / ERROR | Success | Mean tools | Mean reads | Mean Bash discovery | Mean Graph queries | Provider input / output totals | Request-count total | Mean seconds |",
        "| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: |",
    ]

    def number(value: Any) -> str:
        return (
            "unavailable"
            if value is None
            else f"{value:.2f}"
            if isinstance(value, float)
            else str(value)
        )

    for arm, data in summary["arms"].items():
        m = data["measurements"]
        lines.append(
            f"| {arm} | {data['runs']} | {data['pass']} / {data['fail']} / {data['error']} | {number(data['task_success_rate'])} | {number(m['tool_calls']['mean'])} | {number(m['read_file_calls']['mean'])} | {number(m['bash_discovery_calls']['mean'])} | {number(m['graph_query_calls']['mean'])} | {number(m['provider_input_tokens']['total'])} / {number(m['provider_output_tokens']['total'])} | {number(m['request_tokens_total']['total'])} | {number(m['elapsed_seconds']['mean'])} |"
        )
    lines.extend(
        [
            "",
            "All-observed aggregates above can be unbalanced after interruption. The matched comparison below uses only case/repetition groups observed in all four arms.",
            "",
            "| Matched arm | Runs | Overall PASS | Workspace checks PASS | Mean tools | Mean reads |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for arm, data in summary["matched_arms"].items():
        m = data["measurements"]
        lines.append(
            f"| {arm} | {data['runs']} | {data['pass']} | {data['workspace_checks_passed']} / {data['workspace_checks_available']} | {number(m['tool_calls']['mean'])} | {number(m['read_file_calls']['mean'])} |"
        )
    lines.extend(
        [
            "",
            "| Case / repetition | Arm | Verdict / category | Workspace checks | Failed checks | Trace |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for run in report.runs:
        failed = (
            ", ".join(check.name for check in run.result.checks if not check.passed)
            or "none"
        )
        lines.append(
            f"| {run.case_name} / {run.repetition} | {run.arm.value} | {run.result.status.value.upper()} / {run.result.failure_category.value if run.result.failure_category else 'none'} | {('PASS' if all(check.passed and check.error is None for check in run.result.checks) else 'FAIL') if run.result.checks else 'unavailable'} | {failed} | `{run.result.trace_id or 'unavailable'}` |"
        )
    lines.extend(
        [
            "",
            "| Case / repetition | Arm vs A | PASS delta | Tool delta | Read delta | Request-count delta |",
            "| --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in summary["per_case_against_baseline"]:
        d = row["differences"]
        lines.append(
            f"| {row['case']} / {row['repetition']} | {row['arm']} | {row['pass_delta']} | {number(d['tool_calls'])} | {number(d['read_file_calls'])} | {number(d['request_tokens_total'])} |"
        )
    lines.extend(
        [
            "",
            "Observed differences are descriptive. Faster retrieval is not evidence of higher task success unless deterministic PASS results improve. No case was tuned after observing pilot results.",
            "",
            "Assumptions and limitations:",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in LIMITATIONS)
    return "\n".join(lines) + "\n"


def persist(report: ExperimentReport, directory: Path) -> None:
    for run in report.runs:
        write_json(
            directory / "runs" / f"{run.run_id}.json", run.model_dump(mode="json")
        )
    write_json(directory / "experiment.json", report.model_dump(mode="json"))
    summary = comparison(report)
    write_json(directory / "comparison.json", summary)
    # JSON snapshots are the durable contract; Markdown is a derived view, and a
    # reader may open it while the run is still writing.
    atomic_write_text(directory / "findings.md", findings(report, summary))
