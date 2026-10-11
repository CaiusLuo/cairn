"""Dedicated experimental adapter around EvalRunner's existing lifecycle."""

import os
import shlex
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any, Literal

from cairn.core.agent import Agent
from cairn.core.context import ContextRequest
from cairn.core.events import Event, EventHandler
from cairn.core.models import Message, ToolResult
from cairn.evals.intelligence.checks import BehaviorCheck
from cairn.evals.intelligence.fixtures import (
    IntelligenceCase,
    approve_fixtures,
    initialize_git,
    materialize_current,
)
from cairn.evals.intelligence.graph_tool import RepoGraphQueryTool
from cairn.evals.intelligence.models import Arm, DiscoveryEvidence, IntelligenceMetrics
from cairn.evals.models import EvalCase, EvalMetrics, EvalResult
from cairn.evals.runner import EvalRunner, _MeasuredContextBuilder
from cairn.llm.base import LLMClient
from cairn.memory.context import MemoryContext
from cairn.memory.freshness import FreshnessChecker
from cairn.memory.identity import ProjectIdentity, git_read
from cairn.memory.models import Freshness
from cairn.memory.retrieval import MemoryReader
from cairn.observability.models import Span
from cairn.observability.sinks import JsonlTraceSink
from cairn.tools.base import Tool, ToolExecutionContext
from cairn.tools.bash import BashTool
from cairn.workspace.workspace import Workspace


class SafeTraceSink:
    """Persist IDs/counts/timings only, including on arbitrary provider failures."""

    def __init__(self, root: Path, metrics: IntelligenceMetrics) -> None:
        self.sink = JsonlTraceSink(root)
        self.metrics = metrics

    def emit(self, span: Span) -> None:
        if span.name == "agent.turn" and span.end_time is not None:
            self.metrics.agent_elapsed_seconds = (
                span.end_time - span.start_time
            ).total_seconds()
        allowed = {
            "step",
            "tool_schema_count",
            "message_count",
            "context_tokens_after",
            "context_count_is_estimate",
            "omitted_turns",
            "omitted_messages",
            "memory_selected_ids",
            "memory_included_ids",
            "memory_dropped_ids",
            "memory_tokens",
            "memory_checked_count",
            "memory_freshness_counts",
            "memory_read_error",
            "exit_code",
            "cancelled",
            "input_tokens",
            "output_tokens",
            "max_steps",
        }
        attributes = {
            key: value for key, value in span.attributes.items() if key in allowed
        }
        if "tool" in span.attributes:
            tool = span.attributes["tool"]
            attributes["tool"] = (
                tool
                if tool in {"bash", "read_file", "edit_file", "repo_graph"}
                else "unknown_tool"
            )
        self.sink.emit(
            span.model_copy(
                update={
                    "attributes": attributes,
                    "error": "Operation failed." if span.error else None,
                }
            )
        )


class ObservedMemoryReader(MemoryReader):
    def __init__(self, *args: Any, metrics: IntelligenceMetrics, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.metrics = metrics
        self.selected_freshness: dict[str, Freshness] = {}

    async def retrieve(self, query: str) -> MemoryContext:
        started = perf_counter()
        try:
            context = await super().retrieve(query)
            self.metrics.memory_candidates_checked += context.checked_count
            self.metrics.memory_read_errors += int(context.read_error)
            self.metrics.memory_size_drop_events += context.dropped_for_size
            for state, count in context.freshness_counts:
                counts = self.metrics.memory_freshness_counts
                counts[state.value] = counts.get(state.value, 0) + count
            self.selected_freshness = {}
            for item in context.items:
                # Independent bounded observation labels actual selected items,
                # rather than assuming a fixture stays fresh after Agent edits.
                result = await FreshnessChecker(self.resolver).check(
                    self.store.inspect(item.id)
                )
                self.selected_freshness[item.id] = result.status
            return context
        finally:
            self.metrics.memory_retrieval_freshness_seconds += perf_counter() - started


class ObservedContextBuilder(_MeasuredContextBuilder):
    def __init__(
        self, runner: "IntelligenceRunner", metrics: IntelligenceMetrics
    ) -> None:
        super().__init__(metrics, runner.context_budget, runner.token_counter)
        self.observed = metrics
        self.runner = runner

    def build(
        self,
        *,
        system_messages: list[Message],
        history: list[Message],
        current_turn_start: int,
        tools: list[dict[str, Any]],
        project_memory: MemoryContext | None = None,
    ) -> ContextRequest:
        request = super().build(
            system_messages=system_messages,
            history=history,
            current_turn_start=current_turn_start,
            tools=tools,
            project_memory=project_memory,
        )
        metrics = self.observed
        metrics.request_token_counts.append(request.tokens_after)
        metrics.request_tokens_total += request.tokens_after.tokens
        metrics.memory_selected_events += len(request.memory_selected_ids)
        metrics.memory_included_events += len(request.memory_included_ids)
        metrics.memory_budget_drop_events += len(request.memory_dropped_ids)
        for field, ids in (
            ("memory_selected_ids", request.memory_selected_ids),
            ("memory_included_ids", request.memory_included_ids),
            ("memory_dropped_ids", request.memory_dropped_ids),
        ):
            setattr(metrics, field, sorted(set(getattr(metrics, field)) | set(ids)))
        for entry_id in request.memory_included_ids:
            reader = self.runner.reader
            state = (
                reader.selected_freshness.get(entry_id, Freshness.UNKNOWN)
                if reader
                else Freshness.UNKNOWN
            )
            if state is not Freshness.FRESH:
                metrics.memory_ineligible_included_events += 1
            elif self.runner.relevant_memories.get(entry_id, False):
                metrics.memory_relevant_included_events += 1
        return request


class ObservedTool:
    def __init__(
        self, tool: Tool, observe: Callable[[dict[str, Any], ToolResult], None]
    ) -> None:
        self.tool = tool
        self.name = tool.name
        self.description = tool.description
        self.observe = observe

    def schema(self) -> dict[str, Any]:
        return self.tool.schema()

    def validate(self, arguments: dict[str, Any]) -> None:
        self.tool.validate(arguments)

    async def execute(
        self, arguments: dict[str, Any], *, context: ToolExecutionContext | None = None
    ) -> ToolResult:
        result = await self.tool.execute(arguments, context=context)
        self.observe(arguments, result)
        return result


class IntelligenceRunner(EvalRunner):
    def __init__(
        self,
        *args: Any,
        spec: IntelligenceCase,
        arm: Arm,
        observations: IntelligenceMetrics,
        tool_timeout_seconds: float = 10,
        memory_temp_root: Path | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.spec = spec
        self.arm = arm
        self.observations = observations
        self.tool_timeout_seconds = tool_timeout_seconds
        self.memory_temp_root = memory_temp_root
        self.memory_directory: TemporaryDirectory[str] | None = None
        self.reader: ObservedMemoryReader | None = None
        self.relevant_memories: dict[str, bool] = {}
        self.read_paths: set[str] = set()

    async def run_case(self) -> EvalResult:
        started = perf_counter()
        try:
            checks = tuple(
                replace(check, secret_env_keys=self.secret_env_keys)
                if isinstance(check, BehaviorCheck)
                else check
                for check in self.spec.item.checks
            )
            return await super().run(
                self.spec.item.case, checks=checks, metrics=self.observations
            )
        finally:
            if self.memory_directory is not None:
                self.memory_directory.cleanup()
            metrics = self.observations
            metrics.elapsed_seconds = perf_counter() - started
            ineligible = sum(
                count
                for state, count in metrics.memory_freshness_counts.items()
                if state != Freshness.FRESH.value
            )
            metrics.stale_recall_rate = (
                metrics.memory_ineligible_included_events / ineligible
                if ineligible
                else None
            )
            metrics.relevant_memory_precision = (
                metrics.memory_relevant_included_events / metrics.memory_included_events
                if metrics.memory_included_events
                else None
            )

    async def _prepare_workspace(self, workspace: Workspace, case: EvalCase) -> None:
        started = perf_counter()
        await super()._prepare_workspace(workspace, case)
        await initialize_git(workspace)
        identity = await ProjectIdentity.inspect(workspace)
        self.observations.project_id = identity.project_id
        if self.arm.memory:
            memory_started = perf_counter()
            # HOME default is outside workspace/TMPDIR/uv-cache writable roots.
            # MemoryStore checks actual overlaps; it fails closed on custom roots.
            self.memory_directory = TemporaryDirectory(
                prefix=".cairn-eval-memory-", dir=self.memory_temp_root or Path.home()
            )
            store, resolver, relevant = await approve_fixtures(
                self.spec,
                workspace,
                Path(self.memory_directory.name),
                sensitive_values=tuple(
                    os.environ[key]
                    for key in self.secret_env_keys
                    if os.environ.get(key)
                ),
            )
            self.reader = ObservedMemoryReader(
                store, resolver, metrics=self.observations
            )
            self.relevant_memories = relevant
            self.observations.memory_fixture_entries = len(relevant)
            self.observations.memory_fixture_seconds = perf_counter() - memory_started
        await materialize_current(self.spec, workspace)
        self.observations.initial_tree = (
            (await git_read(workspace, "rev-parse", "HEAD^{tree}")).decode().strip()
        )
        self.observations.fixture_setup_seconds = perf_counter() - started

    def _discover(
        self, paths: tuple[str, ...], tool: Literal["read_file", "repo_graph"]
    ) -> None:
        existing = {item.path for item in self.observations.direct_discovery_evidence}
        for path in sorted(set(paths) & set(self.spec.relevant_files) - existing):
            self.observations.direct_discovery_evidence.append(
                DiscoveryEvidence(
                    path=path, tool=tool, step=self.observations.agent_steps_used or 0
                )
            )

    def _observe_graph(
        self, paths: tuple[str, ...], elapsed: float, complete: bool
    ) -> None:
        self.observations.graph_build_refresh_seconds += elapsed
        self.observations.graph_incomplete_builds += int(not complete)
        self._discover(paths, "repo_graph")

    def _observe_read(self, arguments: dict[str, Any], result: ToolResult) -> None:
        path = arguments.get("path")
        if result.exit_code == 0 and isinstance(path, str):
            self.observations.repeated_read_file_calls += int(path in self.read_paths)
            self.read_paths.add(path)
            self._discover((path,), "read_file")

    def _build_agent(
        self,
        workspace: Workspace,
        llm: LLMClient,
        metrics: EvalMetrics,
        event_handler: EventHandler,
    ) -> Agent:
        def observe(event: Event) -> None:
            if event.type == "tool_call":
                self.observations.tool_calls += 1
                tool = event.data.get("tool")
                if tool == "read_file":
                    self.observations.read_file_calls += 1
                elif tool == "repo_graph":
                    self.observations.graph_query_calls += 1
                elif tool == "bash":
                    self.observations.bash_calls += 1
                    command = event.data.get("arguments", {}).get("command", "")
                    try:
                        words = shlex.split(command) if isinstance(command, str) else []
                    except ValueError:
                        words = []
                    self.observations.bash_discovery_calls += int(
                        any(
                            Path(word).name in {"rg", "grep", "find", "ls", "tree"}
                            for word in words
                        )
                    )
            elif event.type == "tool_error":
                self.observations.tool_error_events += 1
            elif event.type == "tool_denied":
                self.observations.tool_denied_events += 1
            elif event.type == "tool_result":
                self.observations.tool_nonzero_results += int(
                    event.data.get("exit_code") != 0
                )
                if event.data.get("tool") == "repo_graph":
                    self.observations.graph_queries_completed += int(
                        event.data.get("exit_code") == 0
                    )
                elif event.data.get("tool") == "bash":
                    # BashTool's own fixed timeout envelope, not model text.
                    self.observations.bash_timeout_results += int(
                        event.data.get("exit_code") == -1
                        and str(event.data.get("stderr", "")).endswith(
                            f" time out after {self.tool_timeout_seconds}s"
                        )
                    )
            event_handler(event)

        agent = super()._build_agent(workspace, llm, metrics, observe)
        bash = agent.tools.get_tool("bash")
        assert isinstance(bash, BashTool)
        bash.timeout = self.tool_timeout_seconds
        read = agent.tools.get_tool("read_file")
        agent.tools.tools["read_file"] = ObservedTool(read, self._observe_read)
        agent.context_builder = ObservedContextBuilder(self, self.observations)
        agent.memory_reader = self.reader
        if self.arm.graph:
            graph_tool = RepoGraphQueryTool(workspace, observer=self._observe_graph)
            graph_tool.initialize()
            agent.tools.register_tool(graph_tool)
        return agent
