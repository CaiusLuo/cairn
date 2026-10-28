"""Thin Cairn solve → patch adapter for the SWE-bench subset.

This is the *solve* stage of the closed loop: run the normal Cairn agent
(``runtime.loop.run_turn`` — the single lifecycle owner) against a task's
problem statement in a disposable git checkout, then collect the working-tree
diff as the official ``model_patch``. It reuses the configured model client and
``RunBudget``; it does not add a second agent loop.

It is deliberately small and honest about boundaries. Running the agent over an
*untrusted* repository with shell/file tools is a trust decision (the same one
SWE-bench answers with per-instance containers). This module therefore takes the
workspace fixtures and a caller-supplied permission handler; the operational
runner only grants shell when explicitly authorized, and the container/VM
isolation of the solve stage is handed back as a decision point rather than
silently expanded here.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from cairn.core.budget import RunBudget
from cairn.core.llm import LLMClient
from cairn.core.permissions import PermissionHandler
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.assembly import build_agent
from cairn.runtime.loop import run_turn


@dataclass(frozen=True, slots=True)
class SolveTask:
    """One task to solve: its instance id, problem statement and repo fixtures."""

    instance_id: str
    problem_statement: str
    files: dict[str, str] = field(default_factory=dict)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def collect_patch(workspace: Workspace) -> str:
    """Return the unified diff of the working tree against the base commit.

    Untracked files are included (``git add -A`` then ``--cached``), so a patch
    that creates a file is captured too. An unmodified checkout yields "".
    """
    root = workspace.root
    _git(root, "add", "-A")
    result = _git(root, "diff", "--cached", "--no-color")
    return result.stdout if result.returncode == 0 else ""


async def solve_task(
    task: SolveTask,
    *,
    llm_factory: Callable[[], LLMClient],
    budget: RunBudget,
    run_timeout_seconds: float = 300.0,
    temp_root: Path | None = None,
    permission_handler: PermissionHandler | None = None,
) -> dict[str, str]:
    """Run the agent on one task in a disposable repo and return a prediction.

    The returned mapping is the official prediction shape (``instance_id``,
    ``model_name_or_path``, ``model_patch``). An agent that makes no change
    yields an empty patch — a legal, empty prediction.
    """
    with TemporaryDirectory(prefix="cairn-solve-", dir=temp_root) as tmp:
        root = Path(tmp)
        workspace = Workspace(root)
        for raw_path, text in task.files.items():
            path = workspace.resolve_path(raw_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        _git(root, "init", "-q", "-b", "solve")
        _git(
            root,
            "-c",
            "user.email=solve@cairn.invalid",
            "-c",
            "user.name=cairn",
            "add",
            "-A",
        )
        _git(
            root,
            "-c",
            "user.email=solve@cairn.invalid",
            "-c",
            "user.name=cairn",
            "commit",
            "-q",
            "-m",
            "base",
        )

        llm = llm_factory()
        model = getattr(llm, "model", None)
        agent = build_agent(
            workspace=workspace,
            llm=llm,
            permission_handler=permission_handler,
            event_handler=None,
            tracer=None,
        )
        async with asyncio.timeout(run_timeout_seconds):
            await run_turn(agent, task.problem_statement, budget=budget)
        patch = collect_patch(workspace)
        return {
            "instance_id": task.instance_id,
            "model_name_or_path": model if isinstance(model, str) else "cairn",
            "model_patch": patch,
        }
