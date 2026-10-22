"""Per-stage timing for the fence append path, V3 versus V4.

The barriers nest: `fence_total` contains `write_txn`, which contains `schema`
and `commit`. Values are reported side by side, never summed. Run against the
schema this build writes; use a checkout of the previous commit for V3.
"""

from __future__ import annotations

import contextlib
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.support.sessions import binding, fence, intent, store

from cairn.infra.sessions.models import SCHEMA_VERSION

COUNTS = (16, 32, 64, 128, 256)
REPEATS = 5


def run_once(root: Path, count: int) -> dict[str, float]:
    directory = root / f"{count}-{time.perf_counter_ns()}"
    directory.mkdir(parents=True)
    api = store(directory)
    stages = {
        "prepare": 0.0,
        "codec": 0.0,
        "validate": 0.0,
        "write": 0.0,
        "commit": 0.0,
    }
    try:
        bind = binding(directory)
        revision = api.create(bind).revision
        turn = intent(bind, revision)
        revision = api.begin_turn(revision, turn)
        original_write, original_commit = api._write, api._commit
        original_validate, original_encode = api._validate_record, api._encode
        original_prepare = api._prepare

        @contextlib.contextmanager
        def timed_write(*args, **kwargs):
            started = time.perf_counter()
            with original_write(*args, **kwargs) as conn:
                yield conn
            stages["write"] += time.perf_counter() - started

        @contextlib.contextmanager
        def timed_prepare(*args, **kwargs):
            started = time.perf_counter()
            with original_prepare(*args, **kwargs) as report:
                yield report
            stages["prepare"] += time.perf_counter() - started

        def timed_commit(*args, **kwargs):
            started = time.perf_counter()
            try:
                return original_commit(*args, **kwargs)
            finally:
                stages["commit"] += time.perf_counter() - started

        def timed_validate(record):
            started = time.perf_counter()
            try:
                return original_validate(record)
            finally:
                stages["validate"] += time.perf_counter() - started

        def timed_encode(*args, **kwargs):
            started = time.perf_counter()
            try:
                return original_encode(*args, **kwargs)
            finally:
                stages["codec"] += time.perf_counter() - started

        api._write = timed_write
        api._prepare = timed_prepare
        api._commit = timed_commit
        api._validate_record = timed_validate
        api._encode = timed_encode
        started = time.perf_counter()
        for ordinal in range(1, count + 1):
            revision = api.fence_tool(
                revision, fence(bind, ordinal=ordinal, call_id=f"c{ordinal}")
            )
        total = time.perf_counter() - started
    finally:
        with contextlib.suppress(Exception):
            api.close()
    return {
        "fences": count,
        "total_s": total,
        "prepare_s": stages["prepare"],
        "codec_s": stages["codec"],
        "validate_s": stages["validate"],
        "write_s": stages["write"],
        "commit_s": stages["commit"],
    }


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="cairn-stages-"))
    rows = []
    for count in COUNTS:
        samples = [run_once(root, count) for _ in range(REPEATS)]
        rows.append(
            {
                key: (
                    count
                    if key == "fences"
                    else round(statistics.median(sample[key] for sample in samples), 4)
                )
                for key in samples[0]
            }
            | {
                "total_max_s": round(max(sample["total_s"] for sample in samples), 4),
                "total_min_s": round(min(sample["total_s"] for sample in samples), 4),
            }
        )
    print(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "repeats": REPEATS,
                "note": "intervals nest: prepare/write/commit/validate/codec are not additive",
                "rows": rows,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
