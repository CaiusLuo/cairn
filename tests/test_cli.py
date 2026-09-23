import asyncio
import builtins
from pathlib import Path

import pytest
from typer.testing import CliRunner

import cairn.cli as cli_module
from cairn.cli import app
from cairn.core.agent import Agent
from cairn.observability.sinks import JsonlTraceSink
from cairn.observability.tracer import Tracer

runner = CliRunner()

ENVIRONMENT: dict[str, str] = {
    "CAIRN_LLM_MODEL": "provider/model",
    "CAIRN_LLM_API_KEY": "secret",
    "CAIRN_BASE_URL": "https://example.test/v1",
}


def _disable_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    def load_nothing() -> bool:
        return False

    monkeypatch.setattr(cli_module, "load_dotenv", load_nothing)


def _set_environment(
    monkeypatch: pytest.MonkeyPatch,
    environment: dict[str, str],
) -> None:
    _disable_dotenv(monkeypatch)
    for name in ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ({}, "CAIRN_LLM_MODEL"),
        ({"CAIRN_LLM_MODEL": "model"}, "CAIRN_LLM_API_KEY"),
        (
            {
                "CAIRN_LLM_MODEL": "model",
                "CAIRN_LLM_API_KEY": "key",
            },
            "CAIRN_BASE_URL",
        ),
    ],
)
def test_main_requires_configuration(
    monkeypatch: pytest.MonkeyPatch,
    environment: dict[str, str],
    message: str,
) -> None:
    _set_environment(monkeypatch, environment)

    with pytest.raises(ValueError, match=message):
        asyncio.run(cli_module.main())


def test_cli_without_command_starts_and_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_environment(monkeypatch, ENVIRONMENT)
    monkeypatch.setattr(cli_module, "print_banner", lambda: None)
    monkeypatch.setattr(builtins, "input", lambda _prompt: "/exit")

    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert "Goodbye! see you next time." in result.stdout


def test_main_runs_turn_and_prints_response(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_environment(monkeypatch, ENVIRONMENT)
    monkeypatch.setattr(cli_module, "print_banner", lambda: None)
    inputs = iter(("hello", "/quit"))
    monkeypatch.setattr(builtins, "input", lambda _prompt: next(inputs))
    responses: list[str] = []

    async def fake_run_turn(
        agent: Agent,
        user_input: str,
        max_steps: int = 20,
    ) -> str:
        assert agent.tools.get_tool("bash").name == "bash"
        assert max_steps == 20
        return f"reply to {user_input}"

    def record_response(content: str) -> None:
        responses.append(content)

    monkeypatch.setattr(cli_module, "run_turn", fake_run_turn)
    monkeypatch.setattr(cli_module, "print_assistant_response", record_response)

    asyncio.run(cli_module.main())

    assert responses == ["reply to hello"]


def _write_trace() -> str:
    tracer = Tracer(JsonlTraceSink(Path(".cairn/traces")))
    root = tracer.start_root_span("agent.turn")
    child = tracer.start_child_span(root, "llm.generate")
    tracer.end_span(child)
    tracer.end_span(root)
    return root.context.trace_id


def test_trace_show_renders_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    trace_id = _write_trace()

    result = runner.invoke(app, ["trace", "show", trace_id[:8]])

    assert result.exit_code == 0
    assert "agent.turn" in result.stdout
    assert "llm.generate" in result.stdout


def test_trace_show_reports_missing_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["trace", "show", "deadbeef"])

    assert result.exit_code == 1
    assert "Trace not found" in result.stdout


def test_trace_show_does_not_require_llm_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_environment(monkeypatch, {})
    trace_id = _write_trace()

    async def unexpected_main() -> None:
        pytest.fail("trace show initialized the agent runtime")

    monkeypatch.setattr(cli_module, "main", unexpected_main)

    result = runner.invoke(app, ["trace", "show", trace_id[:8]])

    assert result.exit_code == 0
    assert "agent.turn" in result.stdout
