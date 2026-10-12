import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cairn.core.budget import RunBudget
from cairn.core.context import ContextBudget, TokenCount
from cairn.core.models import LLMResponse, Message, ToolCall
from cairn.infra.observability.models import Span, SpanStatus, new_trace_context
from cairn.infra.observability.tracer import Tracer
from cairn.infra.repo_graph.models import BuildLimits
from cairn.infra.tools.base import InvalidArguments
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.evals.checks import FileContentEqualsCheck
from cairn.runtime.evals.intelligence.fixtures import IntelligenceCase, MemoryFixture
from cairn.runtime.evals.intelligence.graph_tool import RepoGraphQueryTool
from cairn.runtime.evals.intelligence.models import Arm, IntelligenceMetrics
from cairn.runtime.evals.intelligence.runner import IntelligenceRunner, SafeTraceSink
from cairn.runtime.evals.models import EvalCase, EvalStatus, EvalSuiteCase
from tests.support.runtime import SequenceLLM


def simple_spec(*, stale: bool = False) -> IntelligenceCase:
    files = {"parser.py": "def parse():\n    return 1\n"}
    facts: tuple[MemoryFixture, ...] = (
        MemoryFixture(
            "parser",
            "Parser exposes parse and returns an integer.",
            ("parser.py",),
            True,
        ),
    )
    current: tuple[tuple[str, str], ...] = (
        (("parser.py", "def parse():\n    return 2\n"),) if stale else ()
    )
    if stale:
        facts += (
            MemoryFixture(
                "partial",
                "Parser partial claim needs a dependency review.",
                ("parser.py",),
                True,
                complete=False,
            ),
        )
    case = EvalCase(name="parser", prompt="Inspect parser contract.", files=files)
    return IntelligenceCase(
        EvalSuiteCase(
            case,
            (
                FileContentEqualsCheck(
                    "parser.py", current[0][1] if stale else files["parser.py"]
                ),
            ),
        ),
        facts,
        ("parser.py",),
        current,
    )


def make_runner(
    spec: IntelligenceCase, arm: Arm, llm: SequenceLLM, tmp_path: Path, **kwargs: Any
) -> IntelligenceRunner:
    observations = IntelligenceMetrics()
    return IntelligenceRunner(
        lambda: llm,
        spec=spec,
        arm=arm,
        observations=observations,
        budget=RunBudget(max_steps=3),
        run_timeout_seconds=10,
        check_timeout_seconds=2,
        temp_root=tmp_path,
        tracer=Tracer(SafeTraceSink(tmp_path / "traces", observations)),
        **kwargs,
    )


def test_graph_refresh_completeness_and_read_only(tmp_path: Path) -> None:
    file = tmp_path / "parser.py"
    file.write_text("def old_name():\n    pass\n")
    tool = RepoGraphQueryTool(Workspace(tmp_path))
    tool.initialize()
    before = file.read_bytes()
    result = asyncio.run(
        tool.execute({"operation": "find_symbols", "name": "old_name"})
    )
    assert json.loads(result.stdout)["items"][0]["name"] == "old_name"
    assert file.read_bytes() == before
    file.write_text("def new_name():\n    pass\n")
    data = json.loads(
        asyncio.run(
            tool.execute({"operation": "find_symbols", "name": "new_name"})
        ).stdout
    )
    assert data["items"][0]["name"] == "new_name" and data["freshness"] == "rebuilt"
    tool.builder.limits = BuildLimits(max_entries=1)
    retained = tool.builder.snapshot
    data = json.loads(asyncio.run(tool.execute({"operation": "list_files"})).stdout)
    assert not data["snapshot_complete"] and data["items"] == []
    assert data["freshness"] == "unknown"
    assert tool.builder.snapshot is retained
    assert retained is not None and retained.report.complete


def test_dependency_target_is_graph_discovery_once_without_read(
    tmp_path: Path,
) -> None:
    files = {
        "source.py": "import target\nfrom target import value\n",
        "target.py": "value = 1\n",
    }
    spec = IntelligenceCase(
        EvalSuiteCase(
            EvalCase(name="dependency", prompt="Inspect dependency.", files=files),
            (FileContentEqualsCheck("target.py", files["target.py"]),),
        ),
        (),
        ("target.py",),
        (),
    )
    llm = SequenceLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="dependencies",
                        name="repo_graph",
                        arguments={
                            "operation": "dependencies_of",
                            "path": "source.py",
                        },
                    )
                ]
            ),
            LLMResponse(content="done"),
        ]
    )
    runner = make_runner(spec, Arm.GRAPH, llm, tmp_path)
    assert asyncio.run(runner.run_case()).status is EvalStatus.PASS
    metrics = runner.observations
    assert metrics.read_file_calls == 0
    assert metrics.graph_queries_completed == 1
    assert [
        (evidence.path, evidence.tool) for evidence in metrics.direct_discovery_evidence
    ] == [("target.py", "repo_graph")]


@pytest.mark.parametrize(
    "arguments",
    [
        {"operation": "bash", "command": "pwd"},
        {"operation": "list_files", "command": "pwd"},
        {"operation": "module_for_path", "path": "../escape"},
        {"operation": "dependencies_of", "path": ".git/config"},
        {"operation": "list_files", "limit": True},
        {"operation": "list_files", "limit": 21},
        {"operation": "module_for_path"},
        {"operation": "find_symbols", "name": "bad\nname"},
    ],
)
def test_graph_access_contract(tmp_path: Path, arguments: dict[str, Any]) -> None:
    with pytest.raises(InvalidArguments):
        asyncio.run(RepoGraphQueryTool(Workspace(tmp_path)).execute(arguments))
    assert not list(tmp_path.iterdir())


def test_four_arm_git_identity_and_feature_isolation(tmp_path: Path) -> None:
    runs = []
    for arm in Arm:
        llm = SequenceLLM([LLMResponse(content="done")])
        runner = make_runner(simple_spec(), arm, llm, tmp_path)
        result = asyncio.run(runner.run_case())
        assert result.status is EvalStatus.PASS
        metrics = runner.observations
        runs.append(metrics)
        messages, tools = llm.calls[0]
        assert tools is not None
        assert {schema["function"]["name"] for schema in tools} == (
            {"bash", "read_file", "edit_file"}
            | ({"repo_graph"} if arm.graph else set())
        )
        assert (
            any('"project_memory"' in (message.content or "") for message in messages)
            is arm.memory
        )
        assert metrics.memory_included_events == int(arm.memory)
        assert (
            metrics.provider_input_tokens is None
            and metrics.provider_output_tokens is None
        )
        assert metrics.request_tokens_total > 0
        if runner.memory_directory:
            assert not Path(runner.memory_directory.name).exists()
        assert not list(tmp_path.glob("cairn-eval-*"))
    assert len({metrics.project_id for metrics in runs}) == 4
    assert len({metrics.initial_tree for metrics in runs}) == 1
    assert runs[1].request_tokens_total > runs[0].request_tokens_total
    assert runs[2].request_tokens_total > runs[0].request_tokens_total
    assert set(runs[2].memory_included_ids).isdisjoint(runs[3].memory_included_ids)


@pytest.mark.parametrize("arm", [Arm.MEMORY, Arm.BOTH])
def test_stale_unknown_memory_never_enters_request(tmp_path: Path, arm: Arm) -> None:
    llm = SequenceLLM([LLMResponse(content="done")])
    runner = make_runner(simple_spec(stale=True), arm, llm, tmp_path)
    result = asyncio.run(runner.run_case())
    assert result.status is EvalStatus.PASS
    assert runner.observations.memory_freshness_counts == {"stale": 1, "unknown": 1}
    assert runner.observations.memory_included_events == 0
    assert runner.observations.stale_recall_rate == 0
    assert not any(
        '"project_memory"' in (message.content or "") for message in llm.calls[0][0]
    )


class PressureCounter:
    def count(self, messages: list[Message], tools: list[dict[str, Any]]) -> TokenCount:
        return TokenCount(
            100
            + sum(
                1000 if '"project_memory"' in (m.content or "") else 1 for m in messages
            ),
            True,
        )


def test_optional_memory_budget_drops_are_observed(tmp_path: Path) -> None:
    llm = SequenceLLM([LLMResponse(content="done")])
    runner = make_runner(
        simple_spec(),
        Arm.MEMORY,
        llm,
        tmp_path,
        context_budget=ContextBudget(max_tokens=512, response_tokens=128),
        token_counter=PressureCounter(),
    )
    assert asyncio.run(runner.run_case()).status is EvalStatus.PASS
    metrics = runner.observations
    assert metrics.memory_selected_events == metrics.memory_budget_drop_events == 1
    assert metrics.memory_included_events == 0
    assert metrics.context_trimmed_turns == 0


def test_trace_diagnostics_are_sanitized(tmp_path: Path) -> None:
    sink = SafeTraceSink(tmp_path, IntelligenceMetrics())
    span = Span(
        context=new_trace_context(),
        start_time=datetime.now(UTC),
        name="llm.generate",
        status=SpanStatus.ERROR,
        error="password=secret",
        attributes={
            "tool": "secret-tool",
            "payload": "credential",
            "context_tokens_after": 10,
        },
    )
    sink.emit(span)
    contents = next(tmp_path.glob("*.jsonl")).read_text()
    assert (
        "password" not in contents
        and "credential" not in contents
        and "secret-tool" not in contents
    )
    assert "context_tokens_after" in contents


def test_cancellation_cleans_workspace_and_memory(tmp_path: Path) -> None:
    entered = asyncio.Event()
    directories: list[Path] = []

    class WaitingLLM(SequenceLLM):
        async def generate(
            self, messages: list[Message], tools: list[dict[str, Any]] | None = None
        ) -> LLMResponse:
            entered.set()
            await asyncio.Event().wait()
            return LLMResponse(content="unreachable")

    class CapturingRunner(IntelligenceRunner):
        async def _prepare_workspace(
            self, workspace: Workspace, case: EvalCase
        ) -> None:
            directories.append(workspace.root)
            await super()._prepare_workspace(workspace, case)
            assert self.memory_directory is not None
            directories.append(Path(self.memory_directory.name))

    runner = CapturingRunner(
        lambda: WaitingLLM([]),
        spec=simple_spec(),
        arm=Arm.BOTH,
        observations=IntelligenceMetrics(),
        budget=RunBudget(max_steps=2),
        run_timeout_seconds=10,
        check_timeout_seconds=2,
        temp_root=tmp_path,
    )

    async def cancel() -> None:
        task = asyncio.create_task(runner.run_case())
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel())
    assert len(directories) == 2 and all(not path.exists() for path in directories)
