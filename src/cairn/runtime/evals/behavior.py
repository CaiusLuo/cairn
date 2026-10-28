"""Independent, versioned behavior checks for eval cases.

A ``BehaviorCheck`` executes host-authored Python against the case workspace and
turns the process exit code into a pass/fail verdict. It is the reusable, generic
counterpart to the repository-intelligence ``intelligence.checks.BehaviorCheck``:
the intelligence dataset pins its own copy inside a frozen, digest-checked
contract, so this module deliberately keeps a separate implementation.

A behavior check scores *observable behavior*: the script imports the code under
test, exercises public functions and asserts outputs, boundaries and regressions
to already-working behaviour. It therefore accepts any correct implementation,
not just the bytes of one reference patch, and refuses a near-miss that reproduces
the original defect or breaks an untouched guarantee.

Execution isolation
-------------------
The script is host-owned trusted code, but it *imports the code the evaluated
model wrote*, so that generated code is untrusted. This check therefore never
runs it in a plain subprocess. It executes inside a bubblewrap sandbox
([`cairn.infra.sandbox`](../../infra/sandbox.py)): read-only system mounts, the
workspace bound read-write at its own path, a private ``/tmp``, no network, a
cleared environment and in-process resource limits. When no sandbox is available
the check fails explicitly (``error`` set, so the case is an ERROR) and never
falls back to the host. A temporary directory plus a timeout is not a sandbox.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from cairn.infra.sandbox import IsolatedRunner, IsolationLimits, IsolationUnavailable
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.evals.models import CheckResult

#: A behavior script is small host code, not a program. Bound it so a runaway
#: case fixture cannot smuggle a large payload through a "check".
MAX_SCRIPT_BYTES = 32 * 1024
#: Bound a single behavior execution; the interpreter runs inside the sandbox.
BEHAVIOR_TIMEOUT_SECONDS = 10.0


@dataclass(slots=True)
class BehaviorCheck:
    """Run a host-owned script against the workspace, isolated, and score it."""

    name: str
    script: str
    #: Kept for API compatibility; the sandbox clears the whole environment, so
    #: declared provider secrets can never reach the generated code either way.
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
        runner = IsolatedRunner(
            IsolationLimits(timeout_seconds=BEHAVIOR_TIMEOUT_SECONDS)
        )
        try:
            result = await runner.run_script(workspace, self.script)
        except IsolationUnavailable as exc:
            # Never fall back to the host: an unisolated run could read secrets,
            # write host files or tamper with verification materials.
            return CheckResult(
                name=self.name,
                passed=False,
                error=f"IsolationUnavailable: {exc}",
            )
        if result.exit_code == 0:
            return CheckResult(name=self.name, passed=True, message=None)
        # Bounded, last-line diagnostic so a missing interpreter or a traceback
        # does not read exactly like a genuine behavioral failure.
        detail = (result.stderr or result.stdout).strip().splitlines()
        summary = detail[-1][:200] if detail else f"exit {result.exit_code}"
        return CheckResult(
            name=self.name,
            passed=False,
            message=f"Behavior check failed: {summary}",
        )
