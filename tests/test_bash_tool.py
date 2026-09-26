import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from cairn.tools.bash import BashTool


def test_bash_tool_schema_describes_required_command(tmp_path: Path) -> None:
    schema = BashTool(cwd=tmp_path).schema()

    assert schema["function"]["name"] == "bash"
    assert schema["function"]["parameters"]["required"] == ["command"]
    assert schema["function"]["parameters"]["additionalProperties"] is False


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"command": None},
        {"command": 42},
        {"command": True},
        {"command": ""},
        {"command": " \t\n"},
        {"command": "pwd", "extra": True},
    ],
    ids=("missing", "none", "number", "boolean", "empty", "whitespace", "extra"),
)
def test_bash_tool_rejects_invalid_arguments_before_spawning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, object],
) -> None:
    create_process = AsyncMock(
        side_effect=AssertionError("Invalid arguments reached subprocess creation")
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    with pytest.raises(ValueError, match="command"):
        asyncio.run(BashTool(cwd=tmp_path).execute(arguments))

    create_process.assert_not_called()


@pytest.mark.parametrize(
    ("stdout", "stderr", "exit_code", "expected_stdout", "expected_stderr"),
    [
        (
            "中文输出".encode(),
            "中文错误".encode(),
            0,
            "中文输出",
            "中文错误",
        ),
        (b"before\xffafter", b"", 3, "before\ufffdafter", ""),
        (b"", b"warning:\xff", -1, "", "warning:\ufffd"),
    ],
    ids=("utf8", "invalid-stdout", "invalid-stderr"),
)
def test_bash_tool_preserves_command_text_and_decodes_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: bytes,
    stderr: bytes,
    exit_code: int,
    expected_stdout: str,
    expected_stderr: str,
) -> None:
    process = AsyncMock()
    process.communicate.return_value = (stdout, stderr)
    process.wait.return_value = exit_code
    create_process = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr("cairn.tools.bash.sys.platform", "darwin")
    command = "  printf '%s' 'hello'\n"

    result = asyncio.run(BashTool(cwd=tmp_path).execute({"command": command}))

    create_process.assert_awaited_once()
    assert create_process.call_args.args[-3:] == ("/bin/sh", "-c", command)
    assert result.stdout == expected_stdout
    assert result.stderr == expected_stderr
    assert result.exit_code == exit_code


def test_bash_tool_executes_in_configured_directory(tmp_path: Path) -> None:
    result = asyncio.run(BashTool(cwd=tmp_path).execute({"command": "pwd"}))

    assert result.exit_code == 0
    assert Path(result.stdout.strip()) == tmp_path
    assert result.stderr == ""


def test_auto_allowed_command_does_not_use_shell_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_ls = tmp_path / "ls"
    fake_ls.write_text("#!/bin/sh\nprintf hacked > pwned\n", encoding="utf-8")
    fake_ls.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/bin:/usr/bin")

    result = asyncio.run(BashTool(cwd=tmp_path).execute({"command": "ls"}))

    assert result.exit_code == 0
    assert not (tmp_path / "pwned").exists()


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


def test_bash_tool_confines_files_to_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path.parent))
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("private", encoding="utf-8")
    tool = BashTool(cwd=tmp_path)

    read = asyncio.run(tool.execute({"command": f"cat {outside}"}))
    asyncio.run(tool.execute({"command": f"printf data > {outside}"}))
    inside = asyncio.run(tool.execute({"command": "printf data > inside.txt"}))

    assert read.exit_code != 0
    assert "private" not in read.stdout
    assert outside.read_text(encoding="utf-8") == "private"
    assert inside.exit_code == 0
    assert (tmp_path / "inside.txt").read_text(encoding="utf-8") == "data"


def test_bash_tool_does_not_pass_api_key_to_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAIRN_LLM_API_KEY", "private-key")

    result = asyncio.run(
        BashTool(cwd=tmp_path).execute({"command": 'printf %s "$CAIRN_LLM_API_KEY"'})
    )

    assert result.exit_code == 0
    assert result.stdout == ""
