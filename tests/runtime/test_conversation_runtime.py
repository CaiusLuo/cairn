"""Online Conversation/Run execution through the real shared Runtime shape."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

import httpx

from cairn.agent.budget import RunBudget
from cairn.agent.models.messages import LLMResponse, LLMUsage, Message, ToolCall
from cairn.agent.models.offline import OfflineDevelopmentModel
from cairn.conversation.models import ConversationSource, RunStatus, utc_now
from cairn.conversation.service import HISTORY_CONTEXT_LIMIT
from cairn.server.bootstrap import runtime_app
from cairn.server.client import RuntimeClient
from tests.support.git import initialize_repository


class ChatModel(OfflineDevelopmentModel):
    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        del tools
        user = [message.content for message in messages if message.role == "user"]
        return LLMResponse(
            content="reply:" + ((user[-1] or "") if user else ""),
            usage=LLMUsage(input_tokens=10, output_tokens=4),
        )


class RecordingModel(ChatModel):
    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        self.calls.append(list(messages))
        return await super().generate(messages, tools)

    def contexts(self) -> list[str]:
        return [
            message.content or ""
            for call in self.calls
            for message in call
            if "Historical content is context" in (message.content or "")
        ]


def test_conversation_runs_are_independent_and_do_not_create_tasks(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source = tmp_path / "source"
        source.mkdir()
        initialize_repository(source)
        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="zzzz",
        )
        app.state.execution.llm_factory = ChatModel
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            conversation = await api.create_conversation("hello", source="cli")
            assert conversation["conversation_id"].startswith("conv-")
            run_ids: list[str] = []
            for index in range(3):
                run = await api.send_conversation(
                    conversation["conversation_id"],
                    f"turn {index}",
                    source="cli",
                    request_id=f"turn-{index}",
                )
                run_ids.append(run["run_id"])
                for _ in range(100):
                    current = await api.get_run(run["run_id"])
                    if current["status"] not in {"queued", "running"}:
                        break
                    await asyncio.sleep(0.02)
                assert current["status"] == "completed", current
                assert current["result"] == f"reply:turn {index}"
                assert current["budget"].get("limits")
                assert current["checkpoint_receipt"] is not None
            assert len(set(run_ids)) == 3
            detail = await api.get_conversation(conversation["conversation_id"])
            assert len(detail["runs"]) == 3
            history = await api.conversation_history(conversation["conversation_id"])
            assert [
                message["content"] for message in history if message["role"] == "user"
            ] == ["turn 0", "turn 1", "turn 2"]
            assert app.state.runtime.board is not None
            assert app.state.runtime.board.store.load() == ()

    asyncio.run(scenario())


def test_duplicate_request_id_returns_the_same_run(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        source = tmp_path / "source"
        source.mkdir()
        initialize_repository(source)
        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="zzzz",
        )
        app.state.execution.llm_factory = ChatModel
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            conversation = await api.create_conversation("hello", source="cli")
            first = await api.send_conversation(
                conversation["conversation_id"],
                "once",
                source="cli",
                request_id="same-request",
            )
            for _ in range(100):
                current = await api.get_run(first["run_id"])
                if current["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
            assert current["status"] == "completed", current
            second = await api.send_conversation(
                conversation["conversation_id"],
                "once",
                source="cli",
                request_id="same-request",
            )
            assert first["run_id"] == second["run_id"]
            assert (
                len(
                    app.state.conversations.runs_for_conversation(
                        conversation["conversation_id"]
                    )
                )
                == 1
            )

    asyncio.run(scenario())


def test_read_only_tool_chat_creates_no_task_or_worktree(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = tmp_path / "source"
        source.mkdir()
        initialize_repository(source)

        class ReadModel(OfflineDevelopmentModel):
            async def generate(
                self,
                messages: list[Message],
                tools: list[dict[str, Any]] | None = None,
            ) -> LLMResponse:
                del tools
                if not any(message.role == "tool" for message in messages):
                    return LLMResponse(
                        tool_calls=[
                            ToolCall(
                                id="read-1",
                                name="read_file",
                                arguments={"path": "tracked.txt"},
                            )
                        ],
                        usage=LLMUsage(input_tokens=10, output_tokens=2),
                    )
                return LLMResponse(
                    content="read complete",
                    usage=LLMUsage(input_tokens=10, output_tokens=2),
                )

        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="zzzz",
        )
        app.state.execution.llm_factory = ReadModel
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            conversation = await api.create_conversation(
                "read tracked.txt", source="cli"
            )
            run = await api.send_conversation(
                conversation["conversation_id"],
                "read tracked.txt",
                source="cli",
                request_id="read-only",
            )
            for _ in range(100):
                current = await api.get_run(run["run_id"])
                if current["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
            assert current["status"] == "completed", current
            assert app.state.runtime.list_tasks() == []
            assert app.state.runtime.board is not None
            assert app.state.runtime.board.store.load() == ()
            history = await api.conversation_history(conversation["conversation_id"])
            assert any(message["role"] == "tool" for message in history)
            assert not any(
                path.is_file() for path in (tmp_path / "worktrees").rglob("*")
            )

    asyncio.run(scenario())


def test_second_request_while_run_is_active_returns_conflict(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = tmp_path / "source"
        source.mkdir()
        initialize_repository(source)
        release = asyncio.Event()

        class SlowModel(OfflineDevelopmentModel):
            async def generate(
                self,
                messages: list[Message],
                tools: list[dict[str, Any]] | None = None,
            ) -> LLMResponse:
                del messages, tools
                await release.wait()
                return LLMResponse(
                    content="slow complete",
                    usage=LLMUsage(input_tokens=1, output_tokens=1),
                )

        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="zzzz",
        )
        app.state.execution.llm_factory = lambda: SlowModel()
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            conversation = await api.create_conversation("slow", source="cli")
            first = await api.send_conversation(
                conversation["conversation_id"],
                "first",
                source="cli",
                request_id="first",
            )
            for _ in range(100):
                current = await api.get_run(first["run_id"])
                if current["status"] == "running":
                    break
                await asyncio.sleep(0.02)
            try:
                await api.send_conversation(
                    conversation["conversation_id"],
                    "second",
                    source="web",
                    request_id="second",
                )
            except httpx.HTTPStatusError as error:
                assert error.response.status_code == 409
            else:
                raise AssertionError("second request should have been rejected")
            release.set()
            for _ in range(100):
                current = await api.get_run(first["run_id"])
                if current["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
            assert current["status"] == "completed", current

    asyncio.run(scenario())


def test_history_reads_durable_run_facts_without_a_session(tmp_path: Path) -> None:
    """Runs written before Session identity existed must still be readable."""

    async def scenario() -> None:
        source = tmp_path / "source"
        source.mkdir()
        initialize_repository(source)
        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="zzzz",
        )
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            service = app.state.conversations
            conversation = service.create_conversation(
                "legacy first", ConversationSource.CLI
            )
            service.store.create_run(
                conversation_id=conversation.conversation_id,
                status=RunStatus.COMPLETED,
                prompt="legacy first",
                result="historic answer",
                source=ConversationSource.CLI,
                created_at="2026-11-07T10:00:00+00:00",
            )
            service.store.create_run(
                conversation_id=conversation.conversation_id,
                status=RunStatus.COMPLETED,
                prompt="legacy second",
                result="another historic answer",
                source=ConversationSource.CLI,
                created_at="2026-11-07T10:01:00+00:00",
            )

            history = await api.conversation_history(conversation.conversation_id)
            assert [
                (message["role"], message["content"])
                for message in history
                if message["role"] in {"user", "assistant"}
            ] == [
                ("user", "legacy first"),
                ("assistant", "historic answer"),
                ("user", "legacy second"),
                ("assistant", "another historic answer"),
            ]

    asyncio.run(scenario())


def test_history_returns_the_initial_prompt_before_any_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = tmp_path / "source"
        source.mkdir()
        initialize_repository(source)
        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="zzzz",
        )
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            conversation = app.state.conversations.create_conversation(
                "not executed yet", ConversationSource.CLI
            )
            history = await api.conversation_history(conversation.conversation_id)
            assert history == [{"role": "user", "content": "not executed yet"}]

    asyncio.run(scenario())


def test_history_prepends_legacy_run_facts_before_the_session_transcript(
    tmp_path: Path,
) -> None:
    """A conversation continued after the Session upgrade keeps its old turns."""

    async def scenario() -> None:
        source = tmp_path / "source"
        source.mkdir()
        initialize_repository(source)
        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="zzzz",
        )
        app.state.execution.llm_factory = ChatModel
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            conversation = app.state.conversations.create_conversation(
                "legacy prompt", ConversationSource.CLI
            )
            app.state.conversations.store.create_run(
                conversation_id=conversation.conversation_id,
                status=RunStatus.COMPLETED,
                prompt="legacy prompt",
                result="historic answer",
                source=ConversationSource.CLI,
                created_at="2026-11-07T10:00:00+00:00",
            )
            run = await api.send_conversation(
                conversation.conversation_id,
                "session prompt",
                source="cli",
                request_id="mixed-history",
            )
            for _ in range(100):
                current = await api.get_run(run["run_id"])
                if current["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
            assert current["status"] == "completed", current

            history = await api.conversation_history(conversation.conversation_id)
            assert [
                (message["role"], message["content"])
                for message in history
                if message["role"] in {"user", "assistant"}
            ] == [
                ("user", "legacy prompt"),
                ("assistant", "historic answer"),
                ("user", "session prompt"),
                ("assistant", "reply:session prompt"),
            ]

    asyncio.run(scenario())


def test_history_keeps_a_sessionless_run_after_the_session_transcript(
    tmp_path: Path,
) -> None:
    """A run that failed before getting a Session must not vanish from history."""

    async def scenario() -> None:
        source = tmp_path / "source"
        source.mkdir()
        initialize_repository(source)
        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="zzzz",
        )
        app.state.execution.llm_factory = ChatModel
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            conversation = app.state.conversations.create_conversation(
                "session prompt", ConversationSource.CLI
            )
            run = await api.send_conversation(
                conversation.conversation_id,
                "session prompt",
                source="cli",
                request_id="session-then-failure",
            )
            for _ in range(100):
                current = await api.get_run(run["run_id"])
                if current["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
            assert current["status"] == "completed", current
            app.state.conversations.store.create_run(
                conversation_id=conversation.conversation_id,
                status=RunStatus.FAILED,
                prompt="failed before session",
                error="provider unavailable",
                source=ConversationSource.CLI,
                created_at="2099-01-01T00:00:00+00:00",
            )

            history = await api.conversation_history(conversation.conversation_id)
            assert [
                (message["role"], message["content"])
                for message in history
                if message["role"] in {"user", "assistant"}
            ] == [
                ("user", "session prompt"),
                ("assistant", "reply:session prompt"),
                ("user", "failed before session"),
            ]

    asyncio.run(scenario())


def test_history_orders_a_sessionless_run_between_session_runs(
    tmp_path: Path,
) -> None:
    """An interleaved failed Run keeps its durable position, not the tail."""

    async def scenario() -> None:
        source = tmp_path / "source"
        source.mkdir()
        initialize_repository(source)
        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="zzzz",
        )
        app.state.execution.llm_factory = ChatModel
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            conversation = app.state.conversations.create_conversation(
                "first prompt", ConversationSource.CLI
            )
            first = await api.send_conversation(
                conversation.conversation_id,
                "first prompt",
                source="cli",
                request_id="first-session",
            )
            for _ in range(100):
                current = await api.get_run(first["run_id"])
                if current["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
            assert current["status"] == "completed", current
            app.state.conversations.store.create_run(
                conversation_id=conversation.conversation_id,
                status=RunStatus.FAILED,
                prompt="failed middle",
                error="provider unavailable",
                source=ConversationSource.CLI,
                created_at=utc_now(),
            )
            second = await api.send_conversation(
                conversation.conversation_id,
                "second prompt",
                source="cli",
                request_id="second-session",
            )
            for _ in range(100):
                current = await api.get_run(second["run_id"])
                if current["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
            assert current["status"] == "completed", current

            history = await api.conversation_history(conversation.conversation_id)
            assert [
                (message["role"], message["content"])
                for message in history
                if message["role"] in {"user", "assistant"}
            ] == [
                ("user", "first prompt"),
                ("assistant", "reply:first prompt"),
                ("user", "failed middle"),
                ("user", "second prompt"),
                ("assistant", "reply:second prompt"),
            ]

    asyncio.run(scenario())


def test_history_reads_run_facts_when_the_session_store_is_missing(
    tmp_path: Path,
) -> None:
    """A lost Session store must not make a completed Run unreadable."""

    async def scenario() -> None:
        source = tmp_path / "source"
        source.mkdir()
        initialize_repository(source)
        state = tmp_path / "state"
        worktrees = tmp_path / "worktrees"
        first = runtime_app(source, state, worktrees, budget=RunBudget(5), token="zzzz")
        first.state.execution.llm_factory = ChatModel
        first_api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=first),
        )
        async with first.router.lifespan_context(first):
            conversation = await first_api.create_conversation(
                "legacy prompt", source="cli"
            )
            run = await first_api.send_conversation(
                conversation["conversation_id"],
                "legacy prompt",
                source="cli",
                request_id="legacy-session",
            )
            for _ in range(100):
                current = await first_api.get_run(run["run_id"])
                if current["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
            assert current["status"] == "completed", current
            assert current["session_id"] is not None

        shutil.rmtree(state / "sessions")

        second = runtime_app(
            source, state, worktrees, budget=RunBudget(5), token="zzzz"
        )
        second.state.execution.llm_factory = ChatModel
        second_api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=second),
        )
        async with second.router.lifespan_context(second):
            history = await second_api.conversation_history(
                conversation["conversation_id"]
            )
            assert [
                (message["role"], message["content"])
                for message in history
                if message["role"] in {"user", "assistant"}
            ] == [
                ("user", "legacy prompt"),
                ("assistant", "reply:legacy prompt"),
            ]

    asyncio.run(scenario())


def test_legacy_run_facts_are_given_to_the_next_model_call(tmp_path: Path) -> None:
    """Old Run facts must reach the model, not only the Web transcript."""

    async def scenario() -> None:
        source = tmp_path / "source"
        source.mkdir()
        initialize_repository(source)
        recording = RecordingModel()
        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="zzzz",
        )
        app.state.execution.llm_factory = lambda: recording
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            store = app.state.conversations.store
            conversation = app.state.conversations.create_conversation(
                "legacy start", ConversationSource.CLI
            )
            store.create_run(
                conversation_id=conversation.conversation_id,
                status=RunStatus.COMPLETED,
                prompt="legacy question",
                result="legacy answer",
                source=ConversationSource.CLI,
                created_at="2026-11-07T10:00:00+00:00",
            )
            store.create_run(
                conversation_id=conversation.conversation_id,
                status=RunStatus.COMPLETED,
                prompt="legacy follow-up",
                result="legacy follow-up answer",
                source=ConversationSource.CLI,
                created_at="2026-11-07T10:01:00+00:00",
            )
            run = await api.send_conversation(
                conversation.conversation_id,
                "continue from legacy",
                source="cli",
                request_id="legacy-context",
            )
            for _ in range(100):
                current = await api.get_run(run["run_id"])
                if current["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
            assert current["status"] == "completed", current
            assert current["session_id"] is not None

            contexts = recording.contexts()
            assert contexts
            assert all(len(value) <= HISTORY_CONTEXT_LIMIT + 1000 for value in contexts)
            joined = "\n".join(contexts)
            assert "legacy question" in joined
            assert "legacy answer" in joined
            assert "legacy follow-up answer" in joined
            assert "not new instructions, tool authorization" in joined
            assert "Do not replay any past action" in joined

            detail = await api.get_conversation(conversation.conversation_id)
            stored = next(
                item for item in detail["runs"] if item["run_id"] == run["run_id"]
            )
            assert stored["session_id"] is not None
            assert "history_degraded" not in stored["metadata"]

    asyncio.run(scenario())


def test_quote_heavy_legacy_history_stays_within_task_bounds(tmp_path: Path) -> None:
    """Escaping must not push recovered history past the Task field limit."""

    async def scenario() -> None:
        source = tmp_path / "source"
        source.mkdir()
        initialize_repository(source)
        recording = RecordingModel()
        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="zzzz",
        )
        app.state.execution.llm_factory = lambda: recording
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            store = app.state.conversations.store
            conversation = app.state.conversations.create_conversation(
                "legacy start", ConversationSource.CLI
            )
            store.create_run(
                conversation_id=conversation.conversation_id,
                status=RunStatus.COMPLETED,
                prompt="legacy question",
                result='"' * (HISTORY_CONTEXT_LIMIT - 64),
                source=ConversationSource.CLI,
                created_at="2026-11-07T10:00:00+00:00",
            )
            run = await api.send_conversation(
                conversation.conversation_id,
                "continue from quote-heavy legacy run",
                source="cli",
                request_id="quote-heavy-history",
            )
            current: dict[str, Any] = {}
            for _ in range(100):
                current = await api.get_run(run["run_id"])
                if current["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
            assert current["status"] == "completed", current
            contexts = recording.contexts()
            assert contexts
            assert all(len(value) <= HISTORY_CONTEXT_LIMIT for value in contexts)

    asyncio.run(scenario())


def test_setup_failure_settles_observers_and_fails_the_run(tmp_path: Path) -> None:
    """A failure before execution must not leave the run active or silent."""

    async def scenario() -> None:
        source = tmp_path / "source"
        source.mkdir()
        initialize_repository(source)
        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="zzzz",
        )
        app.state.execution.llm_factory = lambda: ChatModel()
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            service = app.state.conversations
            settled: list[dict[str, Any]] = []
            service.add_run_settled_listener(
                lambda settled_run: settled.append(settled_run.model_dump(mode="json"))
            )

            def explode(*_args: Any, **_kwargs: Any) -> str:
                raise RuntimeError("history setup exploded")

            service._history_context = explode  # type: ignore[method-assign]
            conversation = service.create_conversation(
                "setup failure", ConversationSource.CLI
            )
            run = await api.send_conversation(
                conversation.conversation_id,
                "hello",
                source="cli",
                request_id="setup-failure",
            )
            current: dict[str, Any] = {}
            for _ in range(100):
                current = await api.get_run(run["run_id"])
                if current["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
            assert current["status"] == "failed", current
            assert current["error"] == "conversation_worker_error"
            assert service.active_run_id is None
            assert settled and settled[-1]["run_id"] == run["run_id"]
            assert settled[-1]["status"] == "failed"

    asyncio.run(scenario())


def test_legacy_history_context_is_bounded_to_recent_turns(tmp_path: Path) -> None:
    async def scenario() -> None:
        source = tmp_path / "source"
        source.mkdir()
        initialize_repository(source)
        recording = RecordingModel()
        app = runtime_app(
            source,
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(5),
            token="zzzz",
        )
        app.state.execution.llm_factory = lambda: recording
        api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=app),
        )
        async with app.router.lifespan_context(app):
            store = app.state.conversations.store
            conversation = app.state.conversations.create_conversation(
                "bounded start", ConversationSource.CLI
            )
            store.create_run(
                conversation_id=conversation.conversation_id,
                status=RunStatus.COMPLETED,
                prompt="oldest prompt",
                result="oldest " + "x" * 20_000,
                source=ConversationSource.CLI,
                created_at="2026-11-07T10:00:00+00:00",
            )
            store.create_run(
                conversation_id=conversation.conversation_id,
                status=RunStatus.COMPLETED,
                prompt="newest prompt",
                result="newest " + "y" * 20_000,
                source=ConversationSource.CLI,
                created_at="2026-11-07T10:01:00+00:00",
            )
            run = await api.send_conversation(
                conversation.conversation_id,
                "continue within budget",
                source="cli",
                request_id="bounded-context",
            )
            for _ in range(100):
                current = await api.get_run(run["run_id"])
                if current["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
            assert current["status"] == "completed", current

            contexts = recording.contexts()
            assert contexts
            assert all(len(value) <= HISTORY_CONTEXT_LIMIT + 1000 for value in contexts)
            joined = "\n".join(contexts)
            assert "newest " in joined
            assert "oldest " not in joined

    asyncio.run(scenario())


def test_missing_session_continues_in_a_new_session_after_restart(
    tmp_path: Path,
) -> None:
    """A lost Session must not turn a new Run into a post-accept failure."""

    async def scenario() -> None:
        source = tmp_path / "source"
        source.mkdir()
        initialize_repository(source)
        state = tmp_path / "state"
        worktrees = tmp_path / "worktrees"
        first = runtime_app(source, state, worktrees, budget=RunBudget(5), token="zzzz")
        first.state.execution.llm_factory = ChatModel
        first_api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=first),
        )
        async with first.router.lifespan_context(first):
            conversation = await first_api.create_conversation(
                "first prompt", source="cli"
            )
            run = await first_api.send_conversation(
                conversation["conversation_id"],
                "first prompt",
                source="cli",
                request_id="before-session-loss",
            )
            for _ in range(100):
                current = await first_api.get_run(run["run_id"])
                if current["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
            assert current["status"] == "completed", current
            old_session = current["session_id"]
            assert old_session is not None

        shutil.rmtree(state / "sessions")

        recording = RecordingModel()
        second = runtime_app(
            source, state, worktrees, budget=RunBudget(5), token="zzzz"
        )
        second.state.execution.llm_factory = lambda: recording
        second_api = RuntimeClient(
            "http://127.0.0.1:8777",
            "zzzz",
            transport=httpx.ASGITransport(app=second),
        )
        async with second.router.lifespan_context(second):
            new_run = await second_api.send_conversation(
                conversation["conversation_id"],
                "after restart",
                source="cli",
                request_id="after-session-loss",
            )
            for _ in range(100):
                current = await second_api.get_run(new_run["run_id"])
                if current["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
            assert current["status"] == "completed", current
            assert current["session_id"] not in {None, old_session}

            detail = await second_api.get_conversation(conversation["conversation_id"])
            stored = next(
                item for item in detail["runs"] if item["run_id"] == new_run["run_id"]
            )
            assert stored["metadata"]["history_degraded"]
            old_run = next(
                item for item in detail["runs"] if item["run_id"] == run["run_id"]
            )
            assert old_run["session_id"] == old_session

            history = await second_api.conversation_history(
                conversation["conversation_id"]
            )
            assert [
                (message["role"], message["content"])
                for message in history
                if message["role"] in {"user", "assistant"}
            ] == [
                ("user", "first prompt"),
                ("assistant", "reply:first prompt"),
                ("user", "after restart"),
                ("assistant", "reply:after restart"),
            ]
            contexts = recording.contexts()
            assert contexts
            joined = "\n".join(contexts)
            assert "first prompt" in joined
            assert "reply:first prompt" in joined

    asyncio.run(scenario())
