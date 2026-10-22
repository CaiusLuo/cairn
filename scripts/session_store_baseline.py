"""Capture the V3 baseline this migration must preserve, and time it.

Run before any schema change and again after V4 lands; the two reports must agree
on every logical field. This is evidence, not a product feature, so it talks to
the real store through its public API and to SQLite directly for hashes.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.support.sessions import (
    SESSION,
    T4,
    binding,
    fence,
    intent,
    settled,
    store,
)

from cairn.infra.sessions.models import (
    AttemptDiagnostics,
    HostResolution,
    SessionStoreError,
    ToolFence,
)


def _session_row(path: Path, session_id: str) -> dict[str, Any]:
    with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        return {} if row is None else dict(row)


def _table_digest(path: Path, table: str) -> str:
    with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as conn:
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
    return hashlib.sha256(repr(rows).encode()).hexdigest()


def _close_quietly(api: Any) -> None:
    """Close a store whose Turn may still be owned by this script.

    The baseline deliberately leaves Turns unresolved (executing, recovery
    required); `close()` refusing an owned Turn is expected, not a failure of the
    scenario.
    """
    with contextlib.suppress(SessionStoreError):
        api.close()


def _fence_tuple(fence_value: ToolFence) -> tuple[Any, ...]:
    return (
        fence_value.session_id,
        fence_value.turn_id,
        fence_value.ordinal,
        fence_value.assistant_message_index,
        fence_value.call_id,
        fence_value.tool_name,
        fence_value.observed_at_utc,
    )


def _diagnostics_fences(
    path: Path, session_id: str, turn_id: str
) -> tuple[tuple[Any, ...], ...]:
    """The Turn's fences, from the table when it exists, else from the JSON."""
    with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        has_table = (
            conn.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='tool_fences'"
            ).fetchone()
            is not None
        )
        if has_table:
            rows = conn.execute(
                "SELECT * FROM tool_fences WHERE session_id=? AND turn_id=? ORDER BY ordinal",
                (session_id, turn_id),
            ).fetchall()
            return tuple(
                (
                    row["session_id"],
                    row["turn_id"],
                    row["ordinal"],
                    row["assistant_message_index"],
                    row["call_id"],
                    row["tool_name"],
                    row["observed_at_utc"],
                )
                for row in rows
            )
        row = conn.execute(
            "SELECT diagnostics_json FROM turns WHERE session_id=? AND turn_id=?",
            (session_id, turn_id),
        ).fetchone()
    if row is None:
        return ()
    return tuple(
        _fence_tuple(item)
        for item in AttemptDiagnostics.model_validate_json(
            row["diagnostics_json"]
        ).fences
    )


def scenario_completed(tmp: Path) -> dict[str, Any]:
    """A settled Turn with two fences."""
    api = store(tmp)
    try:
        bind = binding(tmp)
        revision = api.create(bind).revision
        turn = intent(bind, revision)
        revision = api.begin_turn(revision, turn)
        revision = api.fence_tool(revision, fence(bind))
        receipt = api.commit_turn(revision, settled(turn))
        report = api.inspect(SESSION)
        return {
            "kind": "completed",
            "revision": str(report.revision),
            "status": report.status,
            "receipt": receipt.model_dump(mode="json"),
            "fences": _diagnostics_fences(api.path, SESSION, turn.turn_id),
            "tables": {t: _table_digest(api.path, t) for t in ("turns", "checkpoints")},
        }
    finally:
        _close_quietly(api)


def _turn_row(path: Path, session_id: str) -> dict[str, Any]:
    with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM turns WHERE session_id=? ORDER BY rowid", (session_id,)
        ).fetchall()
    return dict(rows[0]) if rows else {}


def scenario_executing(tmp: Path) -> dict[str, Any]:
    """An executing Turn that was fenced but never settled."""
    api = store(tmp)
    try:
        bind = binding(tmp)
        revision = api.create(bind).revision
        turn = intent(bind, revision)
        revision = api.begin_turn(revision, turn)
        revision = api.fence_tool(revision, fence(bind, ordinal=1, call_id="c1"))
        report = api.inspect(SESSION)
        return {
            "kind": "executing",
            "revision": str(report.revision),
            "status": report.status,
            "fences": _diagnostics_fences(api.path, SESSION, turn.turn_id),
        }
    finally:
        _close_quietly(api)


def scenario_recovery(tmp: Path) -> dict[str, Any]:
    """A Turn marked recovery_required through the host path."""
    api = store(tmp)
    try:
        bind = binding(tmp)
        revision = api.create(bind).revision
        turn = intent(bind, revision)
        revision = api.begin_turn(revision, turn)
        revision = api.fence_tool(revision, fence(bind, ordinal=1, call_id="c1"))
        report = api.inspect(SESSION)
        marked = api.require_recovery(report.revision, "storage_failed")
        after = api.inspect(SESSION)
        return {
            "kind": "recovery_required",
            "revision": str(after.revision),
            "status": after.status,
            "fences": _diagnostics_fences(api.path, SESSION, turn.turn_id),
            "marked_version": marked.version,
        }
    finally:
        _close_quietly(api)


def scenario_abandoned(tmp: Path) -> dict[str, Any]:
    """A fenced Turn abandoned by the host."""
    api = store(tmp)
    try:
        bind = binding(tmp)
        revision = api.create(bind).revision
        turn = intent(bind, revision)
        revision = api.begin_turn(revision, turn)
        revision = api.fence_tool(revision, fence(bind, ordinal=1, call_id="c1"))
        # Simulate the owner dying: drop local ownership, then reopen.
        api._owned_turns.clear()
        api.close()
        api = store(tmp)
        report = api.inspect(SESSION)
        api.require_recovery(report.revision, "storage_failed")
        report = api.inspect(SESSION)
        resolution = HostResolution(
            session_id=SESSION,
            turn_id=turn.turn_id,
            actor="baseline",
            resolved_at_utc=T4,
            binding=bind,
            note="baseline abandon",
        )
        api.reconcile(report.revision, resolution)
        after = api.inspect(SESSION)
        return {
            "kind": "abandoned",
            "revision": str(after.revision),
            "status": after.status,
        }
    finally:
        _close_quietly(api)


def scenario_no_fence(tmp: Path) -> dict[str, Any]:
    api = store(tmp)
    try:
        bind = binding(tmp)
        revision = api.create(bind).revision
        turn = intent(bind, revision)
        revision = api.begin_turn(revision, turn)
        receipt = api.commit_turn(revision, settled(turn))
        report = api.inspect(SESSION)
        return {
            "kind": "no_fence",
            "revision": str(report.revision),
            "status": report.status,
            "receipt": receipt.model_dump(mode="json"),
        }
    finally:
        _close_quietly(api)


def measure(path_root: Path, counts: tuple[int, ...]) -> list[dict[str, float]]:
    """Time the full production append path for each fence count.

    Only the outer interval is reported: the per-stage intervals below nest
    inside it and inside each other, so their values must not be summed.
    """
    results: list[dict[str, float]] = []
    for count in counts:
        samples: list[float] = []
        for _ in range(3):
            tmp = path_root / f"bench-{count}-{time.perf_counter_ns()}"
            tmp.mkdir(parents=True)
            api = store(tmp)
            try:
                bind = binding(tmp)
                revision = api.create(bind).revision
                turn = intent(bind, revision)
                revision = api.begin_turn(revision, turn)
                started = time.perf_counter()
                for ordinal in range(1, count + 1):
                    revision = api.fence_tool(
                        revision, fence(bind, ordinal=ordinal, call_id=f"c{ordinal}")
                    )
                samples.append(time.perf_counter() - started)
            finally:
                # The Turn is intentionally still owned: this measures the append
                # path only, and close() refusing an owned Turn is expected.
                with contextlib.suppress(SessionStoreError):
                    api.close()
        results.append(
            {
                "fences": count,
                "median_s": statistics.median(samples),
                "min_s": min(samples),
                "max_s": max(samples),
                "median_ms_per_fence": statistics.median(samples) / count * 1000,
            }
        )
    return results


def main() -> int:
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="cairn-baseline-"))
    report: dict[str, Any] = {"store_path": str(root)}
    for name, fn in (
        ("completed", scenario_completed),
        ("no_fence", scenario_no_fence),
        ("executing", scenario_executing),
        ("recovery", scenario_recovery),
        ("abandoned", scenario_abandoned),
    ):
        directory = root / name
        directory.mkdir()
        try:
            report[name] = fn(directory)
        except SessionStoreError as error:  # pragma: no cover - diagnostic only
            report[name] = {"kind": name, "error": error.code}
    bench_root = root / "bench"
    bench_root.mkdir()
    report["performance"] = measure(bench_root, (16, 32, 64, 128, 256))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
