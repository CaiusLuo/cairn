"""Host-authorized verification and one-shot repair, sequenced in the runtime.

``cairn task run --check-command`` needs to run the operator's fixed command after
a turn, hand a bounded failure back to the agent for at most one repair, then run
the *same* command again. That sequencing belongs to the runtime that already owns
the task's turn(s); the terminal only parses the CLI and presents the result. This
module adds no second Agent loop: every attempt is one existing
``CodingTaskRunner.run`` / ``run_turn``.

The verification command is always the operator's value. It is never taken from
the task text, a model reply or a tool argument. Execution reuses ``BashTool`` for
the workspace cwd, credential-filtered child environment, bounded output, process
group cleanup and cancellation. Timeouts are detected explicitly from an awaited
deadline, never inferred from a ``-1`` exit code (a signal such as SIGHUP also
surfaces as a negative return code).
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from cairn.core.events import Event, EventHandler
from cairn.core.tasks import TaskCheckStatus, TaskResult, TaskSpec, TaskStatus
from cairn.infra.tools.bash import BashTool
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.lifecycle import settle_owned
from cairn.runtime.tasks.runner import CodingTaskRunner

DEFAULT_CHECK_OUTPUT_LIMIT = 4000


@dataclass(frozen=True, slots=True)
class TaskCheckSpec:
    """The operator-selected verification command and its fixed timeout."""

    command: str
    timeout: float

    def __post_init__(self) -> None:
        if not self.command.strip() or "\0" in self.command:
            raise ValueError("check command must be a non-empty string without NUL")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError(
                "check timeout must be a positive finite number of seconds"
            )


@dataclass(frozen=True, slots=True)
class CheckedTaskOutcome:
    """Runtime result of a task plus its optional host verification.

    ``task`` is the final turn result (the repair turn when one ran), or ``None``
    only when a check was requested without shell authorization so no turn ran.
    ``verified`` is true only when the final host check passed **and** no required
    permission was denied; a passing check after a denial still leaves it false.
    """

    task: TaskResult | None
    check_status: TaskCheckStatus | None
    check_exit_code: int | None
    check_attempts: int
    verified: bool
    denied_tools: tuple[str, ...]
    check_permission_denied: bool = False
    cancelled: bool = False


@dataclass(slots=True)
class _CheckExecution:
    exit_code: int | None
    timed_out: bool
    cancelled: bool
    output: str
    started: bool = True


def _redact(text: str | None, secret_values: Sequence[str]) -> str | None:
    if text is None:
        return None
    clean = text
    for value in secret_values:
        if value:
            clean = clean.replace(value, "[redacted]")
    return clean


async def _execute_host_check(
    *,
    workspace: Workspace,
    command: str,
    timeout: float,
    secret_env_keys: frozenset[str],
    cancellation_event: asyncio.Event | None,
    check_ref: dict[str, Any] | None = None,
) -> _CheckExecution:
    """Run the fixed host command, detecting timeout explicitly."""
    if cancellation_event is not None and cancellation_event.is_set():
        # Already cancelled before this check: start no command at all.
        return _CheckExecution(None, False, True, "", started=False)
    if check_ref is not None:
        # Record that a command actually started (not merely requested), so a
        # caller cancellation reports the true attempt count.
        check_ref["attempts"] = int(check_ref.get("attempts", 0)) + 1
        check_ref["running"] = True
    # BashTool's own timeout is set beyond ours only as a backstop; our awaited
    # deadline is the authoritative timeout, so a `-1` process exit is never
    # mistaken for a timeout.
    tool = BashTool(
        workspace,
        timeout=timeout + max(1.0, timeout),
        secret_env_keys=secret_env_keys,
    )
    execution = asyncio.create_task(tool.execute({"command": command}))
    waiter = (
        asyncio.create_task(cancellation_event.wait())
        if cancellation_event is not None
        else None
    )
    timed_out = False
    cancelled = False
    tool_result = None
    primary: BaseException | None = None
    settle_cancellation: asyncio.CancelledError | None = None
    try:
        watched: set[asyncio.Task[Any]] = {execution}
        if waiter is not None:
            watched.add(waiter)
        done, _pending = await asyncio.wait(
            watched, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if execution in done:
            tool_result = execution.result()
        elif waiter is not None and waiter in done:
            cancelled = True
        else:
            timed_out = True
    except BaseException as exc:
        primary = exc
        raise
    finally:
        owned = tuple(task for task in (execution, waiter) if task is not None)
        # Owned-lifecycle settle cancels unfinished work and observes it to
        # completion, absorbing repeated external cancellation so the check's
        # process-group cleanup is never truncated. A late cancellation observed
        # here is re-raised after cleanup when there is no primary error.
        settled = await settle_owned(*owned)
        settle_cancellation = settled.cancellation
        if primary is not None and settle_cancellation is not None:
            primary.add_note("Late cancellation while settling the host check")
    if settle_cancellation is not None:
        raise settle_cancellation

    if check_ref is not None:
        check_ref["running"] = False
    if cancelled:
        return _CheckExecution(None, False, True, "")
    if timed_out:
        return _CheckExecution(None, True, False, "")
    assert tool_result is not None
    output = tool_result.stdout
    if tool_result.stderr:
        output = f"{output}\n{tool_result.stderr}" if output else tool_result.stderr
    return _CheckExecution(tool_result.exit_code, False, False, output)


def _check_failure_prompt(
    *,
    prompt: str,
    exit_code: int,
    output: str,
    secret_values: Sequence[str],
) -> str:
    """A bounded, credential-redacted repair prompt carrying the check failure."""
    clean = (_redact(output, secret_values) or "")[:DEFAULT_CHECK_OUTPUT_LIMIT]
    return (
        "The host verification for the previous attempt failed with exit code "
        f"{exit_code}. Fix the workspace so the same verification passes. Do not "
        "modify, disable or replace the verification.\n\n"
        "--- verification output (bounded, redacted, possibly truncated) ---\n"
        f"{clean}\n--- end verification output ---\n\n"
        "Original task:\n" + prompt
    )


def _outcome(
    *,
    task: TaskResult,
    check_status: TaskCheckStatus,
    check_exit_code: int | None,
    check_attempts: int,
    verified: bool,
    denied: set[str],
) -> CheckedTaskOutcome:
    return CheckedTaskOutcome(
        task=task,
        check_status=check_status,
        check_exit_code=check_exit_code,
        check_attempts=check_attempts,
        verified=verified and not denied,
        denied_tools=tuple(sorted(denied)),
    )


async def run_task_with_check(
    *,
    runner: CodingTaskRunner,
    spec: TaskSpec,
    check: TaskCheckSpec | None,
    shell_authorized: bool,
    cancellation_event: asyncio.Event | None = None,
    secret_values: Sequence[str] = (),
    secret_env_keys: frozenset[str] = frozenset(),
    check_ref: dict[str, Any] | None = None,
) -> CheckedTaskOutcome:
    """Run ``spec`` (once) and, when a check is given, verify and repair once.

    External cancellation propagates after the runner settles owned work; a
    cooperative ``cancellation_event`` stops the next step, including a running
    check, without starting a repair.
    """
    if check is not None:
        # Validate the operator command before any agent turn can run.
        if not check.command.strip() or "\0" in check.command:
            raise ValueError("check command must be a non-empty string without NUL")
        if not math.isfinite(check.timeout) or check.timeout <= 0:
            raise ValueError(
                "check timeout must be a positive finite number of seconds"
            )
    ref = check_ref if check_ref is not None else {}
    denied: set[str] = set()
    original_handler: EventHandler | None = runner.event_handler

    def observe(event: Event) -> None:
        if event.type == "tool_denied":
            name = event.data.get("tool")
            if isinstance(name, str) and name:
                denied.add(name)
        if original_handler is not None:
            original_handler(event)

    runner.event_handler = observe
    try:
        if check is not None and not shell_authorized:
            # No host shell authority: refuse before running any turn or command.
            return CheckedTaskOutcome(
                task=None,
                check_status=TaskCheckStatus.PERMISSION_DENIED,
                check_exit_code=None,
                check_attempts=0,
                verified=False,
                denied_tools=(),
                check_permission_denied=True,
            )

        first = await runner.run(spec, cancellation_event=cancellation_event)
        if check is None:
            return CheckedTaskOutcome(
                task=first,
                check_status=None,
                check_exit_code=None,
                check_attempts=0,
                verified=False,
                denied_tools=tuple(sorted(denied)),
            )
        if first.status is not TaskStatus.COMPLETED:
            return _outcome(
                task=first,
                check_status=TaskCheckStatus.NOT_RUN,
                check_exit_code=None,
                check_attempts=0,
                verified=False,
                denied=denied,
            )

        check_run = await _execute_host_check(
            workspace=runner.workspace,
            command=check.command,
            timeout=check.timeout,
            secret_env_keys=secret_env_keys,
            cancellation_event=cancellation_event,
            check_ref=ref,
        )
        ref["status"] = (
            "cancelled"
            if check_run.cancelled
            else "timed_out"
            if check_run.timed_out
            else "passed"
            if check_run.exit_code == 0
            else "failed"
        )
        if check_run.cancelled:
            started = check_run.started
            return CheckedTaskOutcome(
                task=first,
                check_status=(
                    TaskCheckStatus.CANCELLED if started else TaskCheckStatus.NOT_RUN
                ),
                check_exit_code=None,
                check_attempts=1 if started else 0,
                verified=False,
                denied_tools=tuple(sorted(denied)),
                cancelled=True,
            )
        if check_run.timed_out:
            return _outcome(
                task=first,
                check_status=TaskCheckStatus.TIMED_OUT,
                check_exit_code=None,
                check_attempts=1,
                verified=False,
                denied=denied,
            )
        if check_run.exit_code == 0:
            return _outcome(
                task=first,
                check_status=TaskCheckStatus.PASSED,
                check_exit_code=0,
                check_attempts=1,
                verified=True,
                denied=denied,
            )

        # One bounded, redacted repair turn, then the SAME operator command again.
        repair_prompt = _check_failure_prompt(
            prompt=spec.prompt,
            exit_code=check_run.exit_code if check_run.exit_code is not None else -1,
            output=check_run.output,
            secret_values=secret_values,
        )
        repair = await runner.run(
            TaskSpec(task_id=spec.task_id, prompt=repair_prompt, name="repair"),
            cancellation_event=cancellation_event,
        )
        if repair.status is not TaskStatus.COMPLETED:
            return _outcome(
                task=repair,
                check_status=TaskCheckStatus.FAILED,
                check_exit_code=check_run.exit_code,
                check_attempts=1,
                verified=False,
                denied=denied,
            )
        second = await _execute_host_check(
            workspace=runner.workspace,
            command=check.command,
            timeout=check.timeout,
            secret_env_keys=secret_env_keys,
            cancellation_event=cancellation_event,
            check_ref=ref,
        )
        ref["status"] = (
            "cancelled"
            if second.cancelled
            else "timed_out"
            if second.timed_out
            else "passed"
            if second.exit_code == 0
            else "failed"
        )
        if second.cancelled:
            started = second.started
            return CheckedTaskOutcome(
                task=repair,
                check_status=(
                    TaskCheckStatus.CANCELLED if started else TaskCheckStatus.FAILED
                ),
                check_exit_code=check_run.exit_code,
                check_attempts=2 if started else 1,
                verified=False,
                denied_tools=tuple(sorted(denied)),
                cancelled=True,
            )
        if second.timed_out:
            return _outcome(
                task=repair,
                check_status=TaskCheckStatus.TIMED_OUT,
                check_exit_code=None,
                check_attempts=2,
                verified=False,
                denied=denied,
            )
        if second.exit_code == 0:
            return _outcome(
                task=repair,
                check_status=TaskCheckStatus.PASSED,
                check_exit_code=0,
                check_attempts=2,
                verified=True,
                denied=denied,
            )
        return _outcome(
            task=repair,
            check_status=TaskCheckStatus.FAILED,
            check_exit_code=second.exit_code,
            check_attempts=2,
            verified=False,
            denied=denied,
        )
    finally:
        runner.event_handler = original_handler
