"""Solve stage: run the real agent loop on a task, collect an applicable patch."""

import asyncio
import subprocess
from pathlib import Path

from evaluation.benchmarks.swebench_lite_solve import SolveTask, solve_task

from cairn.core.budget import RunBudget
from cairn.core.models import LLMResponse, ToolCall
from cairn.core.permissions import PermissionChoice, SessionPermissionHandler
from tests.support.runtime import SequenceLLM


def _handler() -> SessionPermissionHandler:
    return SessionPermissionHandler(lambda request: PermissionChoice.ALLOW_SESSION)


def _edit_then_finish(old: str, new: str) -> list[LLMResponse]:
    return [
        LLMResponse(
            tool_calls=[
                ToolCall(
                    id="edit-1",
                    name="edit_file",
                    arguments={"path": "src/mod.py", "old_text": old, "new_text": new},
                )
            ]
        ),
        LLMResponse(content="done"),
    ]


def test_solve_produces_applicable_patch(tmp_path: Path) -> None:
    original = "def f():\n    return 1\n"
    fixed = "def f():\n    return 2\n"
    task = SolveTask(
        instance_id="demo__demo-1",
        problem_statement="Make f() return 2.",
        files={"src/mod.py": original},
    )
    prediction = asyncio.run(
        solve_task(
            task,
            llm_factory=lambda: SequenceLLM(_edit_then_finish(original, fixed)),
            budget=RunBudget(max_steps=4),
            run_timeout_seconds=20,
            permission_handler=_handler(),
        )
    )
    assert prediction["instance_id"] == "demo__demo-1"
    patch = prediction["model_patch"]
    assert "-    return 1" in patch and "+    return 2" in patch

    # Apply the collected patch to a fresh checkout and verify the behavior.
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "mod.py").write_text(original)
    for args in (
        ("init", "-q"),
        ("add", "-A"),
        ("-c", "user.email=a@b.c", "-c", "user.name=x", "commit", "-q", "-m", "b"),
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    patch_file = tmp_path / "p.diff"
    patch_file.write_text(patch)
    applied = subprocess.run(
        ["git", "-C", str(repo), "apply", str(patch_file)],
        capture_output=True,
        text=True,
    )
    assert applied.returncode == 0, applied.stderr
    ns: dict[str, object] = {}
    exec((repo / "src" / "mod.py").read_text(), ns)
    assert ns["f"]() == 2  # type: ignore[operator]


def test_solve_without_edit_is_empty_patch(tmp_path: Path) -> None:
    task = SolveTask(
        instance_id="demo__demo-2",
        problem_statement="Do nothing.",
        files={"src/mod.py": "x = 1\n"},
    )
    prediction = asyncio.run(
        solve_task(
            task,
            llm_factory=lambda: SequenceLLM([LLMResponse(content="no change")]),
            budget=RunBudget(max_steps=3),
            run_timeout_seconds=20,
            permission_handler=_handler(),
        )
    )
    assert prediction["model_patch"] == ""


def test_solve_cli_preflight_requires_tasks(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    import argparse

    from evaluation.runners import run_swebench_lite as script

    args = argparse.Namespace(
        check=False,
        diffs=None,
        predictions=tmp_path / "p.jsonl",
        print_command=False,
        grade=False,
        solve=True,
        live=False,
        official_harness=False,
        model="m",
        max_total_tokens=1000,
        run_id="r1",
        isolated=False,
        tasks=None,
        max_steps=10,
        solve_timeout_seconds=30.0,
    )
    assert script.main(args) == 2
    out = capsys.readouterr().out
    assert "RESULT" in out and "LIVE_BLOCKED" in out
