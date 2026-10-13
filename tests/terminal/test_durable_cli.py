"""Offline public CLI integration using real SQLite and fresh provider runtimes."""

import asyncio
import multiprocessing
import os
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

import pytest
import typer
from typer.testing import CliRunner, Result

from cairn.core.context import ContextBudget, ContextBuilder
from cairn.core.llm import LLMStreamChunk
from cairn.core.model_manager import ModelManager
from cairn.core.models import LLMResponse, Message, ToolCall
from cairn.core.permissions import (
    PermissionCapability,
    PermissionChoice,
    SessionPermissionHandler,
)
from cairn.core.provider_catalog import NamedProvider
from cairn.infra.observability.models import Span
from cairn.infra.observability.sinks import JsonlTraceSink
from cairn.infra.sessions.models import (
    RecoveryReport,
    SessionStoreError,
    ToolFence,
    TurnIntent,
)
from cairn.infra.sessions.store import SessionStore
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.agent import Agent
from cairn.runtime.assembly import build_agent
from cairn.runtime.durable_session import DurableSession
from cairn.runtime.model_executor import ModelExecutor
from cairn.runtime.provider_runtime import ProviderRuntime
from cairn.terminal import cli, durable
from cairn.terminal.input import CliInput
from tests.support.git import git, initialize_repository
from tests.support.runtime import NetworkRequestTool, SequenceLLM

runner = CliRunner()


def test_trace_failure_does_not_disclose_exception(harness: "Harness") -> None:
    def fail(self: JsonlTraceSink, span: Span) -> None:
        raise OSError("private-internal-trace-details")

    harness.monkeypatch.setattr(JsonlTraceSink, "emit", fail)
    harness.inputs("hello", EOFError())
    result = harness.invoke("new")
    assert result.exit_code == 0, result.output
    assert "Trace unavailable: persistence failed." in result.output
    assert "private-internal-trace-details" not in result.output
    assert "Committed checkpoint 1" in result.output


class Inputs(CliInput):
    def __init__(self, *values: str | BaseException) -> None:
        self.values = iter(values)
        self.reads = 0

    async def read(self) -> str:
        self.reads += 1
        value = next(self.values)
        if isinstance(value, BaseException):
            raise value
        return value


@dataclass
class Harness:
    root: Path
    monkeypatch: pytest.MonkeyPatch
    clients: list[SequenceLLM] = field(default_factory=list)
    agents: list[Agent] = field(default_factory=list)
    responses: list[LLMResponse] = field(default_factory=list)
    client_type: type[SequenceLLM] = SequenceLLM

    @property
    def directory(self) -> Path:
        return self.root / "store"

    def inputs(self, *values: str | BaseException) -> Inputs:
        reader = Inputs(*values)
        self.monkeypatch.setattr(cli, "CliInput", lambda: reader)
        return reader

    def invoke(self, operation: str, session_id: str | None = None) -> Result:
        args = ["session", operation]
        if session_id is not None:
            args.append(session_id)
        return runner.invoke(cli.app, [*args, "--store", str(self.directory)])

    def inspect(self, session_id: str) -> RecoveryReport:
        store = durable.open_store(
            self.directory, Workspace(self.root / "work"), inspect=True
        )
        try:
            return store.inspect(session_id)
        finally:
            store.close()

    def create(self) -> str:
        self.inputs(EOFError())
        result = self.invoke("new")
        assert result.exit_code == 0, result.output
        return result.output.split("Session: ", 1)[1].splitlines()[0]

    def approve(self, session_id: str) -> None:
        report = self.inspect(session_id)
        turn_id = report.unresolved.intent.turn_id if report.unresolved else "none"
        token = (
            f"abandon_without_replay {session_id} {turn_id} {report.revision.version}"
        )
        self.monkeypatch.setattr(typer, "prompt", lambda *a, **k: token)


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    # Linux tmp_path lives under forbidden /tmp. Keep store and writable roots
    # as siblings under the real home on both platforms, before replacing HOME.
    with (
        TemporaryDirectory(
            prefix="cairn-durable-cli-", dir=Path.home().resolve()
        ) as parent,
        monkeypatch.context() as isolated,
    ):
        root = Path(parent).resolve()
        work = root / "work"
        work.mkdir()
        scratch = root / "scratch"
        scratch.mkdir()
        home = root / "home"
        home.mkdir()
        isolated.chdir(work)
        isolated.setenv("HOME", str(home))
        isolated.setenv("TMPDIR", str(scratch))
        isolated.setenv("UV_CACHE_DIR", str(root / "cache"))
        isolated.setenv("CAIRN_LLM_MODEL", "offline/model")
        isolated.setenv("CAIRN_LLM_API_KEY", "test-private-credential")
        isolated.setenv("CAIRN_BASE_URL", "https://example.invalid/v1")
        isolated.setattr(cli, "dotenv_values", lambda: {})
        isolated.setattr(cli, "print_banner", lambda: None)
        isolated.setattr(cli, "confirm_provider_access", lambda *a, **k: True)
        isolated.setattr(durable, "interactive_terminal", lambda: True)
        result = Harness(root, isolated)

        def runtime(
            provider: NamedProvider,
            credential: str,
            budget: ContextBudget,
            *,
            group: str,
        ) -> ProviderRuntime:
            client = result.client_type(list(result.responses))
            result.clients.append(client)
            manager = ModelManager(provider.config)
            manager.select_model(group)
            builder = ContextBuilder(budget=budget)

            def factory(model: str) -> tuple[SequenceLLM, ContextBuilder]:
                return client, builder

            return ProviderRuntime(
                provider,
                manager,
                ModelExecutor(manager, factory),
                client,
                builder,
                factory,
            )

        def agent(**kwargs: Any) -> Agent:
            built = build_agent(**kwargs)
            result.agents.append(built)
            return built

        isolated.setattr(cli, "build_provider_runtime", runtime)
        isolated.setattr(cli, "build_agent", agent)
        yield result


def test_default_is_stateless_and_commands_remain_available(harness: Harness) -> None:
    harness.inputs("/help", "/model list", "/quit")
    result = runner.invoke(cli.app, [])
    assert result.exit_code == 0
    assert not harness.directory.exists()
    assert not (harness.root / "home/.local/state/cairn/sessions").exists()
    assert all(not c.calls for c in harness.clients)
    assert harness.agents[0].session_owner is None
    assert "session" in runner.invoke(cli.app, ["--help"]).output


def test_new_multiple_turns_inspect_and_fresh_reopen(harness: Harness) -> None:
    harness.responses = [LLMResponse(content="one"), LLMResponse(content="two")]
    harness.inputs("first", "second", EOFError())
    result = harness.invoke("new")
    assert result.exit_code == 0, result.output
    session_id = result.output.split("Session: ", 1)[1].splitlines()[0]
    report = harness.inspect(session_id)
    assert report.receipt.revision.head_seq == 2
    assert [m.content for m in report.checkpoint.messages] == [
        "first",
        "one",
        "second",
        "two",
    ]
    assert result.output.index("one") < result.output.index("Committed checkpoint 1")
    assert result.output.index("two") < result.output.index("Committed checkpoint 2")
    before = {
        p.name: (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns)
        for p in harness.directory.iterdir()
    }
    with harness.monkeypatch.context() as patch:
        patch.setattr(
            cli,
            "resolve_provider_api_key",
            lambda *a: pytest.fail("inspection accessed a credential"),
        )
        inspected = harness.invoke("inspect", session_id)
    assert inspected.exit_code == 0
    assert before == {
        p.name: (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns)
        for p in harness.directory.iterdir()
    }
    harness.approve(session_id)
    harness.inputs(EOFError())
    reopened = harness.invoke("reopen", session_id)
    assert reopened.exit_code == 0, reopened.output
    assert "Reconciled without replay" in reopened.output
    assert not harness.clients[-1].calls
    assert harness.agents[-1] is not harness.agents[0]
    assert (
        harness.agents[-1].permission_handler
        is not harness.agents[0].permission_handler
    )
    assert len(harness.agents[-1].state.messages) == 4


def _orphan(directory: str, workspace: str, session_id: str) -> None:
    api = SessionStore(Path(directory), forbidden_roots=(Path(workspace),))
    report = api.inspect(session_id)
    turn_id = str(uuid4())
    intent = TurnIntent(
        session_id=session_id,
        turn_id=turn_id,
        base_seq=report.revision.head_seq,
        message_start=len(report.checkpoint.messages),
        input_digest="0" * 64,
        started_at_utc=durable.now(),
        binding=report.binding,
    )
    revision = api.begin_turn(report.revision, intent)
    api.fence_tool(
        revision,
        ToolFence(
            session_id=session_id,
            turn_id=turn_id,
            ordinal=1,
            assistant_message_index=intent.message_start + 1,
            call_id="private-call",
            tool_name="private-tool",
            observed_at_utc=durable.now(),
        ),
    )
    os._exit(0)  # Kernel releases writer ownership; no settlement/close is simulated.


def orphan(harness: Harness, session_id: str) -> None:
    child = multiprocessing.get_context("spawn").Process(
        target=_orphan,
        args=(str(harness.directory), str(harness.root / "work"), session_id),
    )
    child.start()
    child.join(15)
    try:
        assert child.exitcode == 0
    finally:
        if child.is_alive():
            child.kill()
            child.join(5)
        child.close()


def test_orphan_fence_never_auto_resumes_and_explicit_abandonment(
    harness: Harness,
) -> None:
    session_id = harness.create()
    orphan(harness, session_id)
    initial = harness.inspect(session_id)
    assert initial.status == "effects_unknown"
    harness.inputs("must not read")
    reopened = harness.invoke("reopen", session_id)
    assert reopened.exit_code == 0
    assert "Recovery required" in reopened.output
    assert "External effects may already have occurred" in reopened.output
    assert (
        "private-call" not in reopened.output and "private-tool" not in reopened.output
    )
    assert not harness.clients[-1].calls
    assert harness.inspect(session_id) == initial
    harness.approve(session_id)
    reconciled = harness.invoke("reconcile", session_id)
    assert reconciled.exit_code == 0, reconciled.output
    assert not harness.clients[-1].calls
    report = harness.inspect(session_id)
    assert report.unresolved is None and report.attempts[-1].state == "abandoned"
    assert report.checkpoint == initial.checkpoint
    harness.responses = [LLMResponse(content="new work")]
    harness.approve(session_id)
    harness.inputs("fresh request", EOFError())
    result = harness.invoke("reopen", session_id)
    assert result.exit_code == 0, result.output
    assert "Committed checkpoint 1" in result.output
    assert len(harness.clients[-1].calls) == 1
    assert "fresh request" in [m.content for m in harness.agents[-1].state.messages]


@pytest.mark.parametrize("answer", ["refusal", "eof", "noninteractive"])
def test_refusal_eof_noninteractive_never_approve_effects(
    harness: Harness, answer: str
) -> None:
    session_id = harness.create()
    orphan(harness, session_id)
    initial = harness.inspect(session_id)
    if answer == "refusal":
        harness.monkeypatch.setattr(typer, "prompt", lambda *a, **k: "no")
    elif answer == "eof":

        def eof(*args: Any, **kwargs: Any) -> str:
            raise EOFError()

        harness.monkeypatch.setattr(typer, "prompt", eof)
    else:
        harness.monkeypatch.setattr(durable, "interactive_terminal", lambda: False)
        harness.monkeypatch.setattr(
            typer,
            "prompt",
            lambda *a, **k: pytest.fail("noninteractive confirmation"),
        )
    result = harness.invoke("reconcile", session_id)
    assert result.exit_code == 0
    assert harness.inspect(session_id) == initial
    assert not harness.clients[-1].calls


def test_provider_approval_precedes_credential_even_for_env_layout(
    harness: Harness,
) -> None:
    harness.monkeypatch.setattr(cli, "confirm_provider_access", lambda *a, **k: False)
    harness.monkeypatch.setattr(
        cli,
        "resolve_provider_api_key",
        lambda *a: pytest.fail("unapproved credential access"),
    )
    result = harness.invoke("new")
    assert result.exit_code == 1
    assert not harness.directory.exists()
    assert not harness.clients


def test_reopen_needs_fresh_provider_approval(harness: Harness) -> None:
    session_id = harness.create()
    harness.monkeypatch.setattr(cli, "confirm_provider_access", lambda *a, **k: False)
    harness.monkeypatch.setattr(
        cli,
        "resolve_provider_api_key",
        lambda *a: pytest.fail("stored binding authorized credential access"),
    )
    result = harness.invoke("reopen", session_id)
    assert result.exit_code == 1
    assert len(harness.clients) == 1


@pytest.mark.parametrize("kind", ["id", "workspace", "clone", "provider"])
def test_invalid_bindings_fail_closed(harness: Harness, kind: str) -> None:
    if kind == "clone":
        initialize_repository(harness.root / "work")
    session_id = harness.create()
    initial = harness.inspect(session_id)
    if kind == "id":
        session_id = str(uuid4())
    elif kind == "workspace":
        different = harness.root / "other"
        different.mkdir()
        harness.monkeypatch.chdir(different)
    elif kind == "clone":
        marker = harness.root / "work/.git"
        marker.rename(harness.root / "old-git")
        initialize_repository(harness.root / "work")
    else:
        harness.monkeypatch.setenv("CAIRN_BASE_URL", "https://different.invalid/v1")
        harness.monkeypatch.setattr(typer, "prompt", lambda *a, **k: "no")
    result = harness.invoke("reopen", session_id)
    assert result.exit_code == (0 if kind == "provider" else 1)
    assert not harness.clients[-1].calls
    assert harness.inspect(initial.revision.session_id) == initial


def test_git_head_and_provider_changes_require_exact_acknowledgement(
    harness: Harness,
) -> None:
    initialize_repository(harness.root / "work")
    session_id = harness.create()
    git(harness.root / "work", "checkout", "-b", "changed")
    harness.monkeypatch.setenv("CAIRN_BASE_URL", "https://new.invalid/v1")
    harness.approve(session_id)
    harness.inputs(EOFError())
    result = harness.invoke("reopen", session_id)
    assert result.exit_code == 0, result.output
    assert "Git HEAD/branch differs" in result.output
    assert "provider binding differs" in result.output
    assert not harness.clients[-1].calls
    report = harness.inspect(session_id)
    assert report.binding.workspace.git is not None
    assert report.binding.workspace.git.branch == "changed"


def test_durable_switching_is_rejected_without_rebinding(harness: Harness) -> None:
    harness.responses = [LLMResponse(content="done")]
    harness.inputs("/model default", "/provider other", "hello", EOFError())
    result = harness.invoke("new")
    assert result.exit_code == 0, result.output
    assert result.output.count("switching is disabled") == 2
    assert len(harness.clients) == 1 and len(harness.clients[0].calls) == 1
    assert "Committed checkpoint 1" in result.output


def test_host_change_during_confirmation_rejects_without_mutation(
    harness: Harness,
) -> None:
    initialize_repository(harness.root / "work")
    session_id = harness.create()
    initial = harness.inspect(session_id)
    token = f"abandon_without_replay {session_id} none {initial.revision.version}"

    def approve_then_change(*args: Any, **kwargs: Any) -> str:
        git(harness.root / "work", "checkout", "-b", "during-confirmation")
        return token

    harness.monkeypatch.setattr(typer, "prompt", approve_then_change)
    result = harness.invoke("reopen", session_id)
    assert result.exit_code == 1
    assert not harness.clients[-1].calls
    assert harness.inspect(session_id) == initial


def test_secret_exception_and_uncertain_commit_stop_input(harness: Harness) -> None:
    harness.responses = [LLMResponse(content="provisional answer")]
    reader = harness.inputs("hello", "must not execute", EOFError())

    def uncertain(*args: Any, **kwargs: Any) -> Any:
        raise OSError("test-private-credential internal-sql-text")

    harness.monkeypatch.setattr(SessionStore, "commit_turn", uncertain)
    result = harness.invoke("new")
    assert result.exit_code == 0, result.output
    assert reader.reads == 1
    assert len(harness.clients[0].calls) == 1
    assert "Committed checkpoint" not in result.output
    assert (
        "provisional answer" not in result.output
    )  # nonstreamed response never returned
    assert "test-private-credential" not in result.output
    assert "internal-sql-text" not in result.output
    session_id = result.output.split("Session: ", 1)[1].splitlines()[0]
    assert harness.inspect(session_id).status != "clean"


def test_old_network_permissions_never_restored(harness: Harness) -> None:
    tools: list[NetworkRequestTool] = []

    def build(**kwargs: Any) -> Agent:
        agent = build_agent(**kwargs)
        tool = NetworkRequestTool()
        agent.tools.override_tool(tool)
        tools.append(tool)
        harness.agents.append(agent)
        return agent

    harness.monkeypatch.setattr(cli, "build_agent", build)
    harness.monkeypatch.setattr(
        cli, "console_permission_prompt", lambda request: PermissionChoice.ALLOW_SESSION
    )
    harness.responses = [
        LLMResponse(
            tool_calls=[
                ToolCall(
                    id="net",
                    name="bash",
                    arguments={
                        "command": "curl example.invalid",
                        "network_access": True,
                        "justification": "offline test",
                    },
                )
            ]
        ),
        LLMResponse(content="done"),
    ]
    harness.inputs("first", EOFError())
    result = harness.invoke("new")
    assert result.exit_code == 0, result.output
    assert len(tools[0].calls) == 1
    old_handler = harness.agents[0].permission_handler
    assert isinstance(old_handler, SessionPermissionHandler)
    assert PermissionCapability.NETWORK in old_handler.grants
    assert "Provisional tool observation" in result.output
    assert result.output.index("Provisional tool observation") < result.output.index(
        "Committed checkpoint"
    )
    session_id = result.output.split("Session: ", 1)[1].splitlines()[0]
    harness.monkeypatch.setattr(
        cli, "console_permission_prompt", lambda request: PermissionChoice.DENY
    )
    harness.approve(session_id)
    harness.inputs("second", EOFError())
    result = harness.invoke("reopen", session_id)
    assert result.exit_code == 0, result.output
    assert not tools[-1].calls
    new_handler = harness.agents[-1].permission_handler
    assert isinstance(new_handler, SessionPermissionHandler)
    assert not new_handler.grants
    assert "Committed checkpoint 2" in result.output


def test_linux_tmp_store_layout_rejects_with_exact_reason(harness: Harness) -> None:
    with TemporaryDirectory(prefix="cairn-durable-rejected-", dir="/tmp") as parent:
        directory = Path(parent) / "store"
        with pytest.raises(SessionStoreError) as failure:
            durable.open_store(directory, Workspace(harness.root / "work"))
        assert failure.value.code == "writable_root_overlap"
        assert not directory.exists()


@pytest.mark.parametrize("root", ["workspace", "tmp", "cache", "additional"])
def test_storage_rejects_every_effective_writable_root(
    harness: Harness, root: str
) -> None:
    paths = {
        "workspace": harness.root / "work",
        "tmp": harness.root / "scratch",
        "cache": harness.root / "cache",
        "additional": harness.root / "extra",
    }
    path = paths[root]
    options = ["--forbid-root", str(path)] if root == "additional" else []
    result = runner.invoke(
        cli.app, ["session", "new", "--store", str(path / "sessions"), *options]
    )
    assert result.exit_code == 1
    assert not (path / "sessions").exists()


def test_repeated_shutdown_cancellation_holds_writer_until_cleanup(
    harness: Harness,
) -> None:
    async def scenario() -> None:
        started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class HeldClient(SequenceLLM):
            async def generate(
                self, messages: list[Message], tools: list[dict[str, Any]] | None = None
            ) -> LLMResponse:
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    await release.wait()
                raise AssertionError("unreachable")

        harness.client_type = HeldClient
        reader = harness.inputs("hello", EOFError())
        host = asyncio.create_task(
            cli.main(durable=durable.DurableOptions("new", directory=harness.directory))
        )
        await asyncio.wait_for(started.wait(), 5)
        host.cancel()
        await asyncio.wait_for(cleaning.wait(), 5)
        owner = harness.agents[0].session_owner
        assert isinstance(owner, DurableSession)
        assert owner is not None and owner.active_turn is not None
        with pytest.raises(SessionStoreError):
            SessionStore(harness.directory, forbidden_roots=(harness.root / "work",))
        host.cancel()
        await asyncio.sleep(0)
        assert not host.done() and reader.reads == 1
        with pytest.raises(SessionStoreError):
            SessionStore(harness.directory, forbidden_roots=(harness.root / "work",))
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(host, 5)
        writer = SessionStore(
            harness.directory, forbidden_roots=(harness.root / "work",)
        )
        try:
            report = writer.inspect(str(owner.session_id))
            assert report.status == "clean" and report.receipt.outcome == "cancelled"
        finally:
            writer.close()

    asyncio.run(scenario())


def test_streamed_answer_is_provisional_when_commit_fails(harness: Harness) -> None:
    class StreamClient(SequenceLLM):
        async def stream_generate(
            self, messages: list[Message], tools: list[dict[str, Any]] | None = None
        ) -> AsyncGenerator[LLMStreamChunk, None]:
            self.calls.append((messages, tools))
            yield LLMStreamChunk(text="visible provisional answer")
            yield LLMStreamChunk(finish_reason="stop")

    harness.client_type = StreamClient
    reader = harness.inputs("hello", "never execute")

    def failed(*args: Any, **kwargs: Any) -> Any:
        raise OSError("internal storage exception")

    harness.monkeypatch.setattr(SessionStore, "commit_turn", failed)
    result = harness.invoke("new")
    assert result.exit_code == 0, result.output
    assert "visible provisional answer" in result.output
    assert "awaiting durable checkpoint" in result.output
    assert "Committed checkpoint" not in result.output
    assert "internal storage exception" not in result.output
    assert reader.reads == 1


def test_provider_noninteractive_environment_fails_before_credentials(
    harness: Harness,
) -> None:
    from cairn.terminal.output import confirm_provider_access

    harness.monkeypatch.setattr(cli, "confirm_provider_access", confirm_provider_access)
    harness.monkeypatch.setattr(
        cli,
        "resolve_provider_api_key",
        lambda *a: pytest.fail("noninteractive provider access"),
    )
    result = harness.invoke("new")
    assert result.exit_code == 1
    assert not harness.directory.exists()
