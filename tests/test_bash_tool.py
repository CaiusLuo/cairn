import asyncio
from pathlib import Path

from cairn.tools.bash import BashTool


def test_bash_tool_schema_describes_required_command(tmp_path: Path) -> None:
    schema = BashTool(cwd=tmp_path).schema()

    assert schema["function"]["name"] == "bash"
    assert schema["function"]["parameters"]["required"] == ["command"]
    assert schema["function"]["parameters"]["additionalProperties"] is False


def test_bash_tool_executes_in_configured_directory(tmp_path: Path) -> None:
    result = asyncio.run(BashTool(cwd=tmp_path).execute({"command": "pwd"}))

    assert result.exit_code == 0
    assert Path(result.stdout.strip()) == tmp_path
    assert result.stderr == ""


def test_bash_tool_returns_stderr_and_exit_code(tmp_path: Path) -> None:
    result = asyncio.run(
        BashTool(cwd=tmp_path).execute(
            {"command": "printf 'failure' >&2; exit 3"},
        )
    )

    assert result.exit_code == 3
    assert result.stdout == ""
    assert result.stderr == "failure"


def test_bash_tool_kills_timed_out_process(tmp_path: Path) -> None:
    result = asyncio.run(
        BashTool(cwd=tmp_path, timeout=0.01).execute({"command": "sleep 1"})
    )

    assert result.exit_code == -1
    assert "time out after 0.01s" in result.stderr
