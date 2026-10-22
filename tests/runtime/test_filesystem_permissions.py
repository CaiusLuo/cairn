"""Filesystem-access capability through the bash tool and the agent turn."""

import asyncio
import json
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from cairn.core.models import LLMResponse, ToolCall
from cairn.core.permissions import (
    PermissionChoice,
    PermissionSource,
    SessionPermissionHandler,
)
from cairn.infra.observability.tracer import Tracer
from cairn.infra.tools.base import ToolExecutionContext
from cairn.infra.tools.bash import BashTool, _filesystem_mounts
from cairn.infra.tools.filesystem_access import normalize_access_root
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.assembly import build_agent
from cairn.runtime.loop import run_turn
from tests.support.runtime import TEST_BUDGET, RecordingSink, SequenceLLM
from tests.support.sandbox import require_working_sandbox


@pytest.fixture
def outside_dir() -> Iterator[Path]:
    """A real directory under /tmp, not under TMPDIR.

    Neither sandbox grants it by default (macOS denies writes outside the
    workspace and TMPDIR; Linux hides /tmp behind an empty tmpfs), so an
    approved grant is the only way a write there can succeed on either host.
    """
    path = Path(tempfile.mkdtemp(prefix="fsx-outside-", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _fake_process() -> AsyncMock:
    process = AsyncMock()
    process.pid = 12345
    process.wait.return_value = 0
    for name in ("stdout", "stderr"):
        stream = AsyncMock(spec=asyncio.StreamReader)
        stream.read.return_value = b""
        setattr(process, name, stream)
    return process


def _capture_argv(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record the argv of each sandbox launch, faking only the launcher."""
    create = AsyncMock(return_value=_fake_process())
    calls: list[list[str]] = []

    async def record(*args: object, **kwargs: object) -> object:
        calls.append([str(token) for token in args])
        return await create(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record)
    monkeypatch.setattr("cairn.infra.tools.bash.os.killpg", Mock())
    monkeypatch.setattr("cairn.infra.tools.bash.sys.platform", "linux")
    real_is_file = Path.is_file
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda self: True if str(self) == "/usr/bin/bwrap" else real_is_file(self),
    )
    return calls


def _has_bind(argv: list[str], path: str) -> bool:
    return any(
        argv[index : index + 3] == ["--bind", path, path]
        for index in range(len(argv) - 2)
    )


def test_extra_roots_arguments_alone_do_not_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model argument can request a root but never mounts it without a grant."""
    calls = _capture_argv(monkeypatch)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "data"
    outside.mkdir()

    tool = BashTool(Workspace(workspace))
    # The argument names the root, but the execution context carries no grant.
    asyncio.run(
        tool.execute(
            {
                "command": "true",
                "extra_writable_roots": [str(outside)],
                "justification": "clean temp",
            }
        )
    )

    assert not _has_bind(calls[0], str(outside))


def test_filesystem_context_adds_only_extra_binds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _capture_argv(monkeypatch)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "data"
    outside.mkdir()
    tool = BashTool(Workspace(workspace))

    asyncio.run(tool.execute({"command": "true"}))
    asyncio.run(
        tool.execute(
            {"command": "true"},
            context=ToolExecutionContext(accessible_roots=(outside.resolve(),)),
        )
    )

    plain, granted = calls
    assert not _has_bind(plain, str(outside))
    assert _has_bind(granted, str(outside))


def test_filesystem_mounts_bind_root_and_protect_git(tmp_path: Path) -> None:
    root = tmp_path / "data"
    (root / ".git").mkdir(parents=True)

    argv = list(_filesystem_mounts((root.resolve(),)))

    bind = argv.index("--bind")
    assert argv[bind : bind + 3] == ["--bind", str(root), str(root)]
    git_index = argv.index("--ro-bind")
    assert argv[git_index : git_index + 3] == [
        "--ro-bind",
        str(root / ".git"),
        str(root / ".git"),
    ]
    # Git metadata is pinned read-only after the writable bind.
    assert git_index > bind


def test_filesystem_grant_lets_the_sandbox_write_outside(
    tmp_path: Path, outside_dir: Path
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    require_working_sandbox(Workspace(workspace))

    tool = BashTool(Workspace(workspace))
    target = outside_dir / "hello.txt"
    result = asyncio.run(
        tool.execute(
            {"command": f"printf hi > {target}"},
            context=ToolExecutionContext(accessible_roots=(outside_dir.resolve(),)),
        )
    )

    assert result.exit_code == 0, result.stderr
    assert target.read_text() == "hi"


def test_filesystem_capability_is_absent_without_grant(
    tmp_path: Path, outside_dir: Path
) -> None:
    """Without a grant the same outside write is refused; the boundary holds."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    require_working_sandbox(Workspace(workspace))
    tool = BashTool(Workspace(workspace))

    result = asyncio.run(tool.execute({"command": f"printf hi > {outside_dir}/x"}))

    assert result.exit_code != 0
    assert not (outside_dir / "x").exists()


@pytest.mark.parametrize("choice", list(PermissionChoice))
def test_filesystem_end_to_end(
    tmp_path: Path, outside_dir: Path, choice: PermissionChoice
) -> None:
    require_working_sandbox(Workspace(tmp_path))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = outside_dir / "out.txt"

    prompts: list[object] = []

    def prompt(request: object) -> PermissionChoice:
        prompts.append(request)
        return choice

    handler = SessionPermissionHandler(
        prompt,
        workspace_root=workspace,
        root_normalizer=normalize_access_root,
    )
    calls = [
        ToolCall(
            id=str(index),
            name="bash",
            arguments={
                "command": f"printf hi > {target}",
                "extra_writable_roots": [str(outside_dir)],
                "justification": "write a data file",
            },
        )
        for index in range(2)
    ]
    sink = RecordingSink()
    agent = build_agent(
        workspace=Workspace(workspace),
        event_handler=None,
        llm=SequenceLLM([LLMResponse(tool_calls=calls), LLMResponse(content="done")]),
        permission_handler=handler,
        tracer=Tracer(sink),
    )

    assert asyncio.run(run_turn(agent, "test", budget=TEST_BUDGET)) == "done"

    messages = [
        json.loads(m.content or "") for m in agent.state.messages if m.role == "tool"
    ]
    if choice is PermissionChoice.DENY:
        for message in messages:
            assert message == {
                "error": "Permission denied by user.",
                "type": "PermissionDenied",
            }
        assert not target.exists()
        return

    assert target.read_text() == "hi"
    for message in messages:
        assert message["exit_code"] == 0

    spans = [s for s in sink.spans if s.name == "permission.check"]
    assert spans[0].attributes["granted_capabilities"] == ["filesystem_access"]
    assert spans[0].attributes["granted_roots"] == [str(outside_dir.resolve())]
    reused = choice is PermissionChoice.ALLOW_SESSION
    # A session grant is reused for the second identical-root request.
    assert len(prompts) == (1 if reused else 2)
    assert spans[1].attributes["source"] == (
        PermissionSource.SESSION_GRANT.value
        if reused
        else PermissionSource.USER_ONCE.value
    )
