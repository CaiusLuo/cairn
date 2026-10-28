"""SWE-bench Lite subset entry: adapter, command preview and grading execution.

Four distinct modes, each with its own status — so "printed a command" is never
mistaken for "ran the evaluation":

1. ``--check`` — verify the pinned manifest. Status ``MANIFEST_OK``.
2. ``--diffs DIR --predictions P`` — convert existing patches to official
   prediction JSONL. Status ``PREDICTIONS_WRITTEN`` (no scoring).
3. ``--predictions P --print-command [--run-id ID]`` — print the exact official
   harness command. Status ``COMMAND_READY`` (``executed=False``; nothing ran).
4. ``--predictions P --grade --run-id ID --model M --max-total-tokens N
   --isolated`` — actually invoke the official SWE-bench Docker harness on the
   predictions and parse its ``results.json``. Status ``GRADED`` (or
   ``LIVE_BLOCKED`` when the harness is unavailable). This is the only mode that
   executes grading, and it requires an explicit model, budget and isolation flag.

``--live`` is the full closed loop (Cairn solve → patch → official grade). The
solve stage needs an isolated container agent runner that is not built in this
round, so ``--live`` reports ``PARTIAL`` with the missing stages listed and never
claims a score.

The official harness owns the environment, patch application and grading; this
script never applies a patch or runs a repository's tests itself. See
https://www.swebench.com/SWE-bench/guides/evaluation/
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

if not __package__:
    # Support the documented file invocation as well as package imports.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evaluation.benchmarks import swebench_lite as lite

LIVE_NOT_RUN = "LIVE_NOT_RUN"
LIVE_BLOCKED = "LIVE_BLOCKED"
MANIFEST_OK = "MANIFEST_OK"
PREDICTIONS_WRITTEN = "PREDICTIONS_WRITTEN"
COMMAND_READY = "COMMAND_READY"
GRADED = "GRADED"
PARTIAL = "PARTIAL"

#: Stage missing from the closed loop this round.
_MISSING_SOLVE = (
    "cairn-solve-patch-generation (needs an isolated container agent runner; "
    "not built this round)"
)


def _result(
    status: str, *, executed: bool, rc: int, extra: dict[str, object] | None = None
) -> int:
    payload = {"status": status, "executed": executed, "rc": rc, **(extra or {})}
    print("RESULT " + json.dumps(payload, sort_keys=True))
    return rc


def _predict_from_diffs(diffs: Path, model: str) -> list[lite.Prediction]:
    predictions: list[lite.Prediction] = []
    for item in lite.SUBSET:
        patch_path = diffs / f"{item.instance_id}.patch"
        patch = patch_path.read_text(encoding="utf-8") if patch_path.is_file() else ""
        predictions.append(lite.prediction_for_diff(item.instance_id, model, patch))
    return predictions


def _official_command(predictions: Path, run_id: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        "princeton-nlp/SWE-bench_Lite",
        "--predictions_path",
        str(predictions),
        "--run_id",
        run_id,
        "--instance_ids",
        *lite.instance_ids(),
    ]


def _official_harness_available() -> bool:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "swebench.harness.run_evaluation", "--help"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _run_official_harness(
    predictions: Path, run_id: str, *, timeout_seconds: float = 6 * 3600
) -> tuple[int, Path | None]:
    """Run the official harness; return (exit code, results.json path if found)."""
    command = _official_command(predictions, run_id)
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError):
        return 127, None
    candidates = sorted(
        Path("logs").glob(f"evaluation/**/{run_id}/results.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return result.returncode, (candidates[0] if candidates else None)


def _summarize(results_path: Path | None) -> dict[str, object] | None:
    if results_path is None or not results_path.is_file():
        return None
    try:
        results = json.loads(results_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    summary = lite.summarize_results(results)
    return {
        "total": summary.total,
        "submitted": summary.submitted,
        "completed": summary.completed,
        "resolved": summary.resolved,
        "unresolved": summary.unresolved,
        "empty_patches": summary.empty_patches,
        "errors": summary.errors,
        "incomplete": summary.incomplete,
        "resolved_rate": summary.resolved_rate,
    }


def main(args: argparse.Namespace) -> int:
    if args.check:
        print(f"subset: {lite.SUBSET_VERSION} ({len(lite.SUBSET)} instances)")
        print(f"digest: {lite.subset_digest()}")
        print("This is an internal subset, not the full SWE-bench Lite leaderboard.")
        for item in lite.SUBSET:
            version = item.version if item.version is not None else "unknown"
            print(f"  {item.instance_id:<32} {item.repo:<20} v{version}")
        return _result(MANIFEST_OK, executed=False, rc=0)

    if args.diffs is not None:
        if args.predictions is None:
            print("--predictions is required with --diffs.", file=sys.stderr)
            return _result(
                LIVE_BLOCKED, executed=False, rc=2, extra={"missing": ["--predictions"]}
            )
        model = args.model or "cairn-unspecified-model"
        predictions = _predict_from_diffs(args.diffs, model)
        lite.write_predictions(predictions, args.predictions)
        print(f"wrote {len(predictions)} predictions to {args.predictions}")
        return _result(
            PREDICTIONS_WRITTEN, executed=False, rc=0, extra={"count": len(predictions)}
        )

    if args.predictions is None:
        if not args.live:
            print(
                f"{LIVE_NOT_RUN}: nothing to do (give --check/--diffs/--grade).",
                file=sys.stderr,
            )
            return _result(LIVE_NOT_RUN, executed=False, rc=0)
        print(
            f"{PARTIAL}: Cairn solve -> patch generation is not implemented; only "
            "grading of pre-existing predictions is available via --grade.",
            file=sys.stderr,
        )
        return _result(
            PARTIAL, executed=False, rc=2, extra={"missing": [_MISSING_SOLVE]}
        )

    if args.print_command:
        run_id = args.run_id or "cairn-preview"
        print(" ".join(_official_command(args.predictions, run_id)))
        return _result(COMMAND_READY, executed=False, rc=0, extra={"run_id": run_id})

    if args.grade:
        missing: list[str] = []
        if not args.run_id:
            missing.append("--run-id")
        if not args.model:
            missing.append("--model")
        if not args.max_total_tokens:
            missing.append("--max-total-tokens")
        if not args.isolated:
            missing.append("--isolated")
        if missing:
            print(f"{LIVE_BLOCKED}: missing {', '.join(missing)}", file=sys.stderr)
            return _result(
                LIVE_BLOCKED, executed=False, rc=2, extra={"missing": missing}
            )
        if not _official_harness_available():
            print(
                f"{LIVE_BLOCKED}: the official SWE-bench harness is not installed; "
                "install `swebench` and a Docker daemon. No patch applied, no test run.",
                file=sys.stderr,
            )
            return _result(
                LIVE_BLOCKED,
                executed=False,
                rc=2,
                extra={"missing": ["official-harness"]},
            )
        rc, results_path = _run_official_harness(args.predictions, args.run_id)
        summary = _summarize(results_path)
        extra = {
            "run_id": args.run_id,
            "model": args.model,
            "max_total_tokens": args.max_total_tokens,
            "isolated": True,
            "results_path": str(results_path) if results_path else None,
            "summary": summary,
        }
        status = GRADED if rc == 0 else LIVE_BLOCKED
        return _result(status, executed=True, rc=0 if rc == 0 else 1, extra=extra)

    if args.live:
        print(f"{PARTIAL}: closed loop incomplete.", file=sys.stderr)
        return _result(
            PARTIAL, executed=False, rc=2, extra={"missing": [_MISSING_SOLVE]}
        )

    print(f"{LIVE_NOT_RUN}: no mode selected.", file=sys.stderr)
    return _result(LIVE_NOT_RUN, executed=False, rc=0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify the manifest.")
    parser.add_argument("--diffs", type=Path, default=None)
    parser.add_argument("--predictions", type=Path, default=None)
    parser.add_argument(
        "--print-command", action="store_true", help="Print, do not run."
    )
    parser.add_argument(
        "--grade", action="store_true", help="Run the official harness."
    )
    parser.add_argument(
        "--live", action="store_true", help="Full closed loop (partial)."
    )
    parser.add_argument("--official-harness", action="store_true", help="(reserved)")
    parser.add_argument("--model", type=str, default=None)
    parser.add_argument("--max-total-tokens", type=int, default=None)
    parser.add_argument("--run-id", type=str, default=None)
    parser.add_argument(
        "--isolated", action="store_true", help="Confirm container/VM isolation."
    )
    raise SystemExit(main(parser.parse_args()))
