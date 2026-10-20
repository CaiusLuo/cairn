"""The compaction measurement and the live request share one system context.

A request opens with the Agent's system prompt plus whatever its configuration
contributes: a durable recovery notice, the repository context and the skills
advertisement. ``run_compaction`` measures a request to decide whether a manual
compaction is worth doing, so it must measure that same prefix. These tests pin
the contract through observable behaviour: the messages the hook counts are the
messages the model is sent, in the same order, for every contributing
configuration.
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from cairn.core.context import ContextBudget, ContextBuilder, TokenCount
from cairn.core.models import LLMResponse, Message
from cairn.infra.git.context import RepoContextProvider, RepositoryContext
from cairn.infra.skills import SkillLibrary
from cairn.infra.tools.registry import ToolRegistry
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.agent import Agent
from cairn.runtime.durable_session import DurableSession
from cairn.runtime.loop import run_turn
from cairn.runtime.turn.hook import run_compaction
from tests.support.compaction import CharacterCounter, StubSummarizer
from tests.support.durable_sessions import create_session, resolution
from tests.support.runtime import TEST_BUDGET, SequenceLLM
from tests.support.sessions import SESSION, store

SYSTEM_PROMPT = "sys"
SKILL = """\
---
name: reporting
description: Write a status report for a stakeholder.
---

Report body.
"""


class RecordingCounter(CharacterCounter):
    """Character counting that keeps every message list it measured."""

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    def count(self, messages: list[Message], tools: list[dict[str, Any]]) -> TokenCount:
        self.calls.append(list(messages))
        return super().count(messages, tools)


class FixedRepoProvider(RepoContextProvider):
    """A repository provider whose inspection is deterministic and counted."""

    def __init__(self, root: Path) -> None:
        super().__init__(Workspace(root))
        self.calls = 0
        self.context = RepositoryContext(
            workspace_root=root,
            repository_root=root,
            branch="main",
            dirty=True,
            changed_files=("changed.py",),
            untracked_files=("new.py",),
        )

    async def inspect(self) -> RepositoryContext:
        self.calls += 1
        return self.context


@dataclass(frozen=True)
class Rig:
    """One Agent plus the doubles that let a test see what it was sent."""

    agent: Agent
    llm: SequenceLLM
    counter: RecordingCounter
    provider: FixedRepoProvider | None


def write_skill(root: Path) -> SkillLibrary:
    directory = root / "reporting"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(SKILL, encoding="utf-8")
    return SkillLibrary(root)


def make_rig(
    root: Path,
    *,
    repo_context: bool,
    skills: bool,
    workspace: Path | None = None,
) -> Rig:
    counter = RecordingCounter()
    provider = FixedRepoProvider(workspace or root) if repo_context else None
    llm = SequenceLLM([LLMResponse(content="done")])
    agent = Agent(
        llm=llm,
        tools=ToolRegistry(),
        system_prompt=SYSTEM_PROMPT,
        context_builder=ContextBuilder(
            budget=ContextBudget(max_tokens=100_000, response_tokens=100),
            counter=counter,
        ),
        repo_context_provider=provider,
        skill_library=write_skill(root / "skills") if skills else None,
        # Compaction is available; these cases never need a summary.
        summarizer=StubSummarizer(),
    )
    return Rig(agent=agent, llm=llm, counter=counter, provider=provider)


def leading_system_messages(messages: list[Message]) -> list[Message]:
    """The system prefix a request opened with.

    Agent conversation state never holds system messages, so the leading
    system-role run is exactly the context the request was opened with.
    """
    prefix: list[Message] = []
    for message in messages:
        if message.role != "system":
            break
        prefix.append(message)
    return prefix


def expected_system_messages(
    rig: Rig,
    *,
    repo_context: bool,
    skills: bool,
    notice: str | None = None,
) -> list[Message]:
    """The provider order the loop owns: prompt, notice, repository, skills."""
    expected = [Message(role="system", content=rig.agent.system_prompt)]
    if notice is not None:
        expected.append(Message(role="system", content=notice))
    if repo_context:
        assert rig.provider is not None
        expected.append(
            Message(role="system", content=rig.provider.context.to_prompt())
        )
    if skills:
        assert rig.agent.skill_library is not None
        expected.append(
            Message(
                role="system",
                content="\n".join(rig.agent.skill_library.prompt_lines()),
            )
        )
    return expected


def measured_request(rig: Rig) -> list[Message]:
    """The full message list the first measurement of a compaction counted."""
    assert rig.counter.calls, "the compaction measured nothing"
    return rig.counter.calls[0]


def assert_hook_measured_the_sent_request(
    rig: Rig, *, sent_system: list[Message], history: list[Message]
) -> None:
    """The manual compaction counted exactly the request the loop sent."""
    rig.counter.calls.clear()
    outcome = asyncio.run(run_compaction(rig.agent))

    measured = measured_request(rig)
    assert measured[: len(sent_system)] == sent_system
    assert measured[len(sent_system) :] == history

    # The reported measurement is that same request, not a smaller one.
    expected = CharacterCounter().count([*sent_system, *history], []).tokens
    assert outcome.tokens_before == expected


@pytest.mark.parametrize(
    ("repo_context", "skills"),
    [(False, False), (True, False), (False, True), (True, True)],
)
def test_the_hook_measures_the_system_context_the_loop_sends(
    tmp_path: Path, repo_context: bool, skills: bool
) -> None:
    rig = make_rig(tmp_path, repo_context=repo_context, skills=skills)

    assert asyncio.run(run_turn(rig.agent, "first task", budget=TEST_BUDGET)) == "done"

    sent = rig.llm.calls[0][0]
    sent_system = leading_system_messages(sent)
    assert sent_system == expected_system_messages(
        rig, repo_context=repo_context, skills=skills
    )

    assert_hook_measured_the_sent_request(
        rig, sent_system=sent_system, history=rig.agent.state.messages
    )


def test_skills_and_repository_context_are_both_in_the_measured_request(
    tmp_path: Path,
) -> None:
    """A configuration the old hook under-measured now matches the request."""
    rig = make_rig(tmp_path, repo_context=True, skills=True)

    asyncio.run(run_turn(rig.agent, "first task", budget=TEST_BUDGET))

    sent_system = leading_system_messages(rig.llm.calls[0][0])
    assert len(sent_system) == 3, "prompt, repository context and skills"
    assert sent_system[1].content == rig.provider.context.to_prompt()  # type: ignore[union-attr]

    rig.counter.calls.clear()
    outcome = asyncio.run(run_compaction(rig.agent))

    measured_system = leading_system_messages(measured_request(rig))
    assert measured_system == sent_system
    # The difference is real: the old hook measured only the first two.
    assert (
        outcome.tokens_before
        > CharacterCounter()
        .count(sent_system[:2] + rig.agent.state.messages, [])
        .tokens
    )


def test_a_durable_recovery_notice_is_measured_by_the_hook(tmp_path: Path) -> None:
    """A durable notice is in the request, so compaction must count it too.

    The notice sits between the prompt and the repository context. A manual
    compaction that is not given the Session still has one owner to read it
    from: the Agent's owning Session.
    """
    work = tmp_path / "work"
    work.mkdir()
    first = make_rig(tmp_path, repo_context=True, skills=True, workspace=work)
    api, session = create_session(tmp_path, first.agent)

    try:
        assert (
            asyncio.run(
                run_turn(first.agent, "first task", budget=TEST_BUDGET, session=session)
            )
            == "done"
        )
        saved = api.inspect(SESSION)
    finally:
        session.close()

    reopened_rig = make_rig(tmp_path, repo_context=True, skills=True, workspace=work)
    reopened = DurableSession.reopen(
        store(tmp_path), SESSION, agent=reopened_rig.agent, binding=saved.binding
    )
    try:
        report = reopened.recovery_report
        reopened.reconcile(report, host_resolution=resolution(report))
        # Read before the turn: the notice describes what the next request inherits.
        notice = reopened.recovery_notice
        assert notice is not None

        assert (
            asyncio.run(
                run_turn(
                    reopened_rig.agent,
                    "new task",
                    budget=TEST_BUDGET,
                    session=reopened,
                )
            )
            == "done"
        )

        sent_system = leading_system_messages(reopened_rig.llm.calls[0][0])
        assert sent_system == expected_system_messages(
            reopened_rig, repo_context=True, skills=True, notice=notice
        )

        assert_hook_measured_the_sent_request(
            reopened_rig,
            sent_system=sent_system,
            history=reopened_rig.agent.state.messages,
        )
    finally:
        reopened.close()


def test_one_repository_inspection_per_request(tmp_path: Path) -> None:
    """Sharing the builder must not add a second inspection per request."""
    rig = make_rig(tmp_path, repo_context=True, skills=False)
    assert rig.provider is not None

    asyncio.run(run_turn(rig.agent, "first task", budget=TEST_BUDGET))
    assert rig.provider.calls == 1

    asyncio.run(run_compaction(rig.agent))
    assert rig.provider.calls == 2, "one further request, one further inspection"
