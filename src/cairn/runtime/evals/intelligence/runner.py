"""Dedicated experimental adapter around EvalRunner's existing lifecycle."""

import os
import shlex
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any, Literal, cast

from cairn.core.context import ContextBuilder, ContextRequest
from cairn.core.events import Event, EventHandler
from cairn.core.llm import LLMClient
from cairn.core.memory.context import MemoryContext
from cairn.core.memory.models import Freshness
from cairn.core.models import LLMResponse, Message, ToolResult
from cairn.core.permissions import CapabilityRequest
from cairn.infra.memory.identity import ProjectIdentity, git_read
from cairn.infra.observability.models import Span
from cairn.infra.observability.sinks import JsonlTraceSink
from cairn.infra.tools.base import Tool, ToolExecutionContext
from cairn.infra.tools.bash import BashTool
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.agent import Agent
from cairn.runtime.evals.intelligence.checks import BehaviorCheck
from cairn.runtime.evals.intelligence.fixtures import (
    IntelligenceCase,
    approve_fixtures,
    initialize_git,
    materialize_current,
)
from cairn.runtime.evals.intelligence.graph_tool import RepoGraphQueryTool
from cairn.runtime.evals.intelligence.models import (
    Arm,
    DiscoveryEvidence,
    IntelligenceMetrics,
)
from cairn.runtime.evals.models import EvalCase, EvalCheck, EvalMetrics, EvalResult
from cairn.runtime.evals.runner import EvalRunner
from cairn.runtime.memory.freshness import FreshnessChecker
from cairn.runtime.memory.retrieval import MemoryReader

#: A provider failure is labelled for metrics only; the order decides the label.
#: A timeout wins over a status code, which wins over a malformed response.
_STATUS_CATEGORIES: dict[int, str] = {
    400: "invalid_request",
    401: "authentication",
    403: "authentication",
    429: "rate_limit",
}


def _failure_category(exc: Exception, status: object) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    if type(status) is int:
        known = _STATUS_CATEGORIES.get(status)
        if known is not None:
            return known
        if 500 <= status <= 599:
            return "service_unavailable"
    if isinstance(exc, ValueError):
        return "invalid_response_or_request"
    return "unknown"


class ObservedLLM:
    def __init__(self, client: LLMClient, metrics: IntelligenceMetrics) -> None:
        self.client = client
        self.metrics = metrics
        self.model = getattr(client, "model", None)
        self.max_output_tokens = getattr(client, "max_output_tokens", None)

    async def generate(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None
    ) -> LLMResponse:
        self.metrics.provider_requests += 1
        try:
            response = await self.client.generate(messages, tools)
        except Exception as exc:
            self.metrics.provider_failures += 1
            status = getattr(exc, "status_code", None)
            category = _failure_category(exc, status)
            counts = self.metrics.provider_failure_categories
            counts[category] = counts.get(category, 0) + 1
            raise RuntimeError("Model request failed.") from None
        self.metrics.provider_responses += 1
        return response


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
                if tool
                in {
                    "bash",
                    "read_file",
                    "edit_file",
                    "write_file",
                    "patch_file",
                    "glob",
                    "grep",
                    "repo_graph",
                }
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


class ObservedContextBuilder(ContextBuilder):
    """Delegate to the runner's measured builder and add Memory/Context counters.

    Composition, not inheritance: the measured builder is an implementation the
    base class chooses through ``measured_context_builder``, so the observations
    added here do not depend on that concrete class staying subclassable.
    """

    def __init__(
        self,
        runner: "IntelligenceRunner",
        metrics: IntelligenceMetrics,
        measured: ContextBuilder,
    ) -> None:
        # The measured builder already applies the budget and counter contract;
        # mirror them so anything reading this builder sees the same policy.
        super().__init__(budget=measured.budget, counter=measured.counter)
        self.observed = metrics
        self.runner = runner
        self.measured = measured

    def build(self, **kwargs: Any) -> ContextRequest:
        # The measured builder records the base Context counters; this class only
        # adds the observations it owns, so it forwards the whole admission.
        request = self.measured.build(**kwargs)
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
            reader = self.runner.memory_reader
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

    def requested_capability(
        self, arguments: dict[str, Any]
    ) -> "CapabilityRequest | None":
        report = getattr(self.tool, "requested_capability", None)
        if not callable(report):
            return None
        return cast("CapabilityRequest | None", report(arguments))

    async def execute(
        self, arguments: dict[str, Any], *, context: ToolExecutionContext | None = None
    ) -> ToolResult:
        result = await self.tool.execute(arguments, context=context)
        self.observe(arguments, result)
        return result


class IntelligenceRunner(EvalRunner):
    metrics_type = IntelligenceMetrics

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
        self.memory_reader: ObservedMemoryReader | None = None
        self.relevant_memories: dict[str, bool] = {}
        self.read_paths: set[str] = set()

    def _release_memory_directory(self) -> None:
        directory, self.memory_directory = self.memory_directory, None
        if directory is not None:
            directory.cleanup()

    async def run(
        self,
        case: EvalCase,
        *,
        checks: Sequence[EvalCheck],
        metrics: EvalMetrics | None = None,
    ) -> EvalResult:
        """Use the retained metrics by default; EvalRunner owns their lifecycle."""
        return await super().run(
            case,
            checks=checks,
            metrics=self.observations if metrics is None else metrics,
        )

    def bind_metrics(self, metrics: EvalMetrics) -> None:
        assert isinstance(metrics, IntelligenceMetrics)
        self.observations = metrics
        self.read_paths.clear()
        self.memory_reader = None
        self.relevant_memories = {}
        if self.tracer is not None and isinstance(self.tracer.sink, SafeTraceSink):
            self.tracer.sink.metrics = metrics

    def finalize_metrics(self, metrics: EvalMetrics) -> None:
        assert isinstance(metrics, IntelligenceMetrics)
        try:
            self._release_memory_directory()
        finally:
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

    async def run_case(self) -> EvalResult:
        checks = tuple(
            replace(check, secret_env_keys=self.secret_env_keys)
            if isinstance(check, BehaviorCheck)
            else check
            for check in self.spec.item.checks
        )
        return await self.run(
            self.spec.item.case, checks=checks, metrics=self.observations
        )

    async def prepare_workspace(self, workspace: Workspace, case: EvalCase) -> None:
        started = perf_counter()
        await super().prepare_workspace(workspace, case)
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
            self.memory_reader = ObservedMemoryReader(
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

    def measured_context_builder(self, metrics: EvalMetrics) -> ContextBuilder:
        """Wrap the base builder so Memory decisions are observed per request."""
        assert isinstance(metrics, IntelligenceMetrics)
        return ObservedContextBuilder(
            self, metrics, super().measured_context_builder(metrics)
        )

    def build_agent(
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

        # The caller may have instrumented its factory with an old record. Keep
        # exactly one provider observer and bind it to this case's canonical one.
        while isinstance(llm, ObservedLLM):
            llm = llm.client
        agent = super().build_agent(
            workspace, ObservedLLM(llm, self.observations), metrics, observe
        )
        bash = agent.tools.get_tool("bash")
        assert isinstance(bash, BashTool)
        bash.timeout = self.tool_timeout_seconds
        read = agent.tools.get_tool("read_file")
        agent.tools.override_tool(ObservedTool(read, self._observe_read))
        if self.arm.graph:
            graph_tool = RepoGraphQueryTool(workspace, observer=self._observe_graph)
            graph_tool.initialize()
            agent.tools.register_tool(graph_tool)
        return agent
