import asyncio
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from litellm.exceptions import RateLimitError

from cairn.core.context import ContextBudget, ContextBuilder
from cairn.core.events import Event
from cairn.core.llm import LLMClient, LLMStreamChunk, ToolCallDelta
from cairn.core.model_manager import ModelConfig, ModelManager, ProviderConfig
from cairn.core.models import LLMResponse, LLMUsage, Message, ToolResult
from cairn.infra.observability.tracer import Tracer
from cairn.infra.sessions.codec import snapshot_messages
from cairn.infra.sessions.models import (
    GitBinding,
    PathIdentity,
    ProviderReference,
    SessionBinding,
    SessionStoreError,
)
from cairn.infra.tools.base import ToolExecutionContext
from cairn.infra.tools.registry import ToolRegistry
from cairn.runtime.agent import Agent, SessionAdmissionError
from cairn.runtime.durable_session import DurableSession, SessionDurabilityError
from cairn.runtime.lifecycle import await_owned
from cairn.runtime.loop import run_turn
from cairn.runtime.model_executor import ModelExecutor
from cairn.runtime.session import SessionState, TurnOutcome
from tests.support.durable_sessions import create_session, resolution
from tests.support.git import git, initialize_repository
from tests.support.runtime import (
    TEST_BUDGET,
    FailingLLM,
    FailingSink,
    RecordingSink,
    RecordingTool,
    SequenceLLM,
    make_agent,
    tool_response,
)
from tests.support.sessions import (
    SESSION,
    assert_competing_writer,
    binding,
    crash_at,
    store,
)
from tests.support.streaming import ScriptedStreamLLM, StreamBarrier


@pytest.mark.parametrize("after_tool", [False, True])
@pytest.mark.parametrize("close", [False, True])
def test_cancel_stream_preserves_only_accepted_facts_and_holds_writer_through_cleanup(
    tmp_path: Path, after_tool: bool, close: bool
) -> None:
    async def scenario() -> None:
        work = StreamBarrier(asyncio.Event(), asyncio.Event())
        cleanup = StreamBarrier(asyncio.Event(), asyncio.Event())
        partial = [
            LLMStreamChunk(text="provisional only"),
            LLMStreamChunk(
                tool_calls=(
                    ToolCallDelta(
                        0, id="unfinished", name="record", arguments='{"value":'
                    ),
                )
            ),
            work,
        ]
        scripts = (
            (
                [
                    LLMStreamChunk(
                        tool_calls=(
                            ToolCallDelta(
                                0, id="call-1", name="record", arguments="{}"
                            ),
                        )
                    ),
                    LLMStreamChunk(finish_reason="tool_calls"),
                ],
                partial,
            )
            if after_tool
            else (partial,)
        )
        llm = ScriptedStreamLLM(*scripts)
        tool = RecordingTool()
        registry = ToolRegistry()
        registry.register_tool(tool)
        events: list[Event] = []
        agent = Agent(
            llm,
            registry,
            event_handler=lambda e: events.append(e),
            tracer=Tracer(RecordingSink()),
        )
        api, session = create_session(tmp_path, agent)
        task = asyncio.create_task(
            run_turn(agent, "hello", budget=TEST_BUDGET, session=session)
        )
        try:
            await work.entered.wait()
            llm.cleanup_barrier = cleanup
            active = session.active_turn
            before = session.recovery_report
            task.cancel()
            await cleanup.entered.wait()
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done() and session.active_turn is active
                with pytest.raises(SessionAdmissionError):
                    await run_turn(agent, "busy", budget=TEST_BUDGET, session=session)
                report = session.recovery_report
                assert report == before and report.unresolved is not None
                with pytest.raises(SessionAdmissionError):
                    session.reconcile(report, host_resolution=resolution(report))
                with pytest.raises(SessionStoreError, match="turn_still_owned"):
                    api.close()
                assert_competing_writer(tmp_path, "writer_busy")
                assert not any(
                    e.type in ("turn_settled", "turn_committed") for e in events
                )
            if close:
                session.close()
                assert (
                    session.close_requested and session.state is SessionState.EXECUTING
                )
            cleanup.release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert llm.closed == (2 if after_tool else 1)
            assert len(tool.calls) == int(after_tool)
            assert session.active_turn is None
            assert (
                session.last_turn is not None
                and session.last_turn.outcome is TurnOutcome.CANCELLED
            )
            reader = store(tmp_path, mode="inspect") if close else api
            try:
                committed = reader.inspect(SESSION)
                assert committed.checkpoint.outcome == "cancelled"
                assert committed.checkpoint.messages == snapshot_messages(
                    agent.state.messages
                )
                assert committed.checkpoint.message_range.end == (
                    3 if after_tool else 0
                )
                assert "provisional only" not in repr(committed.model_dump())
                assert "unfinished" not in repr(committed.model_dump())
                assert events[-1].type == "turn_committed"
            finally:
                if close:
                    reader.close()
            if close:
                assert session.state is SessionState.CLOSED
                assert_competing_writer(tmp_path, "writer_acquired")
            assert not [
                other
                for other in asyncio.all_tasks()
                if other is not asyncio.current_task() and not other.done()
            ]
        finally:
            work.release.set()
            cleanup.release.set()
            if not task.done():
                task.cancel()
                await await_owned(task)
            session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("trace_fault", [False, True])
def test_cancel_tool_cleanup_releases_only_after_delivery(
    tmp_path: Path,
    trace_fault: bool,
) -> None:
    async def scenario() -> None:
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        observations: list[str] = []

        class HeldTool(RecordingTool):
            async def execute(
                self,
                arguments: dict[str, Any],
                *,
                context: ToolExecutionContext | None = None,
            ) -> ToolResult:
                (tmp_path / "work" / "effect").write_text("one")
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:

                    async def cleanup() -> None:
                        cleaning.set()
                        await release.wait()
                        observations.append("cleaned")

                    await await_owned(asyncio.create_task(cleanup()))
                raise AssertionError("cancelled")

        events: list[Event] = []
        agent = make_agent(SequenceLLM([tool_response()]), HeldTool())
        agent.tracer = Tracer(RecordingSink())
        api, session = create_session(tmp_path, agent)

        def observe(event: Event) -> None:
            events.append(event)
            if trace_fault and event.type == "trace_finish":
                raise RuntimeError("private finalization diagnostic")
            if event.type == "turn_settled":
                assert observations == ["cleaned"]
                if trace_fault:
                    with pytest.raises(SessionStoreError, match="turn_still_owned"):
                        api.close()
                report = session.recovery_report
                with pytest.raises(SessionAdmissionError):
                    session.reconcile(report, host_resolution=resolution(report))

        agent.event_handler = observe
        task = asyncio.create_task(
            run_turn(agent, "hello", budget=TEST_BUDGET, session=session)
        )
        try:
            await entered.wait()
            task.cancel()
            await cleaning.wait()
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and not observations
            with pytest.raises(SessionStoreError, match="turn_still_owned"):
                api.close()
            assert_competing_writer(tmp_path, "writer_busy")
            release.set()
            with pytest.raises(asyncio.CancelledError) as error:
                await task
            if trace_fault:
                assert error.value.__notes__ == [
                    "Turn trace finalization failed.",
                    "Durable session requires recovery.",
                ]
            report = session.recovery_report
            assert report.checkpoint.seq == (0 if trace_fault else 1)
            if trace_fault:
                assert report.unresolved is not None
                assert report.unresolved.diagnostics.reason == "finalization_failed"
            else:
                assert report.checkpoint.messages == snapshot_messages(
                    agent.state.messages
                )
                assert report.checkpoint.outcome == "cancelled"
            assert (tmp_path / "work" / "effect").read_text() == "one"
            assert (
                json.loads(agent.state.messages[-1].content or "")["type"]
                == "ToolCancelled"
            )
            assert session.state is (
                SessionState.RECOVERY_REQUIRED if trace_fault else SessionState.READY
            )
            assert any(e.type == "turn_committed" for e in events) is (not trace_fault)
            assert session.last_turn is not None
            assert not session.last_turn.settlement_delivery_failed
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await await_owned(task)
            session.close()
        assert_competing_writer(tmp_path, "writer_acquired")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure",
    ["sink", "trace_callback", "started_callback", "provider", "provider_and_trace"],
)
def test_primary_errors_trace_sink_vs_finalization_and_empty_rollback_checkpoints(
    tmp_path: Path, failure: str
) -> None:
    events: list[Event] = []

    def observe(event: Event) -> None:
        events.append(event)
        if (
            event.type == "trace_finish"
            and failure in ("trace_callback", "provider_and_trace")
        ) or (event.type == "turn_started" and failure == "started_callback"):
            raise RuntimeError("private trace callback")

    llm: LLMClient = (
        FailingLLM()
        if failure in ("provider", "provider_and_trace")
        else SequenceLLM(
            [
                LLMResponse(
                    content="done", usage=LLMUsage(input_tokens=4, output_tokens=2)
                )
            ]
        )
    )
    agent = Agent(
        llm,
        ToolRegistry(),
        event_handler=observe,
        tracer=Tracer(FailingSink() if failure == "sink" else RecordingSink()),
    )
    api, session = create_session(tmp_path, agent)
    try:
        if failure == "sink":
            assert (
                asyncio.run(
                    run_turn(agent, "hello", budget=TEST_BUDGET, session=session)
                )
                == "done"
            )
        else:
            with pytest.raises(RuntimeError) as error:
                asyncio.run(
                    run_turn(agent, "hello", budget=TEST_BUDGET, session=session)
                )
            if failure in ("provider", "provider_and_trace"):
                assert str(error.value) == "llm failed"
        report = api.inspect(SESSION)
        if failure == "sink":
            summary = report.checkpoint.trace
            assert summary.trace_id is not None and summary.persisted is False
            assert (summary.input_tokens, summary.output_tokens) == (4, 2)
            assert summary.error_code == "sink_failed"
            assert "simulated" not in repr(report.model_dump())
        elif failure == "provider":
            assert report.checkpoint.outcome == "runtime_error"
            assert report.checkpoint.messages == ()
            assert (
                report.checkpoint.message_range.start
                == report.checkpoint.message_range.end
                == 0
            )
            assert events[-1].type == "turn_committed"
        else:
            assert report.checkpoint.seq == 0 and report.unresolved is not None
            assert session.state is SessionState.RECOVERY_REQUIRED
            assert not any(e.type == "turn_committed" for e in events)
        assert not agent.state.messages or failure == "sink"
    finally:
        session.close()


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("primary", ["success", "provider", "cancelled"])
def test_uncertain_checkpoint_commit_inspects_exact_receipt_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, committed: bool, primary: str
) -> None:
    async def scenario() -> None:
        blocked = asyncio.Event()

        class EndLLM:
            def __init__(self) -> None:
                self.calls = 0

            async def generate(
                self, messages: list[Message], tools: list[dict[str, Any]] | None = None
            ) -> LLMResponse:
                self.calls += 1
                if self.calls == 1:
                    return tool_response()
                if primary == "provider":
                    raise RuntimeError("primary provider failure")
                if primary == "cancelled":
                    blocked.set()
                    await asyncio.Event().wait()
                return LLMResponse(content="done")

        llm = EndLLM()
        tool = RecordingTool()
        registry = ToolRegistry()
        registry.register_tool(tool)
        events: list[Event] = []
        agent = Agent(llm, registry, event_handler=lambda e: events.append(e))
        api, session = create_session(tmp_path, agent)
        original = api._commit
        commits = 0

        def uncertain(conn: sqlite3.Connection) -> None:
            nonlocal commits
            commits += 1
            if committed:
                conn.commit()
            raise sqlite3.OperationalError("private unknown acknowledgement")

        def observe(event: Event) -> None:
            events.append(event)
            if event.type == "tool_result":
                monkeypatch.setattr(api, "_commit", uncertain)
            if event.type == "turn_settled":
                with pytest.raises(SessionStoreError, match="turn_still_owned"):
                    api.close()

        agent.event_handler = observe
        task = asyncio.create_task(
            run_turn(agent, "hello", budget=TEST_BUDGET, session=session)
        )
        try:
            if primary == "cancelled":
                await blocked.wait()
                task.cancel()
            expected_error = (
                SessionDurabilityError
                if primary == "success"
                else (
                    asyncio.CancelledError if primary == "cancelled" else RuntimeError
                )
            )
            with pytest.raises(expected_error) as error:
                await task
            assert commits == 1 and llm.calls == 2 and len(tool.calls) == 1
            if primary != "success":
                assert error.value.__notes__ == ["Durable session requires recovery."]
            assert (
                len(agent.state.messages) >= 3
                and agent.state.messages[2].role == "tool"
            )
            report = session.recovery_report
            assert report.checkpoint.seq == int(committed)
            if committed:
                assert report.receipt == session.receipt
                assert report.checkpoint.messages == snapshot_messages(
                    agent.state.messages
                )
            assert session.state is SessionState.RECOVERY_REQUIRED
            assert not any(e.type == "turn_committed" for e in events)
            with pytest.raises(SessionAdmissionError):
                await run_turn(agent, "blocked", budget=TEST_BUDGET, session=session)
            monkeypatch.setattr(api, "_commit", original)
            session.reconcile(report, host_resolution=resolution(report))
            assert llm.calls == 2 and len(tool.calls) == 1
            assert snapshot_messages(agent.state.messages) == report.checkpoint.messages
        finally:
            monkeypatch.setattr(api, "_commit", original)
            session.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["sensitive_result", "checkpoint_io"])
def test_checkpoint_failure_keeps_live_tool_facts_but_never_acknowledges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    class EffectTool(RecordingTool):
        async def execute(
            self,
            arguments: dict[str, Any],
            *,
            context: ToolExecutionContext | None = None,
        ) -> ToolResult:
            self.calls.append(arguments)
            return ToolResult(
                stdout='{"password":"private-result"}'
                if failure == "sensitive_result"
                else "observed",
                exit_code=0,
            )

    tool = EffectTool()
    llm = SequenceLLM([tool_response(), LLMResponse(content="done")])
    agent = make_agent(llm, tool)
    bind = binding(tmp_path)
    api = store(tmp_path, sensitive_values=("private-result",))
    session = DurableSession.create(api, agent=agent, binding=bind)
    if failure == "checkpoint_io":

        def fail_checkpoint(*args: object) -> object:
            raise SessionStoreError("storage_failed")

        monkeypatch.setattr(api, "commit_turn", fail_checkpoint)
    try:
        with pytest.raises(SessionDurabilityError) as error:
            asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET, session=session))
        assert "private-result" not in str(error.value)
        report = session.recovery_report
        assert report.checkpoint.seq == 0 and report.unresolved is not None
        assert len(agent.state.messages) == 4 and len(tool.calls) == 1
        assert report.unresolved.diagnostics.fences[0].ordinal == 1
    finally:
        session.close()


@pytest.mark.parametrize("phase", ["turn_settled", "turn_committed"])
def test_post_receipt_callback_failure_keeps_receipt_and_executes_next_turn_once(
    tmp_path: Path, phase: str
) -> None:
    llm = SequenceLLM(
        [tool_response(), LLMResponse(content="done"), LLMResponse(content="next")]
    )
    tool = RecordingTool()
    agent = make_agent(llm, tool)
    api, session = create_session(tmp_path, agent)

    def observe(event: Event) -> None:
        if event.type == phase:
            assert api.inspect(SESSION).receipt == session.receipt
            assert session.active_turn is not None
            raise RuntimeError("observer private failure")

    agent.event_handler = observe
    try:
        assert (
            asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET, session=session))
            == "done"
        )
        first = session.receipt
        assert session.last_turn is not None
        assert session.last_turn.commit_delivery_failed is (phase == "turn_committed")
        assert session.last_turn.settlement_delivery_failed is (phase == "turn_settled")
        assert session.state is SessionState.READY
        assert (
            asyncio.run(run_turn(agent, "next", budget=TEST_BUDGET, session=session))
            == "next"
        )
        assert len(llm.calls) == 3 and len(tool.calls) == 1
        assert first.revision.head_seq == 1 and session.receipt.revision.head_seq == 2
    finally:
        session.close()


def test_process_death_reopen_requires_exact_explicit_abandonment_without_replay(
    tmp_path: Path,
) -> None:
    api = store(tmp_path)
    api.create(binding(tmp_path))
    api.close()
    crash_at(tmp_path, "fence")
    llm = SequenceLLM([LLMResponse(content="new request")])
    tool = RecordingTool()
    agent = make_agent(llm, tool)
    session = DurableSession.reopen(
        store(tmp_path), SESSION, agent=agent, binding=binding(tmp_path)
    )
    try:
        assert not llm.calls and not tool.calls and not agent.state.messages
        assert session.state is SessionState.RECOVERY_REQUIRED
        report = session.recovery_report
        assert report.status == "effects_unknown" and report.unresolved is not None
        with pytest.raises(SessionAdmissionError):
            asyncio.run(
                run_turn(
                    agent, "no automatic execution", budget=TEST_BUDGET, session=session
                )
            )
        wrong = resolution(report).model_copy(update={"turn_id": None})
        with pytest.raises(SessionStoreError):
            session.reconcile(report, host_resolution=wrong)
        report = session.recovery_report
        session.reconcile(report, host_resolution=resolution(report))
        assert not llm.calls and not tool.calls and not agent.state.messages
        assert (
            asyncio.run(
                run_turn(agent, "new input", budget=TEST_BUDGET, session=session)
            )
            == "new request"
        )
        systems = [m.content or "" for m in llm.calls[0][0] if m.role == "system"]
        assert any("Do not replay" in text for text in systems)
        unresolved = report.unresolved
        assert unresolved is not None
        abandoned_id = unresolved.intent.turn_id
        assert any(abandoned_id in text for text in systems)
    finally:
        session.close()


def test_streaming_fallback_keeps_candidate_admission_and_only_final_response(
    tmp_path: Path,
) -> None:
    first = ScriptedStreamLLM(
        [
            LLMStreamChunk(text="abandoned"),
            RateLimitError(message="rate", model="one", llm_provider="openai"),
        ]
    )
    second = ScriptedStreamLLM(
        [LLMStreamChunk(text="accepted"), LLMStreamChunk(finish_reason="stop")]
    )
    builder = ContextBuilder(
        budget=ContextBudget(max_tokens=8192, response_tokens=1024)
    )
    manager = ModelManager(
        ProviderConfig(
            "https://example.test", "KEY", (ModelConfig("group", ("one", "two")),)
        )
    )
    built: list[str] = []

    def runtime(model: str) -> tuple[LLMClient, ContextBuilder]:
        built.append(model)
        return second, builder

    agent = Agent(
        first,
        ToolRegistry(),
        context_builder=builder,
        model_executor=ModelExecutor(manager, runtime),
        provider_name="offline",
    )
    bind = binding(tmp_path)
    bind = bind.model_copy(
        update={
            "provider": ProviderReference(
                provider="offline",
                model_group="group",
                model_ids=("one", "two"),
                config_digest="0" * 64,
                system_prompt_digest=hashlib.sha256(
                    agent.system_prompt.encode()
                ).hexdigest(),
                context_budget=8192,
                output_reserve=1024,
                max_steps=TEST_BUDGET.max_steps,
            )
        }
    )
    api = store(tmp_path)
    session = DurableSession.create(api, agent=agent, binding=bind)
    try:
        assert (
            asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET, session=session))
            == "accepted"
        )
        assert built == ["two"] and len(first.calls) == len(second.calls) == 1
        assert first.closed == second.closed == 1
        assert [m.content for m in agent.state.messages] == ["hello", "accepted"]
        assert (
            snapshot_messages(agent.state.messages)
            == api.inspect(SESSION).checkpoint.messages
        )
    finally:
        session.close()


def test_git_binding_observation_at_settlement_and_reopen_without_git_dispatch(
    tmp_path: Path,
) -> None:
    initial = binding(tmp_path)
    work = tmp_path / "work"
    initialize_repository(work)
    calls: list[str] = []

    def identify(path: Path) -> PathIdentity:
        info = path.stat()
        return PathIdentity(
            path=str(path.resolve()), device=info.st_dev, inode=info.st_ino
        )

    def observe_binding() -> SessionBinding:
        calls.append("observed")
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        observed = GitBinding(
            repository=initial.workspace.root,
            common=identify(work / ".git"),
            admin=identify(work / ".git"),
            workspace_prefix="",
            registration_digest=hashlib.sha256(str(work).encode()).hexdigest(),
            head=git(work, "rev-parse", "HEAD"),
            branch=git(work, "symbolic-ref", "--short", "HEAD"),
        )
        return initial.model_copy(
            update={
                "workspace": initial.workspace.model_copy(update={"git": observed}),
                "observed_at_utc": now,
            }
        )

    class CommitTool(RecordingTool):
        async def execute(
            self,
            arguments: dict[str, Any],
            *,
            context: ToolExecutionContext | None = None,
        ) -> ToolResult:
            (work / "tracked.txt").write_text("after\n")
            git(work, "add", "tracked.txt")
            git(work, "commit", "-m", "tool mutation")
            return await super().execute(arguments, context=context)

    observed = observe_binding()
    old_head = observed.workspace.git.head if observed.workspace.git else None
    tool = CommitTool()
    llm = SequenceLLM([tool_response(), LLMResponse(content="done")])
    agent = make_agent(llm, tool)
    api = store(tmp_path)
    session = DurableSession.create(
        api, agent=agent, binding=observed, observe_binding=observe_binding
    )
    try:
        asyncio.run(run_turn(agent, "change", budget=TEST_BUDGET, session=session))
        saved = api.inspect(SESSION)
        assert saved.binding.workspace.git is not None
        assert saved.binding.workspace.git.head == git(work, "rev-parse", "HEAD")
        assert saved.binding.workspace.git.head != old_head
        assert saved.attempts[0].intent.binding.workspace.git is not None
        assert saved.attempts[0].intent.binding.workspace.git.head == old_head
        assert len(llm.calls) == 2 and len(tool.calls) == 1
        git(work, "checkout", "-b", "host-change")
        with pytest.raises(SessionAdmissionError):
            asyncio.run(
                run_turn(agent, "unapproved drift", budget=TEST_BUDGET, session=session)
            )
        assert (session.state,) == (SessionState.SUSPENDED,)
        assert len(llm.calls) == 2
        report = session.recovery_report
        host_resolution = resolution(report)
        live = observe_binding().model_copy(
            update={"observed_at_utc": host_resolution.resolved_at_utc}
        )
        session.reconcile(
            report, host_resolution=host_resolution.model_copy(update={"binding": live})
        )
        assert len(llm.calls) == 2 and session.state is SessionState.READY
        llm.responses.append(LLMResponse(content="new branch"))
        asyncio.run(
            run_turn(agent, "approved new work", budget=TEST_BUDGET, session=session)
        )
        saved = api.inspect(SESSION)
        assert saved.binding.workspace.git is not None
        assert saved.binding.workspace.git.branch == "host-change"
    finally:
        session.close()
    before = (
        git(work, "rev-parse", "HEAD"),
        (work / ".git" / "index").read_bytes(),
        (work / "tracked.txt").read_bytes(),
    )
    calls.clear()
    fresh_llm = SequenceLLM([])
    fresh = make_agent(fresh_llm)
    reopened = DurableSession.reopen(
        store(tmp_path),
        SESSION,
        agent=fresh,
        binding=saved.binding,
        observe_binding=observe_binding,
    )
    try:
        assert not calls and not fresh_llm.calls
        assert reopened.state is SessionState.SUSPENDED
        assert snapshot_messages(fresh.state.messages) == saved.checkpoint.messages
        assert before == (
            git(work, "rev-parse", "HEAD"),
            (work / ".git" / "index").read_bytes(),
            (work / "tracked.txt").read_bytes(),
        )
    finally:
        reopened.close()


def test_failed_recovery_read_retains_owned_lock_then_idle_inspection_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = RecordingTool()
    agent = make_agent(
        SequenceLLM([tool_response(), LLMResponse(content="done")]), tool
    )
    api, session = create_session(tmp_path, agent)
    original_inspect = api.inspect

    def unavailable(session_id: str) -> object:
        raise SessionStoreError("invalid_or_unavailable_store")

    def fail_commit(*args: object) -> object:
        monkeypatch.setattr(api, "inspect", unavailable)
        raise SessionStoreError("storage_failed")

    monkeypatch.setattr(api, "commit_turn", fail_commit)
    try:
        with pytest.raises(SessionDurabilityError):
            asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET, session=session))
        assert session.active_turn is None and len(tool.calls) == 1
        with pytest.raises(SessionStoreError):
            session.close()
        assert_competing_writer(tmp_path, "writer_busy")
        with pytest.raises(SessionStoreError, match="turn_still_owned"):
            api.close()
        monkeypatch.setattr(api, "inspect", original_inspect)
        report = session.recovery_report
        assert report.unresolved is not None and report.local_fault
        assert report.checkpoint.seq == 0 and len(agent.state.messages) == 4
        assert_competing_writer(tmp_path, "writer_busy")
    finally:
        monkeypatch.setattr(api, "inspect", original_inspect)
        session.close()
    assert_competing_writer(tmp_path, "writer_acquired")


def test_close_in_committed_callback_defers_writer_release_until_delivery_ends(
    tmp_path: Path,
) -> None:
    agent = make_agent(SequenceLLM([LLMResponse(content="done")]))
    api, session = create_session(tmp_path, agent)
    observed: list[str] = []

    def observe(event: Event) -> None:
        if event.type == "turn_committed":
            session.close()
            assert session.active_turn is not None and session.close_requested
            assert api.inspect(SESSION).receipt == session.receipt
            assert_competing_writer(tmp_path, "writer_busy")
            observed.append("delivered")

    agent.event_handler = observe
    assert (
        asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET, session=session))
        == "done"
    )
    assert observed == ["delivered"] and session.state is SessionState.CLOSED
    assert (
        session.last_turn is not None and not session.last_turn.commit_delivery_failed
    )
    session.close()
    assert_competing_writer(tmp_path, "writer_acquired")


@pytest.mark.parametrize("boundary", ["observation", "fence", "checkpoint"])
def test_cancellation_from_synchronous_host_boundary_never_strands_turn_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    llm = SequenceLLM([tool_response(), LLMResponse(content="done")])
    tool = RecordingTool()
    agent = make_agent(llm, tool)
    api, session = create_session(tmp_path, agent)

    def cancel(*args: object) -> object:
        raise asyncio.CancelledError()

    if boundary == "observation":
        monkeypatch.setattr(session, "_observe_binding", cancel)
    elif boundary == "fence":
        monkeypatch.setattr(api, "fence_tool", cancel)
    else:
        monkeypatch.setattr(api, "commit_turn", cancel)
    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET, session=session))
        assert session.active_turn is None
        assert len(tool.calls) == (1 if boundary == "checkpoint" else 0)
        assert len(llm.calls) == (
            0 if boundary == "observation" else (1 if boundary == "fence" else 2)
        )
        report = session.recovery_report
        assert report.checkpoint.seq == 0
        with pytest.raises(SessionAdmissionError):
            asyncio.run(run_turn(agent, "blocked", budget=TEST_BUDGET, session=session))
    finally:
        session.close()


def test_mismatching_returned_receipt_never_emits_acknowledgement_or_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = RecordingTool()
    llm = SequenceLLM([tool_response(), LLMResponse(content="done")])
    events: list[Event] = []
    agent = make_agent(llm, tool, events)
    api, session = create_session(tmp_path, agent)
    original = api.commit_turn

    def wrong_receipt(*args: Any) -> object:
        receipt = original(*args)
        return receipt.model_copy(update={"sha256": "0" * 64})

    monkeypatch.setattr(api, "commit_turn", wrong_receipt)
    try:
        with pytest.raises(SessionDurabilityError):
            asyncio.run(run_turn(agent, "hello", budget=TEST_BUDGET, session=session))
        report = session.recovery_report
        assert report.checkpoint.seq == 1 and report.receipt == session.receipt
        assert len(llm.calls) == 2 and len(tool.calls) == 1
        assert not any(e.type == "turn_committed" for e in events)
        assert session.state is SessionState.RECOVERY_REQUIRED
    finally:
        session.close()
