"""Offline SessionStore scaling probe; run with uv run python -m evaluation.benchmarks.session_checkpoints.

One process, deterministic four-message Turns (one fenced read), identical sizes,
real SQLite FULL/WAL transactions. No provider, tool or Git execution. Timings
are wall-clock samples; profiler totals are inclusive and overlap. Cold means a
fresh Store/SQLite connection, not a flushed operating-system page cache.
"""

import argparse
import cProfile
import json
import platform
import pstats
import sqlite3
import statistics
import tempfile
import time
import tracemalloc
from importlib.metadata import version
from pathlib import Path
from typing import Any
from uuid import UUID

from tests.support.sessions import T4, binding, fence, intent, messages, settled, store

from cairn.core.models import Message


def stats(values: list[float]) -> dict[str, float]:
    return {"median": statistics.median(values), "min": min(values), "max": max(values)}


def measure(call: Any, repeats: int) -> dict[str, float]:
    samples = []
    for _ in range(repeats):
        start = time.perf_counter_ns()
        call()
        samples.append((time.perf_counter_ns() - start) / 1e6)
    return stats(samples)


def profile(call: Any) -> dict[str, Any]:
    probe = cProfile.Profile()
    probe.runcall(call)
    result = {}
    for (path, _line, name), (
        _primitive,
        count,
        own,
        cumulative,
        _callers,
    ) in pstats.Stats(probe).stats.items():
        if name in {
            "_report",
            "decode_checkpoint",
            "decode_storage_checkpoint",
            "encode_storage_checkpoint",
            "validate_history",
            "_settled_relationship",
            "_schema",
            "_prepare",
            "_current",
            "snapshot_messages",
            "encode_checkpoint",
            "encode_record",
            "_commit",
        }:
            result[f"{Path(path).name}:{name}"] = {
                "calls": count,
                "self_ms": own * 1000,
                "cumulative_ms": cumulative * 1000,
            }
    return result


def run(turns: int, repeats: int, *, legacy: bool = False) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="cairn-checkpoints-") as directory:
        root = Path(directory)
        if legacy:
            from tests.support.sessions import legacy_store

            api = legacy_store(root)
        else:
            api = store(root)
        schema_version = api.schema_version
        bind = binding(root)
        receipt = api.create(bind)
        history = []
        observations: dict[str, list[float]] = {
            key: []
            for key in (
                "begin_ms",
                "fence_ms",
                "snapshot_ms",
                "commit_ms",
                "sqlite_commit_ms",
            )
        }
        wal_peak = 0
        original_commit = api._commit

        def sql_commit(conn: sqlite3.Connection) -> None:
            nonlocal wal_peak
            start = time.perf_counter_ns()
            original_commit(conn)
            observations["sqlite_commit_ms"].append(
                (time.perf_counter_ns() - start) / 1e6
            )
            wal_peak = max(wal_peak, Path(str(api.path) + "-wal").stat().st_size)

        api._commit = sql_commit  # type: ignore[method-assign]
        for number in range(turns + repeats):
            turn = intent(
                bind,
                receipt.revision,
                turn_id=str(UUID(int=number + 1, version=4)),
                start=len(history),
                time=T4,
            )
            start = time.perf_counter_ns()
            active = api.begin_turn(receipt.revision, turn)
            begin_ms = (time.perf_counter_ns() - start) / 1e6
            start = time.perf_counter_ns()
            active = api.fence_tool(
                active,
                fence(bind, turn_id=turn.turn_id, index=len(history) + 1).model_copy(
                    update={"observed_at_utc": T4}
                ),
            )
            fence_ms = (time.perf_counter_ns() - start) / 1e6
            wal_peak = max(wal_peak, Path(str(api.path) + "-wal").stat().st_size)
            tail = messages()
            tail[2].content = "r" * 1024
            tail[3].content = "a" * 1024
            history.extend(tail)
            start = time.perf_counter_ns()
            value = settled(turn, history=history)
            snapshot_ms = (time.perf_counter_ns() - start) / 1e6
            start = time.perf_counter_ns()
            receipt = api.commit_turn(active, value)
            commit_ms = (time.perf_counter_ns() - start) / 1e6
            bind = value.checkpoint.binding
            if number >= turns:
                for key, value_ms in (
                    ("begin_ms", begin_ms),
                    ("fence_ms", fence_ms),
                    ("snapshot_ms", snapshot_ms),
                    ("commit_ms", commit_ms),
                ):
                    observations[key].append(value_ms)
            if number + 1 == turns:
                inspect_ms = measure(
                    lambda api=api, bind=bind: api.inspect(bind.session_id), repeats
                )
                report_profile = profile(
                    lambda api=api, bind=bind: api.inspect(bind.session_id)
                )
                tracemalloc.start()
                api.inspect(bind.session_id)
                _current, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                with sqlite3.connect(api.path) as conn:
                    payload_bytes = conn.execute(
                        "SELECT sum(length(payload)) FROM checkpoints"
                    ).fetchone()[0]
                db_bytes = api.path.stat().st_size
                live_wal_bytes = Path(str(api.path) + "-wal").stat().st_size

                # Reopen read-only at exactly N Turns while writer is idle.
                def cold(session_id: str = bind.session_id) -> None:
                    reader = store(root, mode="inspect")
                    try:
                        reader.inspect(session_id)
                    finally:
                        reader.close()

                live_reopen_ms = measure(cold, repeats)
                api.close()
                cold_ms = measure(cold, repeats)
                api = store(root)
                original_commit = api._commit
                api._commit = sql_commit  # type: ignore[method-assign]

        # Profile a full next begin/fence/commit, separate from unprofiled samples.
        def write_cycle() -> None:
            nonlocal receipt, bind
            turn = intent(
                bind,
                receipt.revision,
                turn_id=str(UUID(int=9999, version=4)),
                start=len(history),
                time=T4,
            )
            active = api.begin_turn(receipt.revision, turn)
            active = api.fence_tool(
                active,
                fence(bind, turn_id=turn.turn_id, index=len(history) + 1).model_copy(
                    update={"observed_at_utc": T4}
                ),
            )
            history.extend(messages())
            value = settled(turn, history=history)
            receipt = api.commit_turn(active, value)
            bind = value.checkpoint.binding

        steady = {
            k: stats(v[-3 * repeats :] if k == "sqlite_commit_ms" else v)
            for k, v in observations.items()
        }
        # Tool-free commits on N+8..N+14; the same window in before/after runs.
        ordinary: dict[str, list[float]] = {
            key: [] for key in ("begin_ms", "snapshot_ms", "commit_ms")
        }
        for number in range(repeats):
            turn = intent(
                bind,
                receipt.revision,
                turn_id=str(UUID(int=5000 + number, version=4)),
                start=len(history),
                time=T4,
            )
            start = time.perf_counter_ns()
            active = api.begin_turn(receipt.revision, turn)
            ordinary["begin_ms"].append((time.perf_counter_ns() - start) / 1e6)
            history.extend(
                [
                    Message(role="user", content="hello"),
                    Message(role="assistant", content="a" * 1024),
                ]
            )
            start = time.perf_counter_ns()
            value = settled(turn, history=history)
            ordinary["snapshot_ms"].append((time.perf_counter_ns() - start) / 1e6)
            start = time.perf_counter_ns()
            receipt = api.commit_turn(active, value)
            ordinary["commit_ms"].append((time.perf_counter_ns() - start) / 1e6)
            bind = value.checkpoint.binding
        measured_wal_peak = wal_peak
        write_profile = profile(write_cycle)
        api.close()
        return {
            "turns": turns,
            "schema_version": schema_version,
            "ordinary": {key: stats(values) for key, values in ordinary.items()},
            "inspect_ms": inspect_ms,
            "cold_reopen_ms": cold_ms,
            "live_reopen_ms": live_reopen_ms,
            "steady": steady,
            "payload_bytes": payload_bytes,
            "db_bytes": db_bytes,
            "live_wal_bytes": live_wal_bytes,
            "peak_wal_at_commit_bytes": measured_wal_peak,
            "inspect_peak_tracemalloc_bytes": peak,
            "inspect_profile": report_profile,
            "write_profile": write_profile,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument(
        "--legacy", action="store_true", help="Use schema 2 on the new implementation"
    )
    parser.add_argument("--turns", type=int, nargs="+", default=[5, 10, 20, 40, 80])
    args = parser.parse_args()
    result = {
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "sqlite": sqlite3.sqlite_version,
            "pydantic": version("pydantic"),
            "legacy": args.legacy,
            "repeats": args.repeats,
            "tool_result_bytes": 1024,
            "assistant_bytes": 1024,
        },
        "measurements": [],
    }
    for count in args.turns:
        record = run(count, args.repeats, legacy=args.legacy)
        result["measurements"].append(record)
        print(count, record["inspect_ms"], flush=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
