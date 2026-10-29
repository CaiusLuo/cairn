"""Offline regressions for issue 86: host-selected verification and one repair.

Exercises the real terminal entry + runtime sequencing with a scripted model and
real local shell commands (skipped when no bash). No provider or network.
"""

import asyncio
import json
import math
import os
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import cairn.terminal.headless as headless
from cairn.core.budget import RunBudget
from cairn.core.models import LLMResponse, ToolCall, ToolResult
from cairn.core.tasks import TaskCheckStatus
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.lifecycle import SettleOutcome
from cairn.runtime.tasks import check as check_module
from cairn.runtime.tasks.check import TaskCheckSpec, _execute_host_check
from cairn.terminal.headless import (
    HeadlessResult,
    HeadlessStatus,
    _exit_code,
    run_headless_task,
    task_app,
)
from tests.support.runtime import SequenceLLM
from tests.support.sandbox import require_local_shell


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "keep.txt").write_text("preserve me\n", encoding="utf-8")
    return ws


def _resp(*calls: ToolCall) -> LLMResponse:
    return LLMResponse(tool_calls=list(calls))


def _write(path: str, content: str, cid: str = "w") -> LLMResponse:
    return _resp(
        ToolCall(
            id=cid, name="write_file", arguments={"path": path, "content": content}
        )
    )


def _edit(path: str, old: str, new: str, cid: str = "e") -> LLMResponse:
    return _resp(
        ToolCall(
            id=cid,
            name="edit_file",
            arguments={"path": path, "old_text": old, "new_text": new},
        )
    )


def _run(**kwargs: object) -> HeadlessResult:
    return asyncio.run(run_headless_task(**kwargs))  # type: ignore[arg-type]


def test_first_check_passes(tmp_path: Path) -> None:
    require_local_shell()
    ws = _ws(tmp_path)
    llm = SequenceLLM([_write("out.txt", "ok\n"), LLMResponse(content="done")])
    result = _run(
        workspace=ws,
        prompt="write ok",
        grant_workspace_trust=True,
        grant_shell=True,
        llm=llm,
        budget=RunBudget(max_steps=20),
        check_command="grep -q '^ok' out.txt",
        check_timeout=20.0,
    )
    assert result.status is HeadlessStatus.COMPLETED
    assert result.verified is True
    assert result.check_requested is True
    assert result.check_status is TaskCheckStatus.PASSED
    assert result.check_attempts == 1
    assert result.check_exit_code == 0
    assert _exit_code(result) == 0
    assert (ws / "keep.txt").read_text(encoding="utf-8") == "preserve me\n"


def test_failed_check_then_repair_passes(tmp_path: Path) -> None:
    require_local_shell()
    ws = _ws(tmp_path)
    llm = SequenceLLM(
        [
            _write("out.txt", "bad\n", cid="w1"),
            LLMResponse(content="first done"),
            _edit("out.txt", "bad\n", "ok\n", cid="e1"),
            LLMResponse(content="repair done"),
        ]
    )
    result = _run(
        workspace=ws,
        prompt="make ok",
        grant_workspace_trust=True,
        grant_shell=True,
        llm=llm,
        budget=RunBudget(max_steps=20),
        check_command="grep -q '^ok' out.txt",
        check_timeout=20.0,
    )
    assert result.status is HeadlessStatus.COMPLETED
    assert result.verified is True
    assert result.check_status is TaskCheckStatus.PASSED
    assert result.check_attempts == 2
    assert _exit_code(result) == 0


def test_both_checks_fail(tmp_path: Path) -> None:
    require_local_shell()
    ws = _ws(tmp_path)
    llm = SequenceLLM(
        [
            _write("out.txt", "bad\n", cid="w1"),
            LLMResponse(content="first done"),
            _edit("out.txt", "bad\n", "still bad\n", cid="e1"),
            LLMResponse(content="repair done"),
        ]
    )
    result = _run(
        workspace=ws,
        prompt="make ok",
        grant_workspace_trust=True,
        grant_shell=True,
        llm=llm,
        budget=RunBudget(max_steps=20),
        check_command="grep -q '^ok' out.txt",
        check_timeout=20.0,
    )
    assert result.status is HeadlessStatus.CHECK_FAILED
    assert result.verified is False
    assert result.check_status is TaskCheckStatus.FAILED
    assert result.check_attempts == 2
    assert result.check_exit_code not in (0, None)
    assert _exit_code(result) == 7


def test_check_without_shell_authorization_runs_nothing(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    llm = SequenceLLM([LLMResponse(content="never")])
    result = _run(
        workspace=ws,
        prompt="x",
        grant_workspace_trust=True,
        grant_shell=False,
        llm=llm,
        budget=RunBudget(max_steps=5),
        check_command="touch marker",
        check_timeout=10.0,
    )
    assert result.status is HeadlessStatus.CHECK_PERMISSION_DENIED
    assert result.check_status is TaskCheckStatus.PERMISSION_DENIED
    assert result.verified is False
    assert llm.calls == []
    assert not (ws / "marker").exists()
    assert _exit_code(result) == 9


def test_cli_check_without_allow_shell_is_checked_offline(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    result = CliRunner().invoke(
        task_app,
        [
            "--workspace",
            str(ws),
            "--prompt",
            "x",
            "--trust-workspace",
            "--check-command",
            "touch marker",
            "--json",
        ],
    )
    assert result.exit_code == 9
    payload = json.loads(result.stdout)
    assert payload["status"] == "check_permission_denied"
    assert payload["check_requested"] is True
    assert payload["check_status"] == "permission_denied"
    assert not (ws / "marker").exists()


def test_cli_invalid_check_command_nul_is_rejected_offline(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    result = CliRunner().invoke(
        task_app,
        [
            "--workspace",
            str(ws),
            "--prompt",
            "x",
            "--trust-workspace",
            "--allow-shell",
            "--check-command",
            "bad\x00cmd",
            "--json",
        ],
    )
    assert result.exit_code == 2
    assert json.loads(result.stdout)["error_category"] == "invalid_check_command"


def test_preset_cancellation_does_not_start_check_command(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    event = asyncio.Event()
    event.set()
    execution = asyncio.run(
        _execute_host_check(
            workspace=Workspace(ws),
            command="touch marker",
            timeout=10.0,
            secret_env_keys=frozenset(),
            cancellation_event=event,
        )
    )
    assert execution.cancelled is True
    assert execution.started is False
    assert not (ws / "marker").exists()


def test_cancellation_set_before_run_yields_cancelled_without_command(
    tmp_path: Path,
) -> None:
    ws = _ws(tmp_path)
    event = asyncio.Event()
    event.set()
    llm = SequenceLLM([LLMResponse(content="never")])
    result = _run(
        workspace=ws,
        prompt="x",
        grant_workspace_trust=True,
        grant_shell=True,
        llm=llm,
        budget=RunBudget(max_steps=5),
        check_command="touch marker",
        check_timeout=10.0,
        cancellation_event=event,
    )
    assert result.status is HeadlessStatus.CANCELLED
    assert result.verified is False
    assert _exit_code(result) == 4
    assert llm.calls == []
    assert not (ws / "marker").exists()


def test_cooperative_cancellation_during_check_is_cancelled_no_repair(
    tmp_path: Path,
) -> None:
    require_local_shell()
    ws = _ws(tmp_path)
    llm = SequenceLLM(
        [
            _write("out.txt", "bad\n", cid="w1"),
            LLMResponse(content="first done"),
            _write("repaired.txt", "should not happen", cid="w2"),
            LLMResponse(content="repair done"),
        ]
    )

    async def scenario() -> HeadlessResult:
        event = asyncio.Event()
        task = asyncio.create_task(
            run_headless_task(
                workspace=ws,
                prompt="make ok",
                grant_workspace_trust=True,
                grant_shell=True,
                llm=llm,
                budget=RunBudget(max_steps=20),
                check_command="sh -c 'echo started > started.txt; sleep 30'",
                check_timeout=60.0,
                cancellation_event=event,
            )
        )
        for _ in range(400):
            if (ws / "started.txt").exists():
                break
            await asyncio.sleep(0.02)
        assert (ws / "started.txt").exists(), "check never started"
        event.set()
        return await task

    result = asyncio.run(scenario())
    assert result.status is HeadlessStatus.CANCELLED
    assert result.verified is False
    # The command did start, so it counts as one attempt with a cancelled status.
    assert result.check_attempts == 1
    assert result.check_status is TaskCheckStatus.CANCELLED
    assert _exit_code(result) == 4
    assert not (ws / "repaired.txt").exists()  # no repair turn ran
    assert len(llm.calls) == 2


def test_cancelled_second_check_counts_two_attempts(tmp_path: Path) -> None:
    require_local_shell()
    ws = _ws(tmp_path)
    # First check: repaired absent -> exit 1. After repair creates 'repaired',
    # the same command blocks, so a cooperative cancellation hits the 2nd check.
    command = "sh -c 'echo run >> starts.txt; if [ -f repaired ]; then sleep 30; else exit 1; fi'"
    llm = SequenceLLM(
        [
            _write("out.txt", "bad\n", cid="w1"),
            LLMResponse(content="first done"),
            _write("repaired", "1", cid="w2"),
            LLMResponse(content="repair done"),
        ]
    )

    async def scenario() -> HeadlessResult:
        event = asyncio.Event()
        task = asyncio.create_task(
            run_headless_task(
                workspace=ws,
                prompt="make ok",
                grant_workspace_trust=True,
                grant_shell=True,
                llm=llm,
                budget=RunBudget(max_steps=20),
                check_command=command,
                check_timeout=60.0,
                cancellation_event=event,
            )
        )
        for _ in range(500):
            if (ws / "starts.txt").exists() and len(
                (ws / "starts.txt").read_text().splitlines()
            ) >= 2:
                break
            await asyncio.sleep(0.02)
        assert (ws / "starts.txt").exists()
        event.set()
        return await task

    result = asyncio.run(scenario())
    assert result.status is HeadlessStatus.CANCELLED
    assert result.check_attempts == 2
    assert result.check_status is TaskCheckStatus.CANCELLED
    assert _exit_code(result) == 4


def test_repeated_external_cancellation_cleans_up_check_child(
    tmp_path: Path,
) -> None:
    require_local_shell()
    ws = _ws(tmp_path)
    llm = SequenceLLM([_write("out.txt", "bad\n"), LLMResponse(content="done")])

    async def scenario() -> int:
        task = asyncio.create_task(
            run_headless_task(
                workspace=ws,
                prompt="make ok",
                grant_workspace_trust=True,
                grant_shell=True,
                llm=llm,
                budget=RunBudget(max_steps=20),
                check_command="sh -c 'echo $$ > child.pid; sleep 30'",
                check_timeout=60.0,
            )
        )
        for _ in range(400):
            if (ws / "child.pid").exists():
                break
            await asyncio.sleep(0.02)
        assert (ws / "child.pid").exists(), "check did not start"
        pid = int((ws / "child.pid").read_text().strip())
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()  # repeated cancellation must not truncate cleanup
        with pytest.raises(asyncio.CancelledError):
            await task
        return pid

    pid = asyncio.run(scenario())
    for _ in range(200):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail("check child process survived cancellation")


def test_late_cancellation_during_cleanup_is_propagated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _ws(tmp_path)

    class _ImmediateBash:
        def __init__(self, *_a: object, **_k: object) -> None:
            pass

        async def execute(
            self, arguments: dict[str, object], **_k: object
        ) -> ToolResult:
            return ToolResult(stdout="ok", stderr="", exit_code=0)

    async def _settle_with_late_cancel(*_tasks: object, **_k: object) -> SettleOutcome:
        return SettleOutcome(tuple(None for _ in _tasks), asyncio.CancelledError())

    monkeypatch.setattr(check_module, "BashTool", _ImmediateBash)
    monkeypatch.setattr(check_module, "settle_owned", _settle_with_late_cancel)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            _execute_host_check(
                workspace=Workspace(ws),
                command="true",
                timeout=5.0,
                secret_env_keys=frozenset(),
                cancellation_event=None,
            )
        )


def test_check_timeout_is_reported(tmp_path: Path) -> None:
    require_local_shell()
    ws = _ws(tmp_path)
    llm = SequenceLLM([_write("out.txt", "bad\n"), LLMResponse(content="done")])
    result = _run(
        workspace=ws,
        prompt="x",
        grant_workspace_trust=True,
        grant_shell=True,
        llm=llm,
        budget=RunBudget(max_steps=20),
        check_command="sleep 30",
        check_timeout=0.3,
    )
    assert result.status is HeadlessStatus.CHECK_TIMED_OUT
    assert result.check_status is TaskCheckStatus.TIMED_OUT
    assert result.check_attempts == 1
    assert result.verified is False
    assert result.check_exit_code is None
    assert _exit_code(result) == 8


def test_invalid_command_nul_runs_no_agent_turn(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    llm = SequenceLLM([_write("ran.txt", "x"), LLMResponse(content="done")])
    with pytest.raises(ValueError):
        _run(
            workspace=ws,
            prompt="x",
            grant_workspace_trust=True,
            grant_shell=True,
            llm=llm,
            budget=RunBudget(max_steps=5),
            check_command="bad\x00cmd",
            check_timeout=5.0,
        )
    assert llm.calls == []
    assert not (ws / "ran.txt").exists()


def test_nonpositive_or_nonfinite_timeout_rejected_at_cli(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    for value in ("0", "-1", "nan", "inf"):
        result = CliRunner().invoke(
            task_app,
            [
                "--workspace",
                str(ws),
                "--prompt",
                "x",
                "--trust-workspace",
                "--allow-shell",
                "--check-command",
                "true",
                "--check-timeout",
                value,
                "--json",
            ],
        )
        assert result.exit_code == 2, value
        payload = json.loads(result.stdout)
        assert payload["status"] == "invalid_request", value
        assert payload["error_category"] == "invalid_check_timeout", value


def test_nonpositive_or_nonfinite_timeout_rejected_at_runtime() -> None:
    for value in (0.0, -1.0, math.inf, math.nan):
        with pytest.raises(ValueError):
            TaskCheckSpec(command="true", timeout=value)


def test_check_command_is_identical_across_rounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _ws(tmp_path)
    recorded: list[str] = []

    class _FakeBash:
        def __init__(self, *_a: object, **_k: object) -> None:
            pass

        async def execute(
            self, arguments: dict[str, object], **_k: object
        ) -> ToolResult:
            command = arguments["command"]
            assert isinstance(command, str)
            recorded.append(command)
            return ToolResult(
                stdout="", stderr="", exit_code=1 if len(recorded) == 1 else 0
            )

    monkeypatch.setattr(check_module, "BashTool", _FakeBash)
    llm = SequenceLLM(
        [
            _write("out.txt", "bad\n", cid="w1"),
            LLMResponse(content="first done"),
            _edit("out.txt", "bad\n", "ok\n", cid="e1"),
            LLMResponse(content="repair done"),
        ]
    )
    result = _run(
        workspace=ws,
        prompt="make ok",
        grant_workspace_trust=True,
        grant_shell=True,
        llm=llm,
        budget=RunBudget(max_steps=20),
        check_command="OPERATOR-FIXED-CHECK",
        check_timeout=5.0,
    )
    assert result.status is HeadlessStatus.COMPLETED
    assert result.check_attempts == 2
    assert recorded == ["OPERATOR-FIXED-CHECK", "OPERATOR-FIXED-CHECK"]


def test_exit_codes_for_check_statuses() -> None:
    ws = "/tmp/ws"
    assert (
        _exit_code(
            HeadlessResult(
                task_id="t", status=HeadlessStatus.CHECK_FAILED, workspace=ws
            )
        )
        == 7
    )
    assert (
        _exit_code(
            HeadlessResult(
                task_id="t", status=HeadlessStatus.CHECK_TIMED_OUT, workspace=ws
            )
        )
        == 8
    )
    assert (
        _exit_code(
            HeadlessResult(
                task_id="t", status=HeadlessStatus.CHECK_PERMISSION_DENIED, workspace=ws
            )
        )
        == 9
    )
    denied = HeadlessResult(
        task_id="t",
        status=HeadlessStatus.COMPLETED,
        workspace=ws,
        permission_denied=True,
    )
    assert _exit_code(denied) == 6
    verified = HeadlessResult(
        task_id="t", status=HeadlessStatus.COMPLETED, workspace=ws, verified=True
    )
    assert _exit_code(verified) == 0


def test_cli_cancellation_during_active_check_preserves_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    require_local_shell()
    ws = _ws(tmp_path)
    llm = SequenceLLM([_write("out.txt", "bad\n"), LLMResponse(content="done")])

    def fake_cfg(**kw: Any) -> None:
        workspace: Path = kw["workspace"]
        command: str = kw["check_command"]
        timeout: float = kw["check_timeout"]
        ref: dict[str, Any] = kw["check_ref"]

        async def run() -> None:
            event = asyncio.Event()
            task = asyncio.create_task(
                headless.run_headless_task(
                    workspace=workspace,
                    prompt="x",
                    grant_workspace_trust=True,
                    grant_shell=True,
                    llm=llm,
                    budget=RunBudget(max_steps=20),
                    check_command=command,
                    check_timeout=timeout,
                    cancellation_event=event,
                    check_ref=ref,
                )
            )
            for _ in range(400):
                if (workspace / "started.txt").exists():
                    break
                await asyncio.sleep(0.02)
            assert (workspace / "started.txt").exists(), "check never started"
            task.cancel()  # external cancellation, like a real SIGINT
            try:
                await task
            except asyncio.CancelledError:
                raise KeyboardInterrupt from None

        asyncio.run(run())

    monkeypatch.setattr(headless, "_run_via_config", fake_cfg)
    result = CliRunner().invoke(
        task_app,
        [
            "--workspace",
            str(ws),
            "--prompt",
            "x",
            "--trust-workspace",
            "--allow-shell",
            "--check-command",
            "sh -c 'echo x > started.txt; sleep 30'",
            "--json",
        ],
    )
    assert result.exit_code == 4
    payload = json.loads(result.stdout)
    assert payload["status"] == "cancelled"
    assert payload["check_requested"] is True
    assert payload["check_status"] == "cancelled"
    assert payload["check_attempts"] == 1


def test_check_child_does_not_inherit_credential_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    require_local_shell()
    secret_name = "CAIRN_ISSUE86_SECRET"
    secret_value = "server-only-credential-value"
    monkeypatch.setenv(secret_name, secret_value)
    ws = _ws(tmp_path)
    llm = SequenceLLM([_write("out.txt", "ok\n"), LLMResponse(content="done")])
    # The child must see the variable absent; otherwise it exits 1 and the task
    # would need a repair, so a passing check proves the credential was stripped.
    command = (
        "sh -c 'if [ -n \"$" + secret_name + '" ]; then echo LEAKED; exit 1; '
        "else exit 0; fi'"
    )
    result = _run(
        workspace=ws,
        prompt="x",
        grant_workspace_trust=True,
        grant_shell=True,
        llm=llm,
        budget=RunBudget(max_steps=20),
        check_command=command,
        check_timeout=20.0,
        secret_env_keys=frozenset({secret_name}),
        secret_values=(secret_value,),
    )
    assert result.status is HeadlessStatus.COMPLETED
    assert result.check_status is TaskCheckStatus.PASSED
    assert result.verified is True
    assert result.check_attempts == 1
    assert secret_value not in result.model_dump_json()


def test_failing_check_output_is_redacted_before_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    require_local_shell()
    secret_name = "CAIRN_ISSUE86_SECRET"
    secret_value = "echoed-secret-must-not-reach-model"
    monkeypatch.setenv(secret_name, secret_value)
    ws = _ws(tmp_path)
    llm = SequenceLLM(
        [
            _write("out.txt", "bad\n", cid="w1"),
            LLMResponse(content="first done"),
            _edit("out.txt", "bad\n", "ok\n", cid="e1"),
            LLMResponse(content="repair done"),
        ]
    )
    # The check echoes the synthetic credential and fails every time; the value
    # must be redacted before the repair model ever sees the output.
    command = f"sh -c 'echo {secret_value}; exit 1'"
    result = _run(
        workspace=ws,
        prompt="x",
        grant_workspace_trust=True,
        grant_shell=True,
        llm=llm,
        budget=RunBudget(max_steps=20),
        check_command=command,
        check_timeout=20.0,
        secret_env_keys=frozenset({secret_name}),
        secret_values=(secret_value,),
    )
    assert result.status is HeadlessStatus.CHECK_FAILED
    assert result.check_attempts == 2
    assert secret_value not in result.model_dump_json()
    # Inspect what the model actually received across both turns.
    transcript = repr(llm.calls)
    assert secret_value not in transcript
    assert "[redacted]" in transcript  # the repair prompt carried a redaction marker


def test_invalid_timeout_without_shell_grant_is_validated_first(
    tmp_path: Path,
) -> None:
    # Argument validity must precede the shell-authorization decision: with no
    # --allow-shell, an invalid timeout is still reported as invalid input (2),
    # not as a permission denial (9).
    ws = _ws(tmp_path)
    for value in ("0", "-2", "nan", "inf"):
        result = CliRunner().invoke(
            task_app,
            [
                "--workspace",
                str(ws),
                "--prompt",
                "x",
                "--trust-workspace",
                "--check-command",
                "true",
                "--check-timeout",
                value,
                "--json",
            ],
        )
        assert result.exit_code == 2, value
        payload = json.loads(result.stdout)
        assert payload["status"] == "invalid_request", value
        assert payload["error_category"] == "invalid_check_timeout", value
