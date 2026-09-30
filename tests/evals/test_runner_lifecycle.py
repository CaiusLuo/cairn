import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cairn.core.models import LLMResponse, Message, ToolCall
from cairn.evals.models import CheckResult, EvalCase, EvalCheck, EvalStatus
from cairn.evals.runner import EvalRunner
from cairn.llm.base import LLMClient
from cairn.workspace.workspace import Workspace
from tests.support.runtime import TEST_BUDGET, SequenceLLM
from tests.support.sandbox import require_working_sandbox


class PassingCheck:
    name = "passing"

    async def evaluate(self, workspace: Workspace) -> CheckResult:
        assert workspace.root.is_dir()
        return CheckResult(name=self.name, passed=True)


class FailingCheck:
    name = "failing"

    async def evaluate(self, workspace: Workspace) -> CheckResult:
        assert workspace.root.is_dir()
        return CheckResult(name=self.name, passed=False, message="expected mismatch")


class SuccessfulLLM:
    async def generate(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None
    ) -> LLMResponse:
        return LLMResponse(content="complete")


class FailingLLM:
    async def generate(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None
    ) -> LLMResponse:
        raise RuntimeError("model unavailable")


def make_runner(
    llm_factory: Callable[[], LLMClient],
    *,
    temp_root: Path | None = None,
    run_timeout_seconds: float = 1,
    check_timeout_seconds: float = 1,
) -> EvalRunner:
    return EvalRunner(
        llm_factory,
        budget=TEST_BUDGET,
        run_timeout_seconds=run_timeout_seconds,
        check_timeout_seconds=check_timeout_seconds,
        temp_root=temp_root,
    )


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), -float("inf")])
def test_timeout_values_must_be_finite_and_positive(value: float) -> None:
    with pytest.raises(ValueError, match="finite and greater than zero"):
        EvalRunner(
            SuccessfulLLM,
            budget=TEST_BUDGET,
            run_timeout_seconds=value,
            check_timeout_seconds=1,
        )

    with pytest.raises(ValueError, match="finite and greater than zero"):
        EvalRunner(
            SuccessfulLLM,
            budget=TEST_BUDGET,
            run_timeout_seconds=1,
            check_timeout_seconds=value,
        )


@pytest.mark.parametrize(
    ("llm_factory", "check", "expected_status"),
    [
        (SuccessfulLLM, PassingCheck(), EvalStatus.PASS),
        (SuccessfulLLM, FailingCheck(), EvalStatus.FAIL),
        (FailingLLM, PassingCheck(), EvalStatus.ERROR),
    ],
)
def test_runner_deletes_only_its_child_directory(
    tmp_path: Path,
    llm_factory: Any,
    check: EvalCheck,
    expected_status: EvalStatus,
) -> None:
    root = tmp_path / "eval-parent"
    root.mkdir()
    sentinel = root / "keep.txt"
    sentinel.write_text("parent-owned", encoding="utf-8")
    seen_roots: list[Path] = []

    class ObserveWorkspace:
        name = "observe"

        async def evaluate(self, workspace: Workspace) -> CheckResult:
            seen_roots.append(workspace.root)
            return await check.evaluate(workspace)

    result = asyncio.run(
        make_runner(llm_factory, temp_root=root).run(
            EvalCase(name="lifecycle", prompt="finish"), checks=[ObserveWorkspace()]
        )
    )

    assert result.status is expected_status
    assert len(seen_roots) == 1
    assert seen_roots[0].parent == root
    assert not seen_roots[0].exists()
    assert sentinel.read_text(encoding="utf-8") == "parent-owned"
    assert list(root.iterdir()) == [sentinel]


def test_run_timeout_waits_for_llm_cancellation_cleanup_before_checks(
    tmp_path: Path,
) -> None:
    root = tmp_path / "eval-parent"
    root.mkdir()
    sentinel = root / "keep.txt"
    sentinel.write_text("parent-owned", encoding="utf-8")
    sibling = root / "sibling"
    sibling.mkdir()
    (sibling / "keep.txt").write_text("sibling-owned", encoding="utf-8")
    settled = asyncio.Event()
    case_roots: list[Path] = []

    class SlowLLM:
        async def generate(
            self, messages: list[Message], tools: list[dict[str, Any]] | None = None
        ) -> LLMResponse:
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.02)
                settled.set()
            raise AssertionError("cancelled generation unexpectedly resumed")

    class AfterRunCheck:
        name = "after-run"

        async def evaluate(self, workspace: Workspace) -> CheckResult:
            assert settled.is_set()
            assert workspace.root.is_dir()
            case_roots.append(workspace.root)
            return CheckResult(name=self.name, passed=True)

    result = asyncio.run(
        make_runner(
            SlowLLM,
            temp_root=root,
            run_timeout_seconds=0.2,
        ).run(EvalCase(name="timeout", prompt="wait"), checks=[AfterRunCheck()])
    )

    assert result.status is EvalStatus.ERROR
    assert result.error is not None and result.error.startswith("TimeoutError:")
    assert result.checks[0].passed
    assert settled.is_set()
    assert len(case_roots) == 1
    assert case_roots[0].parent == root
    assert not case_roots[0].exists()
    assert list(root.glob("cairn-eval-*")) == []
    assert sentinel.read_text(encoding="utf-8") == "parent-owned"
    assert (sibling / "keep.txt").read_text(encoding="utf-8") == "sibling-owned"


def test_external_cancellation_during_model_run_propagates_after_cleanup(
    tmp_path: Path,
) -> None:
    root = tmp_path / "eval-parent"
    root.mkdir()
    sentinel = root / "keep.txt"
    sentinel.write_text("parent-owned", encoding="utf-8")
    sibling = root / "sibling"
    sibling.mkdir()
    (sibling / "keep.txt").write_text("sibling-owned", encoding="utf-8")
    started = asyncio.Event()
    settled = asyncio.Event()
    checks_called = False

    class SlowLLM:
        async def generate(
            self, messages: list[Message], tools: list[dict[str, Any]] | None = None
        ) -> LLMResponse:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.02)
                settled.set()
            raise AssertionError("cancelled generation unexpectedly resumed")

    class ShouldNotCheck:
        name = "never"

        async def evaluate(self, workspace: Workspace) -> CheckResult:
            nonlocal checks_called
            checks_called = True
            return CheckResult(name=self.name, passed=True)

    async def scenario() -> None:
        task = asyncio.create_task(
            make_runner(SlowLLM, temp_root=root).run(
                EvalCase(name="cancel", prompt="wait"), checks=[ShouldNotCheck()]
            )
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())

    assert settled.is_set()
    assert not checks_called
    assert list(root.glob("cairn-eval-*")) == []
    assert sentinel.read_text(encoding="utf-8") == "parent-owned"
    assert (sibling / "keep.txt").read_text(encoding="utf-8") == "sibling-owned"


def test_external_cancellation_during_check_propagates_after_cleanup(
    tmp_path: Path,
) -> None:
    root = tmp_path / "eval-parent"
    root.mkdir()
    sentinel = root / "keep.txt"
    sentinel.write_text("parent-owned", encoding="utf-8")
    sibling = root / "sibling"
    sibling.mkdir()
    (sibling / "keep.txt").write_text("sibling-owned", encoding="utf-8")
    started = asyncio.Event()
    settled = asyncio.Event()
    case_roots: list[Path] = []

    class SlowCheck:
        name = "slow"

        async def evaluate(self, workspace: Workspace) -> CheckResult:
            assert workspace.root.is_dir()
            case_roots.append(workspace.root)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.02)
                settled.set()
            raise AssertionError("cancelled check unexpectedly resumed")

    async def scenario() -> None:
        task = asyncio.create_task(
            make_runner(SuccessfulLLM, temp_root=root).run(
                EvalCase(name="cancel-check", prompt="finish"), checks=[SlowCheck()]
            )
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())

    assert settled.is_set()
    assert len(case_roots) == 1
    assert case_roots[0].parent == root
    assert not case_roots[0].exists()
    assert list(root.glob("cairn-eval-*")) == []
    assert sentinel.read_text(encoding="utf-8") == "parent-owned"
    assert (sibling / "keep.txt").read_text(encoding="utf-8") == "sibling-owned"


def test_check_timeout_settles_before_next_check_and_keeps_results(
    tmp_path: Path,
) -> None:
    root = tmp_path / "eval-parent"
    root.mkdir()
    sentinel = root / "keep.txt"
    sentinel.write_text("parent-owned", encoding="utf-8")
    sibling = root / "sibling"
    sibling.mkdir()
    (sibling / "keep.txt").write_text("sibling-owned", encoding="utf-8")
    settled = asyncio.Event()
    case_roots: list[Path] = []

    class SlowCheck:
        name = "slow"

        async def evaluate(self, workspace: Workspace) -> CheckResult:
            case_roots.append(workspace.root)
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.02)
                settled.set()
            raise AssertionError("timed-out check unexpectedly resumed")

    class LaterCheck:
        name = "later"

        async def evaluate(self, workspace: Workspace) -> CheckResult:
            assert settled.is_set()
            return CheckResult(name=self.name, passed=True)

    result = asyncio.run(
        make_runner(
            SuccessfulLLM,
            temp_root=root,
            check_timeout_seconds=0.01,
        ).run(
            EvalCase(name="check-timeout", prompt="finish"),
            checks=[PassingCheck(), SlowCheck(), LaterCheck()],
        )
    )

    assert result.status is EvalStatus.ERROR
    assert result.error is None
    assert [check.name for check in result.checks] == ["passing", "slow", "later"]
    assert result.checks[0].passed
    assert not result.checks[1].passed
    assert result.checks[1].error is not None
    assert result.checks[1].error.startswith("TimeoutError: eval check exceeded")
    assert result.checks[2].passed
    assert settled.is_set()
    assert len(case_roots) == 1
    assert case_roots[0].parent == root
    assert not case_roots[0].exists()
    assert list(root.glob("cairn-eval-*")) == []
    assert sentinel.read_text(encoding="utf-8") == "parent-owned"
    assert (sibling / "keep.txt").read_text(encoding="utf-8") == "sibling-owned"


def test_checker_own_timeout_error_keeps_its_message(tmp_path: Path) -> None:
    class SelfTimeout:
        name = "self-timeout"

        async def evaluate(self, workspace: Workspace) -> CheckResult:
            raise TimeoutError("checker-specific deadline")

    result = asyncio.run(
        make_runner(SuccessfulLLM, temp_root=tmp_path).run(
            EvalCase(name="self-timeout", prompt="finish"), checks=[SelfTimeout()]
        )
    )

    assert result.status is EvalStatus.ERROR
    assert result.error is None
    assert result.checks[0].error == "TimeoutError: checker-specific deadline"


def test_llm_timeout_error_keeps_its_original_message(tmp_path: Path) -> None:
    class TimeoutLLM:
        async def generate(
            self, messages: list[Message], tools: list[dict[str, Any]] | None = None
        ) -> LLMResponse:
            raise TimeoutError("provider's own deadline")

    result = asyncio.run(
        make_runner(TimeoutLLM, temp_root=tmp_path).run(
            EvalCase(name="llm-timeout", prompt="finish"), checks=[PassingCheck()]
        )
    )

    assert result.status is EvalStatus.ERROR
    assert result.error == "TimeoutError: provider's own deadline"
    assert result.checks[0].passed


def test_invalid_temp_root_is_error_without_factory_call_or_deletion(
    tmp_path: Path,
) -> None:
    root = tmp_path / "owned-parent"
    root.mkdir()
    sentinel = root / "keep.txt"
    sentinel.write_text("parent-owned", encoding="utf-8")
    invalid_root = root / "does-not-exist"
    factory_called = False

    def factory() -> SuccessfulLLM:
        nonlocal factory_called
        factory_called = True
        return SuccessfulLLM()

    result = asyncio.run(
        make_runner(factory, temp_root=invalid_root).run(
            EvalCase(name="invalid-root", prompt="finish"), checks=[PassingCheck()]
        )
    )

    assert result.status is EvalStatus.ERROR
    assert result.error is not None
    assert not factory_called
    assert sentinel.read_text(encoding="utf-8") == "parent-owned"
    assert list(root.iterdir()) == [sentinel]


def test_missing_checks_is_configuration_error_without_factory_call(
    tmp_path: Path,
) -> None:
    root = tmp_path / "owned-parent"
    root.mkdir()
    sentinel = root / "keep.txt"
    sentinel.write_text("parent-owned", encoding="utf-8")
    factory_called = False

    def factory() -> SuccessfulLLM:
        nonlocal factory_called
        factory_called = True
        return SuccessfulLLM()

    result = asyncio.run(
        make_runner(factory, temp_root=root).run(
            EvalCase(name="no-checks", prompt="finish"), checks=[]
        )
    )

    assert result.status is EvalStatus.ERROR
    assert result.error == "ValueError: at least one eval check is required"
    assert not factory_called
    assert list(root.iterdir()) == [sentinel]


def test_run_timeout_cancels_real_bash_and_waits_for_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    require_working_sandbox(Workspace(tmp_path))
    root = tmp_path / "eval-parent"
    root.mkdir()
    sentinel = root / "keep.txt"
    sentinel.write_text("parent-owned", encoding="utf-8")
    sibling = root / "sibling"
    sibling.mkdir()
    (sibling / "keep.txt").write_text("sibling-owned", encoding="utf-8")
    cleanup_finished = asyncio.Event()
    case_roots: list[Path] = []
    from cairn.tools.bash import BashTool

    original_cleanup = BashTool._cleanup_process

    async def observe_cleanup(
        tool: BashTool, process: asyncio.subprocess.Process
    ) -> None:
        await original_cleanup(tool, process)
        assert process.returncode is not None
        cleanup_finished.set()

    monkeypatch.setattr(BashTool, "_cleanup_process", observe_cleanup)
    llm = SequenceLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="bash-call",
                        name="bash",
                        arguments={"command": "sleep 30"},
                    )
                ]
            )
        ]
    )

    class CleanupObservedCheck:
        name = "cleanup-observed"

        async def evaluate(self, workspace: Workspace) -> CheckResult:
            assert cleanup_finished.is_set()
            assert workspace.root.is_dir()
            case_roots.append(workspace.root)
            return CheckResult(name=self.name, passed=True)

    result = asyncio.run(
        make_runner(
            lambda: llm,
            temp_root=root,
            run_timeout_seconds=0.75,
        ).run(
            EvalCase(name="bash-timeout", prompt="run the command"),
            checks=[CleanupObservedCheck()],
        )
    )

    assert result.status is EvalStatus.ERROR
    assert result.error is not None and result.error.startswith("TimeoutError:")
    assert cleanup_finished.is_set()
    assert result.checks[0].passed
    assert len(case_roots) == 1
    assert case_roots[0].parent == root
    assert not case_roots[0].exists()
    assert list(root.glob("cairn-eval-*")) == []
    assert sentinel.read_text(encoding="utf-8") == "parent-owned"
    assert (sibling / "keep.txt").read_text(encoding="utf-8") == "sibling-owned"
