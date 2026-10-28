"""Independent, versioned behavior checks with a trusted host-side verdict.

A ``BehaviorCheck`` runs a *trusted probe program* against the case workspace
inside the bubblewrap sandbox and turns the **data it observes** into a pass/fail
verdict **on the host**. The evaluated model's code is untrusted, so the verdict
must not depend on anything the candidate can fabricate:

* the probe program records raw observed values with ``obs(name, value)`` and
  writes them to a bounded JSON record; it never decides pass/fail;
* the *expected* values live only on the host, never inside the sandbox, so a
  candidate cannot read the answer key;
* the host compares the returned observations to the expectations and only then
  marks PASS.

Consequences (the point of the redesign): a candidate that calls ``os._exit(0)``,
``sys.exit(0)``, prints ``SUCCESS`` or otherwise does not complete produces **no
complete observation record** and therefore FAILS. Exit status is never used as a
pass signal. A probe program that raises is recorded as an incomplete/error probe
and fails; only a full set of in-range observations can pass.

Execution isolation
-------------------
The probe runs inside the bubblewrap sandbox ([`cairn.infra.sandbox`]): read-only
code/library mounts, the workspace bound read-write, a private ``/tmp``, no
network, a cleared environment and in-process resource limits. When no sandbox is
available the check fails explicitly (``error`` set) and never runs generated code
on the host.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, ClassVar

from cairn.infra.sandbox import IsolatedRunner, IsolationLimits, IsolationUnavailable
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.evals.models import CheckResult

#: A probe program is small host code, not a program. Bound it.
MAX_PROGRAM_BYTES = 32 * 1024
#: Bound a single behavior execution; the interpreter runs inside the sandbox.
BEHAVIOR_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class Contains:
    """Expected value is a string that must contain ``substring``."""

    substring: str


def _matches(actual: Any, expected: object) -> bool:
    if isinstance(expected, Contains):
        return isinstance(actual, str) and expected.substring in actual
    if isinstance(expected, bool) or isinstance(actual, bool):
        return actual is expected
    return bool(actual == expected)


def _harness(program: str, results_relpath: str) -> str:
    """Build the sandboxed probe harness (data collection only, no verdict).

    The harness never contains expected values; it defines ``obs`` and executes
    the trusted probe program, then writes the observations to a JSON file. A
    candidate that exits the process early bypasses the write, so the host sees a
    missing record and fails the check.
    """
    return (
        "import json, os, sys\n"
        "sys.path.insert(0, os.getcwd())\n"
        "sys.path.insert(0, os.path.join(os.getcwd(), 'src'))\n"
        "OBS = {}\n"
        "def _safe(v):\n"
        "    if v is None or isinstance(v, (bool, int, float, str)):\n"
        "        return v\n"
        "    if isinstance(v, (list, tuple)):\n"
        "        return [(_safe(x)) for x in v]\n"
        "    if isinstance(v, dict):\n"
        "        return {str(k): _safe(v[k]) for k in v}\n"
        "    return repr(v)\n"
        "def obs(name, value):\n"
        "    OBS[name] = _safe(value)\n"
        "_status = 'ok'\n"
        "try:\n"
        f"    exec(compile({program!r}, '<probe>', 'exec'), "
        "{'obs': obs, '__builtins__': __builtins__})\n"
        "except SystemExit:\n"
        "    _status = 'sys_exit'\n"
        "except BaseException as exc:\n"
        "    _status = 'error:' + type(exc).__name__ + ':' + str(exc)[:80]\n"
        "with open(" + repr(results_relpath) + ", 'w', encoding='utf-8') as fh:\n"
        "    json.dump({'status': _status, 'obs': OBS}, fh)\n"
    )


@dataclass(slots=True)
class BehaviorCheck:
    """Run a trusted probe program, then judge its observations on the host."""

    name: str
    program: str
    expected: dict[str, object]
    #: Kept for API compatibility; the sandbox clears the whole environment.
    secret_env_keys: frozenset[str] = frozenset()
    check_type: ClassVar[str] = "behavior"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("behavior check name must be a non-empty string")
        if not isinstance(self.program, str) or not self.program.strip():
            raise ValueError("behavior probe program must be a non-empty string")
        if len(self.program.encode("utf-8")) > MAX_PROGRAM_BYTES:
            raise ValueError("behavior probe program limit exceeded")
        if not isinstance(self.expected, dict) or not self.expected:
            raise ValueError("behavior check must declare expected observations")

    async def evaluate(self, workspace: Workspace) -> CheckResult:
        runner = IsolatedRunner(
            IsolationLimits(timeout_seconds=BEHAVIOR_TIMEOUT_SECONDS)
        )
        results_relpath = f".cairn-probe-{uuid.uuid4().hex}.json"
        results_path = workspace.root / results_relpath
        try:
            try:
                await runner.run_script(
                    workspace, _harness(self.program, results_relpath)
                )
            except IsolationUnavailable as exc:
                return CheckResult(
                    name=self.name,
                    passed=False,
                    error=f"IsolationUnavailable: {exc}",
                )
            return self._judge(results_path)
        finally:
            # Never leak the observation record back into the graded workspace.
            with contextlib.suppress(OSError):
                results_path.unlink(missing_ok=True)

    def _judge(self, results_path: Any) -> CheckResult:
        if not results_path.is_file():
            return CheckResult(
                name=self.name,
                passed=False,
                message="Behavior check failed: candidate produced no observation "
                "record (it did not complete, e.g. os._exit/crash)",
            )
        try:
            record = json.loads(results_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return CheckResult(
                name=self.name,
                passed=False,
                message="Behavior check failed: observation record was unreadable",
            )
        status = record.get("status")
        if status != "ok":
            return CheckResult(
                name=self.name,
                passed=False,
                message=f"Behavior check failed: probe did not complete ({status})",
            )
        observations = record.get("obs")
        if not isinstance(observations, dict):
            return CheckResult(
                name=self.name,
                passed=False,
                message="Behavior check failed: observation record malformed",
            )
        for key, expected in self.expected.items():
            if key not in observations:
                return CheckResult(
                    name=self.name,
                    passed=False,
                    message=f"Behavior check failed: missing observation {key!r}",
                )
            if not _matches(observations[key], expected):
                return CheckResult(
                    name=self.name,
                    passed=False,
                    message=(
                        f"Behavior check failed: {key!r} = "
                        f"{observations[key]!r}, expected {expected!r}"
                    ),
                )
        return CheckResult(name=self.name, passed=True, message=None)
