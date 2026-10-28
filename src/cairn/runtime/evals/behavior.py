"""Independent, versioned behavior checks for eval cases.

A ``BehaviorCheck`` executes host-authored Python inside the case workspace and
turns the process exit code into a pass/fail verdict. It is the reusable,
generic counterpart to the repository-intelligence
``intelligence.checks.BehaviorCheck``: the intelligence dataset pins its own
copy inside a frozen, digest-checked contract, so this module deliberately keeps
a separate implementation rather than importing that one. Both run the exact
same Bash/environment contract (workspace interpreter, no ``python`` alias,
bounded output, sanitized secret variables); sharing the runner would couple two
independently versioned datasets.

A behavior check scores *observable behavior*: the script imports the code under
test, exercises public functions and asserts outputs, boundaries and regressions
to already-working behaviour. It therefore accepts any correct implementation,
not just the exact bytes of one reference patch, and it refuses a near-miss that
reproduces the original defect or breaks an untouched guarantee.

Scope and honesty about it
--------------------------
The script runs with the workspace's interpreter through the real ``BashTool``.
That provides a fresh temporary directory, the declared provider-secret variables
stripped, and a wall-clock timeout — but it is *not* an OS sandbox. A behavior
script is host-owned trusted code: it is never model context, never written by the
evaluated model, and never persisted into the case. Execution isolation for the
*generated code under test* is the caller's responsibility (the case workspace
lives in a throwaway temporary directory); a timeout and a temporary directory are
not a sandbox.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import ClassVar

from cairn.infra.tools.bash import BashTool, workspace_python
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.evals.models import CheckResult

#: A behavior script is small host code, not a program. Bound it so a runaway
#: case fixture cannot smuggle a large payload through a "check".
MAX_SCRIPT_BYTES = 32 * 1024
#: Bound a single behavior execution; the interpreter is the only process.
BEHAVIOR_TIMEOUT_SECONDS = 10


@dataclass(slots=True)
class BehaviorCheck:
    """Run a host-owned script against the workspace and score its behavior."""

    name: str
    script: str
    secret_env_keys: frozenset[str] = frozenset()
    #: Stable label so reporting is independent of the concrete class name.
    check_type: ClassVar[str] = "behavior"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("behavior check name must be a non-empty string")
        if not isinstance(self.script, str) or not self.script.strip():
            raise ValueError("behavior check script must be a non-empty string")
        if len(self.script.encode("utf-8")) > MAX_SCRIPT_BYTES:
            raise ValueError("behavior check script limit exceeded")

    async def evaluate(self, workspace: Workspace) -> CheckResult:
        interpreter = workspace_python(workspace.root)
        if not interpreter:
            return CheckResult(
                name=self.name,
                passed=False,
                message="Behavior check failed: no Python interpreter available.",
            )
        command = f"{shlex.quote(interpreter)} -B -c {shlex.quote(self.script)}"
        result = await BashTool(
            workspace,
            timeout=BEHAVIOR_TIMEOUT_SECONDS,
            stdout_limit=1024,
            stderr_limit=1024,
            secret_env_keys=self.secret_env_keys,
        ).execute({"command": command})
        if result.exit_code == 0:
            return CheckResult(name=self.name, passed=True, message=None)
        # Keep a bounded, last-line diagnostic: a missing interpreter or an
        # escaping traceback must not read exactly like a genuine behavior fail.
        detail = (result.stderr or result.stdout).strip().splitlines()
        summary = detail[-1][:200] if detail else f"exit {result.exit_code}"
        return CheckResult(
            name=self.name,
            passed=False,
            message=f"Behavior check failed: {summary}",
        )
