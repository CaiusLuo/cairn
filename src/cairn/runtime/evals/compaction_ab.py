"""Real-summary vs full-context continuation comparison for one compaction case.

The offline scripted suite (``evaluation/cases/context_compaction.py`` and
``evaluation/runners/run_context_compaction.py``) already proves Cairn's own
compaction *mechanism*. This module answers a different, opt-in question that only
a real model can answer: when a task's conversation is continued from a **real
model summary** instead of the **full context**, does it behave as well?

It is deliberately an *orchestrator*, not a second execution engine. Both arms run
through the one lifecycle owner, ``runtime.loop.run_turn``, in independent
throwaway workspaces built from the **same pre-compaction snapshot**.

Two integrity properties are enforced here, not merely reported:

* **A hard total token budget.** When a budget is supplied, one shared ledger
  caps the FULL_CONTEXT arm, the REAL_SUMMARY arm *and* every summarizer call
  together. Each provider request reserves conservatively (an upper-bound prompt
  estimate plus the output cap) *before* it is sent; if the budget cannot cover
  the reservation the call is refused before reaching the provider. Unknown usage
  is never treated as zero.
* **A real comparison requires real compaction.** A run is only *valid* when the
  summary arm actually invoked the summarizer **and** a later request used the
  compacted history, the current instruction survived verbatim, key tool facts
  survived, and a non-idempotent side effect was not replayed. Otherwise the run
  is marked invalid — never a successful comparison.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any

from cairn.core.budget import RunBudget
from cairn.core.compaction.summarizer import Summarizer
from cairn.core.context import ContextBudget, ContextBuilder, TokenCount, TokenCounter
from cairn.core.events import EventHandler
from cairn.core.llm import LLMClient
from cairn.core.models import LLMResponse, Message, ToolResult
from cairn.core.permissions import PermissionHandler
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.assembly import build_agent
from cairn.runtime.evals.models import EvalCheck, EvalFailureCategory
from cairn.runtime.loop import run_turn

#: The system-policy marker a summarizer request carries; used to tell a summary
#: provider call apart from a model-continuation call when accounting budget.
_SUMMARY_MARKER = "summarizing one conversation slice"


class Arm(StrEnum):
    """The two continuation strategies compared on identical inputs."""

    FULL_CONTEXT = "full_context"
    REAL_SUMMARY = "real_summary"


class FailureKind(StrEnum):
    """Why an arm did not end within budget; ``NONE`` means it ended."""

    NONE = "none"
    RUN_TIMEOUT = "run_timeout"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CONTEXT_EXCEEDED = "context_exceeded"
    TOTAL_BUDGET_EXCEEDED = "total_budget_exceeded"
    RUNTIME_ERROR = "runtime_error"
    CANCELLED = "cancelled"
    SETUP_ERROR = "setup_error"


class TotalBudgetExceeded(Exception):
    """A provider request was refused before sending: it cannot be afforded."""

    def __init__(self, *, needed: int, remaining: int) -> None:
        self.needed = needed
        self.remaining = remaining
        super().__init__(
            f"total token budget would be exceeded: next request needs "
            f"{needed} tokens but only {remaining} remain"
        )


@dataclass(slots=True)
class TokenBudgetLedger:
    """A conservative hard cap shared across both arms and summarizer calls.

    The budget is **withheld before a request is sent** (``withhold``), so a
    request that is attempted but whose response is lost or that fails/cancels
    still consumes its reservation rather than counting as zero. ``settle`` runs
    exactly once per request: with an exact, known actual cost it refunds the
    unused part of the reservation; with unknown usage, an estimate-degraded
    counter, a failure or a cancellation it keeps the full reservation
    (conservative). Every request therefore settles at most once.
    """

    total: int
    spent: int = 0
    unknown_usage: bool = False
    estimate_degraded: bool = False
    requests_settled: int = 0

    def __post_init__(self) -> None:
        if self.total <= 0:
            raise ValueError("total token budget must be positive")

    @property
    def remaining(self) -> int:
        return self.total - self.spent

    def withhold(self, estimate: int, output_cap: int) -> int:
        """Reserve (and charge) before sending; raise if it cannot be afforded."""
        need = max(0, estimate) + max(0, output_cap)
        if need > self.remaining:
            raise TotalBudgetExceeded(needed=need, remaining=self.remaining)
        self.spent += need
        return need

    def settle(self, withheld: int, *, actual: int | None, is_estimate: bool) -> None:
        """Settle one request exactly once, conservatively when cost is unknown."""
        self.requests_settled += 1
        if actual is None or is_estimate:
            # Keep the full reservation; never treat unknown/degraded cost as zero.
            if is_estimate:
                self.estimate_degraded = True
            else:
                self.unknown_usage = True
            return
        refund = withheld - actual
        if refund >= 0:
            self.spent -= refund
        else:
            # Estimate under-counted; charge the real cost (may push remaining
            # negative, which fail-closes subsequent requests).
            self.spent += -refund


class _CharCounter:
    """One token per character plus one per message.

    A deterministic *proxy* used by offline doubles only (it is a character
    heuristic, not a model tokenizer). ``include_tools`` adds the tool-schema
    characters so a budget reservation cannot silently omit tool tokens; the
    live runner instead supplies ``LiteLLMTokenCounter`` (the real per-model
    tokenizer, which includes tools).
    """

    def __init__(self, *, include_tools: bool = False) -> None:
        self.include_tools = include_tools

    def count(self, messages: list[Message], tools: list[dict[str, Any]]) -> TokenCount:
        message_chars = sum(len(message.content or "") + 1 for message in messages)
        tool_chars = (
            len(json.dumps(tools, sort_keys=True))
            if self.include_tools and tools
            else 0
        )
        return TokenCount(tokens=message_chars + tool_chars, is_estimate=False)


def _is_summary_request(messages: Sequence[Message]) -> bool:
    return any(
        message.role == "system" and _SUMMARY_MARKER in (message.content or "")
        for message in messages
    )


class BudgetedLLM:
    """Wrap an ``LLMClient`` and charge every request against a shared ledger.

    The reservation happens before the provider call, so an unaffordable request
    is refused before it is sent. Requests are recorded so the comparison can
    prove that a later request used compacted history.
    """

    def __init__(
        self,
        client: LLMClient,
        ledger: TokenBudgetLedger | None,
        counter: TokenCounter,
        output_cap: int | None,
    ) -> None:
        self.client = client
        self.model = getattr(client, "model", None)
        fallback_cap = getattr(client, "max_output_tokens", None)
        self.max_output_tokens = (
            output_cap if isinstance(output_cap, int) else fallback_cap
        )
        self._ledger = ledger
        self._counter = counter
        self._output_cap = (
            self.max_output_tokens if isinstance(self.max_output_tokens, int) else 0
        )
        #: (is_summary, messages) for each provider call, in order.
        self.requests: list[tuple[bool, tuple[Message, ...]]] = []
        self._input_total: int | None = 0
        self._output_total: int | None = 0

    def model_requests(self) -> list[tuple[Message, ...]]:
        return [tuple(m) for is_summary, m in self.requests if not is_summary]

    def summary_request_count(self) -> int:
        return sum(1 for is_summary, _ in self.requests if is_summary)

    def usage_totals(self) -> tuple[int | None, int | None]:
        return self._input_total, self._output_total

    async def generate(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None
    ) -> LLMResponse:
        is_summary = _is_summary_request(messages)
        counted = self._counter.count(messages, tools or [])
        if self._ledger is not None:
            withheld = self._ledger.withhold(counted.tokens, self._output_cap)
        else:
            withheld = 0
        self.requests.append((is_summary, tuple(messages)))
        try:
            response = await self.client.generate(messages, tools)
        except BaseException:
            # Attempted request whose response was lost / failed / cancelled: keep
            # the reservation (a real send may have been billed) and re-raise.
            if self._ledger is not None:
                self._ledger.settle(
                    withheld, actual=None, is_estimate=counted.is_estimate
                )
            raise
        usage = response.usage
        known = (
            usage is not None
            and usage.input_tokens is not None
            and usage.output_tokens is not None
        )
        actual = (
            usage.input_tokens + usage.output_tokens
            if known
            and usage is not None
            and usage.input_tokens is not None
            and usage.output_tokens is not None
            else None
        )
        if self._ledger is not None:
            self._ledger.settle(
                withheld, actual=actual, is_estimate=counted.is_estimate
            )
        if usage is not None and usage.input_tokens is not None:
            self._input_total = (
                None
                if self._input_total is None
                else self._input_total + usage.input_tokens
            )
        else:
            self._input_total = None
        if usage is not None and usage.output_tokens is not None:
            self._output_total = (
                None
                if self._output_total is None
                else self._output_total + usage.output_tokens
            )
        else:
            self._output_total = None
        return response


@dataclass(frozen=True, slots=True)
class CompactionCaseSpec:
    """One pre-snapshot task plus the facts that must survive either arm.

    ``files`` and ``pre_side_effects`` materialise the identical starting
    workspace in both arms. ``history`` seeds the prior conversation (complete
    tool-call/result groups and a user correction). ``forbidden_history_marker``
    is text that a real summary is expected to compress away; ``key_fact_marker``
    is a fact that must still reach the model after compaction.
    """

    name: str
    prompt: str
    files: dict[str, str]
    current_instruction: str
    pre_side_effects: dict[str, str] = field(default_factory=dict)
    #: Prior conversation seeded before ``run_turn``; must be whole tool groups.
    history: tuple[Message, ...] = ()
    #: Backward-compatible simple history: each entry becomes a user+ack pair.
    older_history: tuple[str, ...] = ()
    budget_tokens: int = 8_000
    #: Tighter context budget for the REAL_SUMMARY arm to force compaction.
    summary_budget_tokens: int | None = None
    response_tokens: int = 1_024
    system_prompt: str = "sys"
    #: Text a summary should compress away; absent from a compacted request.
    forbidden_history_marker: str | None = None
    #: Fact that must survive compaction into a later request (generic).
    key_fact_marker: str | None = None
    #: A fact available ONLY from a tool result; a faithful summary must keep it.
    tool_fact_marker: str | None = None
    #: A user correction present only in older history; must survive compaction.
    correction_marker: str | None = None
    #: Version string of this sample (its history/markers/behaviour contract).
    version: str = "compaction-sample-v2"
    #: Expected continuation runs of the side-effecting tool (None = unasserted).
    expected_side_effect_runs: int | None = None
    #: Name of the non-idempotent side-effect tool and the log it appends to.
    side_effect_tool: str = "apply_side_effect"
    side_effect_log: str | None = None


@dataclass(slots=True)
class ArmObservation:
    """What one arm actually did; unknown values stay ``None``."""

    arm: Arm
    answer: str | None = None
    ended_within_budget: bool | None = None
    failure: FailureKind = FailureKind.NONE
    error: str | None = None
    checks: tuple[EvalCheck, ...] = ()
    checks_passed: bool | None = None
    check_results: dict[str, bool] = field(default_factory=dict)
    tool_run_count: int | None = None
    side_effect_runs: int | None = None
    instruction_reached_model: bool | None = None
    summary_calls: int | None = None
    provider_input_tokens: int | None = None
    provider_output_tokens: int | None = None
    elapsed_seconds: float | None = None
    model_identifier: str | None = None
    model_version: str | None = None
    #: Integrity observations.
    compaction_occurred: bool | None = None
    compacted_history_used: bool | None = None
    key_fact_preserved: bool | None = None
    tool_fact_preserved: bool | None = None
    correction_preserved: bool | None = None
    summary_text_used: bool | None = None
    replayed_side_effect: bool | None = None
    request_count: int | None = None
    tokens_spent: int | None = None
    usage_unknown: bool | None = None


@dataclass(slots=True)
class ComparisonReport:
    """Both arms for one case, with correctness and termination kept separate."""

    case_name: str
    full_context: ArmObservation
    real_summary: ArmObservation
    both_ended_within_budget: bool | None = None
    final_workspace_correct: dict[str, bool | None] = field(default_factory=dict)
    #: Whether this is a *valid* full-vs-summary comparison (real compaction etc).
    valid: bool | None = None
    invalid_reasons: list[str] = field(default_factory=list)
    #: Identical starting state both arms must share.
    initial_snapshot_digest: str = ""
    total_token_budget: int | None = None
    tokens_spent_total: int | None = None
    usage_unknown: bool | None = None
    notes: list[str] = field(default_factory=list)


def _check_name(check: EvalCheck) -> str:
    label = getattr(check, "check_type", None)
    return label if isinstance(label, str) and label.strip() else type(check).__name__


def _snapshot_digest(spec: CompactionCaseSpec) -> str:
    payload = {
        "files": dict(sorted(spec.files.items())),
        "pre_side_effects": dict(sorted(spec.pre_side_effects.items())),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class _RecordingSummarizer:
    """Wrap a summarizer to record produced texts, so validity can prove the
    summary was actually substituted into a later request."""

    def __init__(self, inner: Summarizer) -> None:
        self.inner = inner
        self.calls: list[int] = []
        self.texts: list[str] = []

    async def summarize(self, messages: list[Message], *, max_tokens: int):  # type: ignore[no-untyped-def]
        # Count the attempt before awaiting, so a failed/timeout summary is still
        # counted as an attempted (and charged) call.
        self.calls.append(1)
        result = await self.inner.summarize(messages, max_tokens=max_tokens)
        if getattr(result, "ok", False) and getattr(result, "text", ""):
            self.texts.append(result.text)
        return result


class _SideEffectTrackingTool:
    """A non-idempotent tool: it appends one entry to a log each time it runs.

    Appending (not overwriting) makes a replayed side effect observable: a second
    run changes the log, so an overwrite that looks identical cannot hide a
    replay.
    """

    def __init__(self, name: str, workspace: Workspace, log_path: str | None) -> None:
        self.name = name
        self.description = "Append one deployment entry to the log (not idempotent)."
        self._workspace = workspace
        self._log_path = log_path
        self.runs = 0

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description}

    def validate(self, arguments: dict[str, Any]) -> None:
        pass

    async def execute(self, arguments: dict[str, Any], **_: Any) -> ToolResult:
        self.runs += 1
        if self._log_path is not None:
            path = self._workspace.resolve_path(self._log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write("applied\n")
        return ToolResult(stdout="applied", exit_code=0)


class _BigOutputTool:
    """A read-only tool returning a large deterministic output.

    Used by the continuation to create enough tokens in the current turn that the
    budget forces compaction of older history (folding alone cannot shrink it).
    """

    def __init__(self, output: str) -> None:
        self.name = "inspect"
        self.description = "Return a large deterministic inspection result."
        self._output = output
        self.runs = 0

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description}

    def validate(self, arguments: dict[str, Any]) -> None:
        pass

    async def execute(self, arguments: dict[str, Any], **_: Any) -> ToolResult:
        self.runs += 1
        return ToolResult(stdout=self._output, exit_code=0)


def _seed_history(agent: Any, spec: CompactionCaseSpec) -> None:
    if spec.history:
        for message in spec.history:
            agent.state.messages.append(message.model_copy(deep=True))
        return
    for older in spec.older_history:
        agent.state.add_user_message(older)
        agent.state.add_assistant_message("acknowledged")


async def _run_arm(
    *,
    spec: CompactionCaseSpec,
    arm: Arm,
    llm_factory: Callable[[], LLMClient],
    summarizer_factory: Callable[[LLMClient], Summarizer | None] | None,
    checks: Sequence[EvalCheck],
    budget: RunBudget,
    ledger: TokenBudgetLedger | None,
    run_timeout_seconds: float,
    temp_root: Path | None,
    permission_handler: PermissionHandler | None,
    event_handler: EventHandler | None,
    token_counter: TokenCounter | None,
    compacted_marker_guard: str | None,
) -> ArmObservation:
    observation = ArmObservation(arm=arm, checks=tuple(checks))
    started = perf_counter()
    # The ContextBuilder keeps the messages-only proxy (calibrated admission);
    # the budget ledger reserves over messages *and* tool schemas.
    counter = token_counter or _CharCounter()
    reserve_counter = token_counter or _CharCounter(include_tools=True)
    arm_context_budget = (
        spec.summary_budget_tokens
        if arm is Arm.REAL_SUMMARY and spec.summary_budget_tokens is not None
        else spec.budget_tokens
    )
    spent_before = ledger.spent if ledger is not None else 0
    with TemporaryDirectory(prefix=f"cairn-cmp-{arm.value}-", dir=temp_root) as root:
        workspace = Workspace(Path(root))
        try:
            for raw_path, text in spec.files.items():
                path = workspace.resolve_path(raw_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            for raw_path, text in spec.pre_side_effects.items():
                path = workspace.resolve_path(raw_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
        except Exception as exc:
            observation.failure = FailureKind.SETUP_ERROR
            observation.error = f"{type(exc).__name__}: {exc}"
            observation.ended_within_budget = False
            return observation

        base_llm = llm_factory()
        model = getattr(base_llm, "model", None)
        if isinstance(model, str):
            observation.model_identifier = model
        wrapped = BudgetedLLM(base_llm, ledger, reserve_counter, spec.response_tokens)
        summarizer = summarizer_factory(wrapped) if summarizer_factory else None
        recorder = _RecordingSummarizer(summarizer) if summarizer is not None else None
        summarizer_for_agent = recorder if recorder is not None else summarizer

        builder = ContextBuilder(
            budget=ContextBudget(
                max_tokens=arm_context_budget, response_tokens=spec.response_tokens
            ),
            counter=counter,
        )
        agent = build_agent(
            workspace=workspace,
            llm=wrapped,
            permission_handler=permission_handler,
            event_handler=event_handler,
            tracer=None,
            context_builder=builder,
            summarizer=summarizer_for_agent,
        )
        agent.system_prompt = spec.system_prompt
        side_effect_tool = _SideEffectTrackingTool(
            spec.side_effect_tool, workspace, spec.side_effect_log
        )
        agent.tools.register_tool(side_effect_tool)
        agent.tools.register_tool(_BigOutputTool("I" * 700))
        _seed_history(agent, spec)

        deadline = asyncio.timeout(run_timeout_seconds)
        try:
            async with deadline:
                observation.answer = await run_turn(
                    agent, spec.current_instruction, budget=budget
                )
            observation.ended_within_budget = True
        except asyncio.CancelledError:
            observation.failure = FailureKind.CANCELLED
            observation.error = "CancelledError"
            observation.ended_within_budget = False
            raise
        except TimeoutError as exc:
            observation.ended_within_budget = False
            if deadline.expired():
                observation.failure = FailureKind.RUN_TIMEOUT
                observation.error = f"TimeoutError: {exc}"
            else:
                observation.failure = FailureKind.RUNTIME_ERROR
                observation.error = f"TimeoutError: {exc}"
        except TotalBudgetExceeded as exc:
            observation.ended_within_budget = False
            observation.failure = FailureKind.TOTAL_BUDGET_EXCEEDED
            observation.error = f"TotalBudgetExceeded: {exc}"
        except Exception as exc:
            observation.ended_within_budget = False
            observation.error = f"{type(exc).__name__}: {exc}"
            name = type(exc).__name__
            if name == "RunBudgetExceeded":
                observation.failure = FailureKind.BUDGET_EXHAUSTED
            elif name == "ContextBudgetExceeded":
                observation.failure = FailureKind.CONTEXT_EXCEEDED
            else:
                observation.failure = FailureKind.RUNTIME_ERROR

        observation.tool_run_count = side_effect_tool.runs
        observation.side_effect_runs = side_effect_tool.runs
        observation.instruction_reached_model = any(
            message.role == "user" and message.content == spec.current_instruction
            for message in agent.state.messages
        )
        observation.summary_calls = _summary_calls(summarizer_for_agent, wrapped)
        observation.elapsed_seconds = perf_counter() - started
        observation.request_count = len(wrapped.requests)
        input_total, output_total = wrapped.usage_totals()
        observation.provider_input_tokens = input_total
        observation.provider_output_tokens = output_total
        if ledger is not None:
            observation.tokens_spent = ledger.spent - spent_before
            observation.usage_unknown = ledger.unknown_usage

        _observe_integrity(spec, observation, wrapped, recorder)

        try:
            observation.check_results = await _run_checks(
                checks, workspace, run_timeout_seconds
            )
            observation.checks_passed = all(observation.check_results.values())
        except Exception as exc:
            observation.checks_passed = False
            observation.error = (
                f"{observation.error}; check error: {type(exc).__name__}"
                if observation.error
                else f"check error: {type(exc).__name__}"
            )
    return observation


def _observe_integrity(
    spec: CompactionCaseSpec,
    observation: ArmObservation,
    wrapped: BudgetedLLM,
    recorder: _RecordingSummarizer | None = None,
) -> None:
    model_requests = wrapped.model_requests()
    observation.compaction_occurred = (
        bool(observation.summary_calls)
        if observation.summary_calls is not None
        else None
    )
    instruction = spec.current_instruction

    def _carries_instruction(request: tuple[Message, ...]) -> bool:
        return any(instruction in (message.content or "") for message in request)

    def _carries(request: tuple[Message, ...], marker: str) -> bool:
        return any(marker in (message.content or "") for message in request)

    # Requests that already carry the current instruction but no longer the large
    # older text (i.e. post-compaction / post-omission requests).
    compacted_requests = [
        request
        for request in model_requests
        if _carries_instruction(request)
        and not (
            spec.forbidden_history_marker is not None
            and _carries(request, spec.forbidden_history_marker)
        )
    ]
    targets = (
        compacted_requests
        if spec.forbidden_history_marker is not None
        else [request for request in model_requests if _carries_instruction(request)]
    )
    if spec.forbidden_history_marker is not None:
        observation.compacted_history_used = bool(compacted_requests)
    if spec.key_fact_marker is not None:
        observation.key_fact_preserved = any(
            _carries(request, spec.key_fact_marker) for request in targets
        )
    tool_marker = spec.tool_fact_marker or spec.key_fact_marker
    if tool_marker is not None:
        observation.tool_fact_preserved = any(
            _carries(request, tool_marker) for request in targets
        )
    if spec.correction_marker is not None:
        observation.correction_preserved = any(
            _carries(request, spec.correction_marker) for request in targets
        )
    if recorder is not None and recorder.texts:
        observation.summary_text_used = any(
            any(text[:40] in (message.content or "") for message in request)
            for text in recorder.texts
            for request in model_requests
        )
    if (
        spec.expected_side_effect_runs is not None
        and observation.side_effect_runs is not None
    ):
        observation.replayed_side_effect = (
            observation.side_effect_runs > spec.expected_side_effect_runs
        )


async def _run_checks(
    checks: Sequence[EvalCheck], workspace: Workspace, timeout: float
) -> dict[str, bool]:
    results: dict[str, bool] = {}
    for check in checks:
        async with asyncio.timeout(timeout):
            result = await check.evaluate(workspace)
        results[_check_name(check)] = bool(result.passed and result.error is None)
    return results


def _summary_calls(summarizer: Summarizer | None, wrapped: BudgetedLLM) -> int | None:
    calls = getattr(summarizer, "calls", None)
    if isinstance(calls, list):
        return len(calls)
    if isinstance(calls, int):
        return calls
    count = wrapped.summary_request_count()
    if summarizer is None and count == 0:
        return None
    return count


async def compare_case(
    spec: CompactionCaseSpec,
    *,
    llm_factory: Callable[[], LLMClient],
    checks: Sequence[EvalCheck] = (),
    budget: RunBudget,
    total_token_budget: int | None = None,
    run_timeout_seconds: float = 120.0,
    check_timeout_seconds: float = 10.0,
    temp_root: Path | None = None,
    permission_handler: PermissionHandler | None = None,
    event_handler: EventHandler | None = None,
    token_counter: TokenCounter | None = None,
    summarizer_factory: Callable[[LLMClient], Summarizer | None] | None = None,
) -> ComparisonReport:
    """Run both arms on the same snapshot in independent directories.

    ``FULL_CONTEXT`` uses ``spec.budget_tokens`` and no summarizer. ``REAL_SUMMARY``
    uses ``spec.summary_budget_tokens`` (tighter) and the supplied summarizer so
    the older history is actually replaced. When ``total_token_budget`` is set, one
    ledger caps both arms and summarizer calls together.
    """
    checks = tuple(checks)
    ledger = TokenBudgetLedger(total_token_budget) if total_token_budget else None
    digest = _snapshot_digest(spec)

    full = await _run_arm(
        spec=spec,
        arm=Arm.FULL_CONTEXT,
        llm_factory=llm_factory,
        summarizer_factory=None,
        checks=checks,
        budget=budget,
        ledger=ledger,
        run_timeout_seconds=run_timeout_seconds,
        temp_root=temp_root,
        permission_handler=permission_handler,
        event_handler=event_handler,
        token_counter=token_counter,
        compacted_marker_guard=None,
    )
    summary = await _run_arm(
        spec=spec,
        arm=Arm.REAL_SUMMARY,
        llm_factory=llm_factory,
        summarizer_factory=summarizer_factory or (lambda client: None),
        checks=checks,
        budget=budget,
        ledger=ledger,
        run_timeout_seconds=run_timeout_seconds,
        temp_root=temp_root,
        permission_handler=permission_handler,
        event_handler=event_handler,
        token_counter=token_counter,
        compacted_marker_guard=spec.forbidden_history_marker,
    )

    report = ComparisonReport(
        case_name=spec.name,
        full_context=full,
        real_summary=summary,
        initial_snapshot_digest=digest,
        total_token_budget=total_token_budget,
    )
    both_terminated = (
        full.ended_within_budget is True and summary.ended_within_budget is True
    )
    report.both_ended_within_budget = both_terminated
    if both_terminated:
        names = set(full.check_results) | set(summary.check_results)
        report.final_workspace_correct = {
            name: full.check_results.get(name) is True
            and summary.check_results.get(name) is True
            for name in sorted(names)
        }
    else:
        report.notes.append(
            "At least one arm did not end within budget; a fair full-vs-summary "
            "correctness comparison is not available."
        )

    if ledger is not None:
        report.tokens_spent_total = ledger.spent
        report.usage_unknown = ledger.unknown_usage

    report.invalid_reasons = _invalid_reasons(full, summary, both_terminated)
    report.valid = both_terminated and not report.invalid_reasons
    return report


def _invalid_reasons(
    full: ArmObservation, summary: ArmObservation, both_terminated: bool
) -> list[str]:
    reasons: list[str] = []
    if not both_terminated:
        for observation in (full, summary):
            if observation.ended_within_budget is not True:
                reasons.append(
                    f"{observation.arm.value} did not end within budget "
                    f"({observation.failure.value})"
                )
        return reasons
    if summary.compaction_occurred is not True:
        reasons.append(
            "REAL_SUMMARY did not invoke the summarizer; no compaction occurred, "
            "so this is not a full-vs-summary comparison"
        )
    if summary.compacted_history_used is False:
        reasons.append("no post-compaction request used the reduced history")
    if summary.key_fact_preserved is False:
        reasons.append("compaction dropped a key tool fact")
    if summary.tool_fact_preserved is False:
        reasons.append("compaction dropped a tool-only fact")
    if summary.correction_preserved is False:
        reasons.append("compaction dropped a pre-existing user correction")
    if summary.replayed_side_effect is True:
        reasons.append("REAL_SUMMARY replayed a non-idempotent side effect")
    return reasons


def failure_category(kind: FailureKind) -> EvalFailureCategory | None:
    """Map an arm failure to the eval failure vocabulary for reporting."""
    if kind is FailureKind.NONE:
        return None
    if kind is FailureKind.RUN_TIMEOUT:
        return EvalFailureCategory.RUN_TIMEOUT
    if kind in (FailureKind.BUDGET_EXHAUSTED, FailureKind.TOTAL_BUDGET_EXCEEDED):
        return EvalFailureCategory.BUDGET_EXHAUSTED
    return EvalFailureCategory.RUNTIME_ERROR
