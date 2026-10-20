"""Run the fixed compaction cases offline and assert what must survive.

Deterministic and provider-free: a scripted client supplies the tool calls and
the summary, so every assertion here is about Cairn's own behaviour. Real-model
effect is a separate measurement and must not be folded into these checks.

Run it directly::

    uv run python -m evaluation.runners.run_context_compaction
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from evaluation.cases.context_compaction import (
    CaseFacts,
    CompactionCase,
    offline_compaction_cases,
    tool_facts,
)

from cairn.core.budget import RunBudget
from cairn.core.compaction.summarizer import SummaryResult
from cairn.core.context import ContextBudget, ContextBuilder, TokenCount
from cairn.core.models import LLMResponse, Message, ToolResult
from cairn.infra.tools.base import ToolExecutionContext
from cairn.infra.tools.registry import ToolRegistry
from cairn.runtime.agent import Agent
from cairn.runtime.loop import run_turn


class CharCounter:
    """One token per character, so a budget is a readable number."""

    def count(self, messages: list[Message], tools: list[dict[str, Any]]) -> TokenCount:
        return TokenCount(
            tokens=sum(len(message.content or "") for message in messages),
            is_estimate=False,
        )


class ScriptedLLM:
    def __init__(self, responses: Sequence[LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def generate(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None
    ) -> LLMResponse:
        self.calls += 1
        return self._responses.pop(0)


class CountingSummarizer:
    def __init__(self) -> None:
        self.calls = 0

    async def summarize(
        self, messages: list[Message], *, max_tokens: int
    ) -> SummaryResult:
        self.calls += 1
        return SummaryResult(text="earlier work summarized")


class OutputTool:
    """A tool whose only job is to emit a large result."""

    name = "big"
    description = "Return a large deterministic result."

    def __init__(self, output: str) -> None:
        self.output = output
        self.runs = 0

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description}

    def validate(self, arguments: dict[str, Any]) -> None:
        pass

    async def execute(
        self, arguments: dict[str, Any], *, context: ToolExecutionContext | None = None
    ) -> ToolResult:
        self.runs += 1
        return ToolResult(stdout=self.output, exit_code=0)


@dataclass(frozen=True, slots=True)
class CaseResult:
    name: str
    passed: bool
    failures: tuple[str, ...]
    facts: CaseFacts


def run_case(case: CompactionCase) -> CaseResult:
    """Execute one case and check the facts that must survive."""
    llm = ScriptedLLM(case.responses)
    summarizer = CountingSummarizer()
    tool = OutputTool(case.tool_output)
    tools = ToolRegistry()
    tools.register_tool(tool)
    agent = Agent(
        llm=llm,
        tools=tools,
        system_prompt=case.system_prompt,
        context_builder=ContextBuilder(
            budget=ContextBudget(max_tokens=case.budget_tokens, response_tokens=100),
            counter=CharCounter(),
        ),
        summarizer=summarizer,
    )
    for older in case.older_history:
        agent.state.add_user_message(older)
        agent.state.add_assistant_message("acknowledged")

    answer = asyncio.run(
        run_turn(agent, case.current_instruction, budget=RunBudget(max_steps=20))
    )
    facts = CaseFacts(
        answer=answer,
        tool_run_count=tool.runs,
        conversation=tuple(agent.state.messages),
        summary_calls=summarizer.calls,
    )
    return CaseResult(
        name=case.name,
        passed=not _failures(case, facts),
        failures=tuple(_failures(case, facts)),
        facts=facts,
    )


def _failures(case: CompactionCase, facts: CaseFacts) -> list[str]:
    problems: list[str] = []
    if facts.answer != "finished":
        problems.append(f"turn did not finish: {facts.answer!r}")
    if facts.tool_run_count != case.expected_tool_runs:
        problems.append(
            f"tool ran {facts.tool_run_count} times, "
            f"expected {case.expected_tool_runs} (no replay allowed)"
        )
    problems.extend(_conversation_failures(case, facts))
    return problems


def _conversation_failures(case: CompactionCase, facts: CaseFacts) -> list[str]:
    problems: list[str] = []
    conversation = list(facts.conversation)
    if not conversation:
        return ["conversation is empty after the turn"]
    if conversation[-1].role != "assistant" or conversation[-1].content != "finished":
        problems.append("the final assistant answer is not the last message")
    instructions = [
        message.content
        for message in conversation
        if message.role == "user" and message.content == case.current_instruction
    ]
    if not instructions:
        problems.append(
            "the current instruction is missing from the conversation verbatim"
        )
    facts_seen = tool_facts(facts.conversation)
    if len(facts_seen) != case.expected_tool_runs:
        problems.append(
            f"{len(facts_seen)} tool facts recorded, expected {case.expected_tool_runs}"
        )
    for message in facts_seen:
        if not message.content:
            problems.append("a recorded tool fact lost its content")
    return problems


def run_all() -> list[CaseResult]:
    return [run_case(case) for case in offline_compaction_cases()]


def main() -> int:
    results = run_all()
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"{status}  {result.name}")
        for failure in result.failures:
            print(f"      - {failure}")
        print(
            f"      turns={result.facts.tool_run_count} "
            f"summary_calls={result.facts.summary_calls} "
            f"messages={len(result.facts.conversation)}"
        )
    failed = [result for result in results if not result.passed]
    print(f"\n{len(results) - len(failed)}/{len(results)} cases passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
