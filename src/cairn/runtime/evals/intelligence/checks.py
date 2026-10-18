"""Host-owned behavior checks; scripts/answers never enter Agent context."""

import shlex
from dataclasses import dataclass

from cairn.infra.tools.bash import BashTool, sandbox_python
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.evals.models import CheckResult


@dataclass(slots=True)
class BehaviorCheck:
    name: str
    script: str
    secret_env_keys: frozenset[str] = frozenset()

    async def evaluate(self, workspace: Workspace) -> CheckResult:
        if len(self.script.encode()) > 8192:
            raise ValueError("Benchmark check script limit exceeded")
        # Prefer the workspace's own environment, then the running interpreter.
        # Never a bare `python`: Debian and Ubuntu ship only `python3` (PEP 394).
        interpreter = sandbox_python(workspace.root)
        if not interpreter:
            return CheckResult(
                name=self.name,
                passed=False,
                message="Behavior check failed: no Python interpreter available.",
            )
        command = f"{shlex.quote(interpreter)} -B -c {shlex.quote(self.script)}"
        result = await BashTool(
            workspace,
            timeout=5,
            stdout_limit=1024,
            stderr_limit=1024,
            secret_env_keys=self.secret_env_keys,
        ).execute({"command": command})
        if result.exit_code == 0:
            return CheckResult(name=self.name, passed=True, message=None)
        # Keep a bounded diagnostic: without it a missing interpreter reads
        # exactly like a genuine behavioral failure.
        detail = (result.stderr or result.stdout).strip().splitlines()
        summary = detail[-1][:200] if detail else f"exit {result.exit_code}"
        return CheckResult(
            name=self.name,
            passed=False,
            message=f"Behavior check failed: {summary}",
        )
