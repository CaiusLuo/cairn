import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from typer.testing import CliRunner

import cairn.cli as cli_module
from cairn.assembly import build_agent
from cairn.cli import app
from cairn.core.agent import Agent
from cairn.core.budget import RunBudget
from cairn.core.events import Event
from cairn.input import CliInput
from cairn.observability.sinks import JsonlTraceSink
from cairn.observability.tracer import Tracer

runner = CliRunner()

ENVIRONMENT: dict[str, str] = {
    "CAIRN_LLM_MODEL": "provider/model",
    "CAIRN_LLM_API_KEY": "secret",
    "CAIRN_BASE_URL": "https://example.test/v1",
}


class ScriptedCliInput(CliInput):
    def __init__(self, *values: str | BaseException) -> None:
        self._values = iter(values)
        self.read_count = 0

    async def read(self) -> str:
        self.read_count += 1
        try:
            value = next(self._values)
        except StopIteration as exc:
            raise AssertionError("No scripted CLI input remains") from exc
        if isinstance(value, BaseException):
            raise value
        return value


def _set_cli_inputs(
    monkeypatch: pytest.MonkeyPatch,
    *values: str | BaseException,
) -> ScriptedCliInput:
    reader = ScriptedCliInput(*values)
    monkeypatch.setattr(cli_module, "CliInput", lambda: reader)
    return reader


def _disable_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "dotenv_values", lambda: {})


def _set_environment(
    monkeypatch: pytest.MonkeyPatch,
    environment: dict[str, str],
) -> None:
    _disable_dotenv(monkeypatch)
    for name in (*ENVIRONMENT, "PROJECT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)


def _write_env_file(tmp_path: Path, contents: str) -> Path:
    env_file = tmp_path / ".env"
    env_file.write_text(contents, encoding="utf-8")
    return env_file


def _run_main_capturing_llm(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(cli_module, "print_banner", lambda: None)
    _set_cli_inputs(monkeypatch, "/exit")
    captured: dict[str, Any] = {}

    def capture_agent(**kwargs: Any) -> Agent:
        captured.update(kwargs)
        return build_agent(**kwargs)

    monkeypatch.setattr(cli_module, "build_agent", capture_agent)
    asyncio.run(cli_module.main())
    return captured["llm"]


def test_main_requires_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_environment(monkeypatch, {})

    with pytest.raises(ValueError, match="CAIRN_LLM_MODEL"):
        asyncio.run(cli_module.main())


@pytest.mark.parametrize(
    ("host_values", "expected"),
    [
        pytest.param(
            {},
            {
                "CAIRN_LLM_MODEL": "file/model",
                "CAIRN_LLM_API_KEY": "file-key",
                "CAIRN_BASE_URL": "https://file.example/v1",
            },
            id="dotenv-fallback",
        ),
        pytest.param(
            {"CAIRN_LLM_MODEL": "host/model"},
            {
                "CAIRN_LLM_MODEL": "host/model",
                "CAIRN_LLM_API_KEY": "file-key",
                "CAIRN_BASE_URL": "https://file.example/v1",
            },
            id="host-precedence-and-file-fallback",
        ),
    ],
)
def test_main_uses_host_precedence_without_injecting_env_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_values: dict[str, str],
    expected: dict[str, str],
) -> None:
    for name in (*ENVIRONMENT, "PROJECT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    for name, value in host_values.items():
        monkeypatch.setenv(name, value)
    env_file = _write_env_file(
        tmp_path,
        "CAIRN_LLM_MODEL=file/model\n"
        "CAIRN_LLM_API_KEY=file-key\n"
        "CAIRN_BASE_URL=https://file.example/v1\n"
        "PROJECT_SECRET=should-not-be-injected\n",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_module, "dotenv_values", lambda: dotenv_values(env_file))

    llm = _run_main_capturing_llm(monkeypatch)

    assert llm.model == expected["CAIRN_LLM_MODEL"]
    assert llm.api_key == expected["CAIRN_LLM_API_KEY"]
    assert llm.api_base == expected["CAIRN_BASE_URL"]
    assert "PROJECT_SECRET" not in os.environ


def test_cli_ignores_blank_input_and_runs_normal_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_environment(monkeypatch, ENVIRONMENT)
    monkeypatch.setattr(cli_module, "print_banner", lambda: None)
    submitted_prompt = "  hello\nworld  "
    _set_cli_inputs(monkeypatch, "", " \t\n ", submitted_prompt, "/quit")
    turns: list[str] = []
    responses: list[str] = []

    async def fake_run_turn(
        agent: Agent,
        user_input: str,
        *,
        budget: RunBudget,
    ) -> str:
        turns.append(user_input)
        assert budget == cli_module.DEFAULT_CLI_RUN_BUDGET
        return f"reply to {user_input}"

    monkeypatch.setattr(cli_module, "run_turn", fake_run_turn)
    monkeypatch.setattr(cli_module, "print_assistant_response", responses.append)

    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert turns == [submitted_prompt]
    assert responses == [f"reply to {submitted_prompt}"]


def test_cli_bracketed_paste_starts_one_turn_only_after_explicit_submit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_environment(monkeypatch, ENVIRONMENT)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_module, "print_banner", lambda: None)
    monkeypatch.setattr(cli_module, "print_assistant_response", lambda _text: None)
    pasted = '```python\nif ready:\n    print("hello")\n```'
    turns: list[str] = []
    turn_started: asyncio.Event | None = None

    async def fake_run_turn(
        agent: Agent,
        user_input: str,
        *,
        budget: RunBudget,
    ) -> str:
        assert budget == cli_module.DEFAULT_CLI_RUN_BUDGET
        turns.append(user_input)
        assert turn_started is not None
        turn_started.set()
        return "done"

    monkeypatch.setattr(cli_module, "run_turn", fake_run_turn)

    async def scenario() -> None:
        nonlocal turn_started
        turn_started = asyncio.Event()
        with create_pipe_input() as pipe_input:
            cli_input = CliInput(input=pipe_input, output=DummyOutput())
            main_task = asyncio.create_task(cli_module.main(cli_input))

            pipe_input.send_text(f"\x1b[200~{pasted}\x1b[201~")
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(turn_started.wait(), timeout=0.05)
            assert turns == []

            pipe_input.send_text("\r")
            await asyncio.wait_for(turn_started.wait(), timeout=1)
            assert turns == [pasted]

            pipe_input.send_text("/exit\r")
            await asyncio.wait_for(main_task, timeout=1)

    asyncio.run(scenario())


def test_cli_continues_after_failed_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_environment(monkeypatch, ENVIRONMENT)
    monkeypatch.setattr(cli_module, "print_banner", lambda: None)
    _set_cli_inputs(monkeypatch, "first", "second", "/quit")
    turns: list[str] = []

    async def fake_run_turn(
        agent: Agent,
        user_input: str,
        *,
        budget: RunBudget,
    ) -> str:
        turns.append(user_input)
        assert budget == cli_module.DEFAULT_CLI_RUN_BUDGET
        if user_input == "first":
            raise RuntimeError("first turn [/bold red] failed")
        return "second response"

    monkeypatch.setattr(cli_module, "run_turn", fake_run_turn)

    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert turns == ["first", "second"]
    assert "RuntimeError: first turn [/bold red] failed" in result.stdout
    assert "second response" in result.stdout


@pytest.mark.parametrize(
    ("input_values", "expected_reads"),
    [
        pytest.param(
            (KeyboardInterrupt(), "/exit"), 2, id="ctrl-c-keeps-session-usable"
        ),
        pytest.param((EOFError(),), 1, id="eof-exits-cleanly"),
    ],
)
def test_cli_ctrl_c_and_eof_never_start_a_turn(
    monkeypatch: pytest.MonkeyPatch,
    input_values: tuple[str | BaseException, ...],
    expected_reads: int,
) -> None:
    _set_environment(monkeypatch, ENVIRONMENT)
    monkeypatch.setattr(cli_module, "print_banner", lambda: None)
    reader = _set_cli_inputs(monkeypatch, *input_values)

    async def unexpected_run_turn(*_args: object, **_kwargs: object) -> str:
        pytest.fail("Cancelled input reached the model")

    monkeypatch.setattr(cli_module, "run_turn", unexpected_run_turn)

    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert reader.read_count == expected_reads
    assert "Goodbye! see you next time." in result.stdout


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("/help", "Available commands:"),
        ("/trace", "No trace available yet."),
        ("/exit", "Goodbye! see you next time."),
        ("/unknown", "Unknown command: /unknown"),
    ],
)
def test_interactive_commands_do_not_call_model(
    monkeypatch: pytest.MonkeyPatch, command: str, expected: str
) -> None:
    _set_environment(monkeypatch, ENVIRONMENT)
    monkeypatch.setattr(cli_module, "print_banner", lambda: None)
    _set_cli_inputs(monkeypatch, command, "/QUIT")

    async def unexpected_run_turn(*_args: object, **_kwargs: object) -> str:
        pytest.fail("Interactive command reached the model")

    monkeypatch.setattr(cli_module, "run_turn", unexpected_run_turn)

    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert expected in result.stdout


def test_interactive_trace_uses_latest_completed_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_environment(monkeypatch, ENVIRONMENT)
    monkeypatch.setattr(cli_module, "print_banner", lambda: None)
    _set_cli_inputs(monkeypatch, "hello", "/trace", "/quit")

    async def fake_run_turn(
        agent: Agent,
        user_input: str,
        *,
        budget: RunBudget,
    ) -> str:
        assert user_input == "hello"
        assert budget == cli_module.DEFAULT_CLI_RUN_BUDGET
        assert agent.tracer is not None
        span = agent.tracer.start_root_span("agent.turn")
        agent.emit(Event(type="trace_start", data={"trace_id": span.context.trace_id}))
        agent.emit(Event(type="agent_step", data={"step": 1, "max_steps": 1}))
        agent.tracer.end_span(span)
        agent.emit(
            Event(
                type="trace_finish",
                data={
                    "trace_id": span.context.trace_id,
                    "status": "ok",
                    "usage": {"input_tokens": 30, "output_tokens": 5},
                },
            )
        )
        return "done"

    monkeypatch.setattr(cli_module, "run_turn", fake_run_turn)

    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert "agent.turn" in result.stdout
    assert "step 1/1" in result.stdout
    assert result.stdout.index("done") < result.stdout.index("trace:")
    assert result.stdout.count("trace:") == 1
    assert result.stdout.count("tokens: input 30, output 5") == 1


def test_interactive_trace_persistence_failure_is_not_saved_as_latest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_environment(monkeypatch, ENVIRONMENT)
    monkeypatch.setattr(cli_module, "print_banner", lambda: None)
    _set_cli_inputs(monkeypatch, "hello", "/trace", "/quit")

    async def fake_run_turn(
        agent: Agent,
        user_input: str,
        *,
        budget: RunBudget,
    ) -> str:
        assert user_input == "hello"
        assert budget == cli_module.DEFAULT_CLI_RUN_BUDGET
        agent.emit(
            Event(
                type="trace_finish",
                data={
                    "trace_id": "failed-trace",
                    "status": "ok",
                    "persisted": False,
                    "persistence_error": "OSError: simulated trace write failure",
                },
            )
        )
        return "done"

    monkeypatch.setattr(cli_module, "run_turn", fake_run_turn)

    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert "trace unavailable: persistence failed" in result.stdout
    assert "OSError: simulated trace write failure" in result.stdout
    assert "No trace available yet." in result.stdout


def _write_trace() -> str:
    tracer = Tracer(JsonlTraceSink(Path(".cairn/traces")))
    root = tracer.start_root_span("agent.turn")
    child = tracer.start_child_span(root, "llm.generate")
    tracer.end_span(child)
    tracer.end_span(root)
    return root.context.trace_id


@pytest.mark.parametrize("action", ["show", "list", "missing"])
def test_interactive_trace_commands_route_without_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_environment(monkeypatch, ENVIRONMENT)
    monkeypatch.setattr(cli_module, "print_banner", lambda: None)
    captured: dict[str, Agent] = {}
    original_build_agent = build_agent

    def capture_agent(**kwargs: Any) -> Agent:
        agent = original_build_agent(**kwargs)
        agent.state.add_user_message("existing user turn")
        agent.state.add_assistant_message("existing assistant turn")
        captured["agent"] = agent
        return agent

    monkeypatch.setattr(cli_module, "build_agent", capture_agent)
    trace_id: str | None = None
    if action != "missing":
        trace_id = _write_trace()
    if action == "missing":
        command = "/trace deadbeef"
    elif action == "show":
        assert trace_id is not None
        command = f"/trace {trace_id[:8]}"
    else:
        command = "/trace list"
    _set_cli_inputs(monkeypatch, command, "/quit")

    async def unexpected_run_turn(*_args: object, **_kwargs: object) -> str:
        pytest.fail("Trace command reached the model")

    monkeypatch.setattr(cli_module, "run_turn", unexpected_run_turn)

    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert [message.content for message in captured["agent"].state.messages] == [
        "existing user turn",
        "existing assistant turn",
    ]
    if action == "show":
        assert trace_id is not None
        assert "agent.turn" in result.stdout
        assert "llm.generate" in result.stdout
    elif action == "list":
        assert trace_id is not None
        assert trace_id[:8] in result.stdout
    else:
        assert "Trace not found" in result.stdout


@pytest.mark.parametrize("failure", ("corrupt-jsonl", "unreadable-file"))
def test_interactive_trace_read_errors_keep_session_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_environment(monkeypatch, ENVIRONMENT)
    monkeypatch.setattr(cli_module, "print_banner", lambda: None)
    if failure == "corrupt-jsonl":
        trace_root = tmp_path / ".cairn" / "traces"
        trace_root.mkdir(parents=True)
        (trace_root / "corrupt.jsonl").write_text("{invalid json\n", encoding="utf-8")
        trace_command = "/trace list"
        expected_error = "json"
    else:
        trace_id = _write_trace()
        trace_path = Path(".cairn/traces") / f"{trace_id}.jsonl"
        original_read_text = Path.read_text

        def fail_trace_read(
            path: Path,
            encoding: str | None = None,
            errors: str | None = None,
        ) -> str:
            if path == trace_path:
                raise PermissionError("permission denied")
            return original_read_text(path, encoding=encoding, errors=errors)

        monkeypatch.setattr(Path, "read_text", fail_trace_read)
        trace_command = f"/trace {trace_id[:8]}"
        expected_error = "permission denied"

    _set_cli_inputs(monkeypatch, trace_command, "/help", "/quit")

    async def unexpected_run_turn(*_args: object, **_kwargs: object) -> str:
        pytest.fail("Trace command reached the model")

    monkeypatch.setattr(cli_module, "run_turn", unexpected_run_turn)

    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert expected_error in result.stdout.lower()
    assert "Available commands:" in result.stdout
    assert "Goodbye! see you next time." in result.stdout
