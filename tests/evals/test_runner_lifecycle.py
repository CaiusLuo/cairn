import asyncio
from collections.abc import Callable
from io import StringIO
from pathlib import Path
from typing import Any, TextIO, cast

import pytest

from cairn.core.models import LLMResponse, Message, ToolCall
from cairn.evals.checks import CHECK_READ_CHUNK_SIZE, FileNotContainsCheck
from cairn.evals.models import CheckResult, EvalCase, EvalCheck, EvalResult, EvalStatus
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


class SuccessfulLLM:
    async def generate(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None
    ) -> LLMResponse:
        return LLMResponse(content="complete")


class TimeoutLLM:
    async def generate(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None
    ) -> LLMResponse:
        raise TimeoutError("provider's own deadline")


class SelfTimeoutCheck:
    name = "self-timeout"

    async def evaluate(self, workspace: Workspace) -> CheckResult:
        raise TimeoutError("checker-specific deadline")


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


@pytest.fixture
def owned_eval_parent(tmp_path: Path) -> tuple[Path, Path, Path]:
    parent = tmp_path / "eval-parent"
    parent.mkdir()
    sentinel = parent / "keep.txt"
    sentinel.write_text("parent-owned", encoding="utf-8")
    sibling = parent / "sibling"
    sibling.mkdir()
    (sibling / "keep.txt").write_text("sibling-owned", encoding="utf-8")
    return parent, sentinel, sibling


def assert_parent_preserved(parent: Path, sentinel: Path, sibling: Path) -> None:
    assert list(parent.glob("cairn-eval-*")) == []
    assert sentinel.read_text(encoding="utf-8") == "parent-owned"
    assert (sibling / "keep.txt").read_text(encoding="utf-8") == "sibling-owned"


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
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


def test_run_timeout_waits_for_llm_cancellation_cleanup_before_checks(
    owned_eval_parent: tuple[Path, Path, Path],
) -> None:
    root, sentinel, sibling = owned_eval_parent
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
    assert_parent_preserved(root, sentinel, sibling)


@pytest.mark.parametrize("stage", ["run", "check"])
def test_external_cancellation_propagates_after_cleanup(
    stage: str,
    owned_eval_parent: tuple[Path, Path, Path],
) -> None:
    root, sentinel, sibling = owned_eval_parent
    started = asyncio.Event()
    settled = asyncio.Event()
    checks_called = False
    case_roots: list[Path] = []

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

    class SlowCheck:
        name = "slow-check"

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

    llm_factory: Callable[[], LLMClient]
    checks: list[EvalCheck]
    if stage == "run":
        llm_factory = SlowLLM
        checks = [ShouldNotCheck()]
    else:
        llm_factory = SuccessfulLLM
        checks = [SlowCheck()]

    async def scenario() -> None:
        task = asyncio.create_task(
            make_runner(llm_factory, temp_root=root).run(
                EvalCase(name=f"cancel-{stage}", prompt="wait"), checks=checks
            )
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())

    assert settled.is_set()
    if stage == "run":
        assert not checks_called
        assert case_roots == []
    else:
        assert len(case_roots) == 1
        assert case_roots[0].parent == root
        assert not case_roots[0].exists()
    assert_parent_preserved(root, sentinel, sibling)


def test_check_timeout_settles_before_next_check_and_keeps_results(
    owned_eval_parent: tuple[Path, Path, Path],
) -> None:
    root, sentinel, sibling = owned_eval_parent
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
    assert_parent_preserved(root, sentinel, sibling)


@pytest.mark.parametrize(
    ("llm_factory", "checks", "execution_error", "check_error"),
    [
        (
            TimeoutLLM,
            [PassingCheck()],
            "TimeoutError: provider's own deadline",
            None,
        ),
        (
            SuccessfulLLM,
            [SelfTimeoutCheck()],
            None,
            "TimeoutError: checker-specific deadline",
        ),
    ],
)
def test_component_timeout_errors_keep_original_messages(
    owned_eval_parent: tuple[Path, Path, Path],
    llm_factory: Callable[[], LLMClient],
    checks: list[EvalCheck],
    execution_error: str | None,
    check_error: str | None,
) -> None:
    root, sentinel, sibling = owned_eval_parent
    result = asyncio.run(
        make_runner(llm_factory, temp_root=root).run(
            EvalCase(name="component-timeout", prompt="finish"), checks=checks
        )
    )

    assert result.status is EvalStatus.ERROR
    assert result.error == execution_error
    assert len(result.checks) == 1
    assert result.checks[0].error == check_error
    assert result.checks[0].passed is (check_error is None)
    assert_parent_preserved(root, sentinel, sibling)


def test_invalid_temp_root_is_error_without_factory_call_or_deletion(
    owned_eval_parent: tuple[Path, Path, Path],
) -> None:
    root, sentinel, sibling = owned_eval_parent
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
    assert_parent_preserved(root, sentinel, sibling)


def test_run_timeout_cancels_real_bash_and_waits_for_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owned_eval_parent: tuple[Path, Path, Path],
) -> None:
    require_working_sandbox(Workspace(tmp_path))
    root, sentinel, sibling = owned_eval_parent
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
    assert_parent_preserved(root, sentinel, sibling)


def test_builtin_check_timeout_closes_stream_and_runner_cleans_workspace(
    monkeypatch: pytest.MonkeyPatch,
    owned_eval_parent: tuple[Path, Path, Path],
) -> None:
    parent, sentinel, sibling = owned_eval_parent

    class InfiniteTextStream(StringIO):
        def __init__(self) -> None:
            super().__init__()
            self.read_count = 0

        def read(self, size: int | None = -1) -> str:
            assert size is not None
            assert size > 0
            assert size == CHECK_READ_CHUNK_SIZE
            self.read_count += 1
            assert self.read_count <= 8, "check read guard exceeded"
            advance_loop_clock()
            return "x" * size

    stream = InfiniteTextStream()
    original_open = Path.open
    check_timeout_seconds = 0.1
    clock_offset = 0.0

    def advance_loop_clock() -> None:
        nonlocal clock_offset
        clock_offset += check_timeout_seconds * 2

    def controlled_open(
        path: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> TextIO:
        if path.name == "answer.txt" and mode == "r":
            return stream
        return cast(
            TextIO, original_open(path, mode, buffering, encoding, errors, newline)
        )

    monkeypatch.setattr(Path, "open", controlled_open)
    workspace_roots: list[Path] = []

    class CheckAfterTimeout:
        name = "after-timeout"

        async def evaluate(self, workspace: Workspace) -> CheckResult:
            assert stream.closed
            assert workspace.root.is_dir()
            workspace_roots.append(workspace.root)
            return CheckResult(name=self.name, passed=True)

    async def run_case() -> EvalResult:
        loop = asyncio.get_running_loop()
        real_loop_time = loop.time

        def advanced_loop_time() -> float:
            return real_loop_time() + clock_offset

        monkeypatch.setattr(loop, "time", advanced_loop_time)
        return await make_runner(
            SuccessfulLLM,
            temp_root=parent,
            run_timeout_seconds=10,
            check_timeout_seconds=check_timeout_seconds,
        ).run(
            EvalCase(
                name="builtin-timeout",
                prompt="finish",
                files={"answer.txt": "regular fixture file"},
            ),
            checks=[FileNotContainsCheck("answer.txt", "needle"), CheckAfterTimeout()],
        )

    result = asyncio.run(run_case())

    assert result.status is EvalStatus.ERROR
    assert result.error is None
    assert result.checks[0].error is not None
    assert result.checks[0].error.startswith("TimeoutError:")
    assert result.checks[1].passed
    assert stream.read_count <= 8
    assert stream.closed
    assert len(workspace_roots) == 1
    assert workspace_roots[0].parent == parent
    assert not workspace_roots[0].exists()
    assert_parent_preserved(parent, sentinel, sibling)
