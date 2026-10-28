"""SWE-bench Lite subset entry: adapter + offline format check; LIVE_NOT_RUN.

Thin on purpose. The library is
[`evaluation/benchmarks/swebench_lite.py`](swebench_lite.py); this is its command
line. The official harness owns the environment, patch application and grading;
this script never applies a patch or runs a repository's tests itself. See the
evaluation guide: https://www.swebench.com/SWE-bench/guides/evaluation/

Usage::

    # Offline: verify the pinned manifest.
    uv run python evaluation/runners/run_swebench_lite.py --check

    # Offline: adapt real diffs (one <instance_id>.patch each) to predictions.
    uv run python evaluation/runners/run_swebench_lite.py --diffs DIR --predictions out.jsonl

    # Real scoring: requires the official harness plus an explicit budget.
    uv run python evaluation/runners/run_swebench_lite.py --live \
        --predictions out.jsonl --model <id> --max-total-tokens <n> \
        --run-id <id> --official-harness
"""

import argparse
import subprocess
import sys
from pathlib import Path

if not __package__:
    # Support the documented file invocation as well as package imports.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evaluation.benchmarks import swebench_lite as lite

LIVE_NOT_RUN = "LIVE_NOT_RUN"
LIVE_BLOCKED = "LIVE_BLOCKED"


def _predict_from_diffs(diffs: Path, model: str) -> list[lite.Prediction]:
    predictions: list[lite.Prediction] = []
    for item in lite.SUBSET:
        patch_path = diffs / f"{item.instance_id}.patch"
        patch = patch_path.read_text(encoding="utf-8") if patch_path.is_file() else ""
        predictions.append(lite.prediction_for_diff(item.instance_id, model, patch))
    return predictions


def _official_harness_available() -> bool:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "swebench.harness.run_evaluation", "--help"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def main(args: argparse.Namespace) -> int:
    if args.check:
        print(f"subset: {lite.SUBSET_VERSION} ({len(lite.SUBSET)} instances)")
        print(f"digest: {lite.subset_digest()}")
        print("This is an internal subset, not the full SWE-bench Lite leaderboard.")
        for item in lite.SUBSET:
            version = item.version if item.version is not None else "unknown"
            print(f"  {item.instance_id:<32} {item.repo:<20} v{version}")
        return 0

    if args.predictions is None:
        print("--predictions is required for adapter or live mode.", file=sys.stderr)
        return 2

    if args.diffs is not None:
        model = args.model or "cairn-unspecified-model"
        predictions = _predict_from_diffs(args.diffs, model)
        lite.write_predictions(predictions, args.predictions)
        print(f"wrote {len(predictions)} predictions to {args.predictions}")
        return 0

    if not args.live:
        print(
            f"{LIVE_NOT_RUN}: no --diffs given and --live not set; nothing scored.",
            file=sys.stderr,
        )
        return 0

    missing: list[str] = []
    if not args.official_harness:
        missing.append("--official-harness")
    if not args.model:
        missing.append("--model")
    if not args.max_total_tokens:
        missing.append("--max-total-tokens")
    if not args.run_id:
        missing.append("--run-id")
    if missing:
        print(f"{LIVE_BLOCKED}: missing {', '.join(missing)}", file=sys.stderr)
        return 2
    if not _official_harness_available():
        print(
            f"{LIVE_BLOCKED}: the official SWE-bench harness is not installed; "
            "install `swebench` and a Docker daemon to score. "
            "No patches were applied and no tests were run.",
            file=sys.stderr,
        )
        return 2
    # Delegate scoring to the official harness; never re-implement it here.
    print(
        "Run the official harness with:\n"
        "  python -m swebench.harness.run_evaluation "
        "--dataset_name princeton-nlp/SWE-bench_Lite "
        f"--predictions_path {args.predictions} --run_id {args.run_id} "
        f"--instance_ids {' '.join(lite.instance_ids())}\n"
        f"Budget: token cap {args.max_total_tokens}, model {args.model}.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify the manifest.")
    parser.add_argument("--diffs", type=Path, default=None)
    parser.add_argument("--predictions", type=Path, default=None)
    parser.add_argument("--model", type=str, default=None)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--official-harness", action="store_true")
    parser.add_argument("--max-total-tokens", type=int, default=None)
    parser.add_argument("--run-id", type=str, default=None)
    raise SystemExit(main(parser.parse_args()))
