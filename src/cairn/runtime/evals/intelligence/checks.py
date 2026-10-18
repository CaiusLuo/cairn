"""Host-owned behavior checks; scripts/answers never enter Agent context."""

import platform
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

from cairn.infra.tools.bash import BashTool
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.evals.models import CheckResult

# A behavior script runs inside the Bash sandbox, which exposes only the
# platform's system directories. The running interpreter may live in a virtual
# environment outside those directories, so it is not reachable there: Linux
# mounts /usr, /bin and friends read-only and nothing else, and macOS permits
# the checkout interpreter specifically. Name the interpreter that the sandbox
# actually has rather than assuming a `python` entry point exists — Debian 12
# ships only `python3` (PEP 394), so a bare `python` fails every check there.
_SANDBOX_INTERPRETERS = {
    "linux": ("/usr/bin/python3", "/usr/bin/python"),
    "darwin": (sys.executable,),
}


def _sandbox_python() -> str:
    """The interpreter path a behavior script can use inside the sandbox."""
    candidates = _SANDBOX_INTERPRETERS.get(platform.system().lower(), ())
    for candidate in (*candidates, sys.executable):
        if candidate and Path(candidate).is_file():
            return candidate
    # BashTool will report the platform's own failure; this only stops the
    # check from silently running against a name that does not resolve.
    raise RuntimeError(
        f"no Python interpreter is available to behavior checks on {platform.system()}"
    )


@dataclass(slots=True)
class BehaviorCheck:
    name: str
    script: str
    secret_env_keys: frozenset[str] = frozenset()

    async def evaluate(self, workspace: Workspace) -> CheckResult:
        if len(self.script.encode()) > 8192:
            raise ValueError("Benchmark check script limit exceeded")
        command = f"{shlex.quote(_sandbox_python())} -B -c {shlex.quote(self.script)}"
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
