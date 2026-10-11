"""Host-owned behavior checks; scripts/answers never enter Agent context."""

import shlex
from dataclasses import dataclass

from cairn.evals.models import CheckResult
from cairn.tools.bash import BashTool
from cairn.workspace.workspace import Workspace


@dataclass(slots=True)
class BehaviorCheck:
    name: str
    script: str
    secret_env_keys: frozenset[str] = frozenset()

    async def evaluate(self, workspace: Workspace) -> CheckResult:
        if len(self.script.encode()) > 8192:
            raise ValueError("Benchmark check script limit exceeded")
        result = await BashTool(
            workspace,
            timeout=5,
            stdout_limit=1024,
            stderr_limit=1024,
            secret_env_keys=self.secret_env_keys,
        ).execute({"command": "python -B -c " + shlex.quote(self.script)})
        return CheckResult(
            name=self.name,
            passed=result.exit_code == 0,
            message=None if result.exit_code == 0 else "Behavior check failed.",
        )
