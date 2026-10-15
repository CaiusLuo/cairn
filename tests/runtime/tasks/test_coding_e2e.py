"""Offline Issue #44 acceptance through the real TaskRunner and native tools.

Keep artifacts with pytest --basetemp=/tmp/cairn-issue44-e2e. The task and
oracle come from the existing EvalSuite; only the provider is scripted.
"""

import asyncio
import difflib
import hashlib
import json
import os
import shlex
from pathlib import Path
from time import perf_counter
from typing import Any

import pytest
from examples.evals.coding_smoke_cases import coding_smoke_suite

from cairn.core.budget import RunBudget
from cairn.core.events import Event
from cairn.core.models import LLMResponse, LLMUsage, Message, ToolCall
from cairn.core.permissions import (
    PermissionChoice,
    PermissionRequest,
    SessionPermissionHandler,
)
from cairn.infra.git import WorktreeProvider, WorktreeState
from cairn.infra.observability.sinks import JsonlTraceSink
from cairn.infra.observability.storage import TraceStore
from cairn.infra.observability.tracer import Tracer
from cairn.infra.workspace.workspace import Workspace
from cairn.runtime.evals import FileContentEqualsCheck
from cairn.runtime.tasks import CodingTaskRunner, TaskResult, TaskSpec, TaskStatus
from tests.support.git import git, initialize_repository
from tests.support.runtime import SequenceLLM
from tests.support.sandbox import require_working_sandbox, sandbox_python

SECRET_NAME = "CAIRN_ISSUE44_TEST_SECRET"
SECRET_VALUE = "fixture-secret-must-not-reach-child-or-artifacts"


def repository_state(root: Path) -> dict[str, Any]:
    """Observe source files and index bytes without refreshing the index."""
    return {
        "head": git(root, "rev-parse", "HEAD"),
        "index": (root / ".git/index").read_bytes(),
        "staged": git(root, "diff", "--cached", "--no-ext-diff"),
        "unstaged": git(root, "diff", "--no-ext-diff"),
        "files": {
            str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(root).parts
        },
    }


@pytest.mark.parametrize(
    ("mode", "handler_present", "usage_available"),
    [
        ("completed", True, True),
        ("completed", False, False),
        ("model_failure", True, True),
        ("cancelled", True, True),
        ("external_cancel", True, True),
        ("edit_failure", True, True),
        ("claim_only", True, True),
    ],
    ids=[
        "verified",
        "no-handler-unknown-usage",
        "model-failure-after-effects",
        "cooperative-cancel-after-effects",
        "external-cancel-after-effects",
        "failed-edit-is-not-verified",
        "model-claim-is-not-verification",
    ],
)
def test_offline_coding_task_e2e(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    handler_present: bool,
    usage_available: bool,
) -> None:
    # No provider configuration or credentials are loaded. Isolate Git from
    # inherited hooks, signing, alternate indexes and user-specific settings.
    for key in tuple(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_DEFAULT_HASH", "sha1")
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-10-15T00:00:00Z")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-10-15T00:00:00Z")
    monkeypatch.setenv(SECRET_NAME, SECRET_VALUE)
    suite = coding_smoke_suite()
    item = next(c for c in suite.cases if c.case.name == "single-file-bug-fix")
    equality = next(c for c in item.checks if isinstance(c, FileContentEqualsCheck))
    path, original, expected = (
        equality.path,
        item.case.files[equality.path],
        equality.expected,
    )
    source = tmp_path / "source"
    source.mkdir()
    initialize_repository(source)
    git(source, "config", "commit.gpgsign", "false")
    for name, content in item.case.files.items():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))
        git(source, "add", "--", name)
    git(source, "commit", "-m", "existing coding smoke fixture")
    # A dirty source with distinct staged/unstaged bytes must survive unchanged.
    (source / "tracked.txt").write_text("staged source content\n")
    git(source, "add", "--", "tracked.txt")
    (source / "tracked.txt").write_text("unstaged source content\n")
    (source / "untracked.txt").write_text("preserve this source file\n")
    before = repository_state(source)
    refs = git(source, "show-ref")
    registrations = git(source, "worktree", "list", "--porcelain")
    require_working_sandbox(Workspace(source))
    provider = WorktreeProvider(
        Workspace(source),
        tmp_path / "worktrees",
        secret_env_keys=frozenset({SECRET_NAME}),
    )
    artifact = tmp_path / "evidence"
    artifact.mkdir()

    async def scenario() -> dict[str, Any]:
        events: list[Event] = []
        approvals: list[PermissionRequest] = []
        cancellation = asyncio.Event()
        at_final = asyncio.Event()
        cleaned = asyncio.Event()
        usage = LLMUsage(input_tokens=11, output_tokens=3) if usage_available else None

        def deny(request: PermissionRequest) -> PermissionChoice:
            approvals.append(request)
            return PermissionChoice.DENY

        def capture(event: Event) -> None:
            events.append(event)

        def response(*calls: ToolCall) -> LLMResponse:
            return LLMResponse(tool_calls=list(calls), usage=usage)

        async with provider.managed(str(before["head"]), None) as handle:
            work = handle.path
            assert work != source and not work.is_relative_to(source)
            assert (work / path).read_bytes() == original.encode()
            assert not (work / "untracked.txt").exists()
            work_index = (handle.admin_path / "index").read_bytes()
            outside = work.parent / "outside.txt"
            outside.write_text("outside boundary\n")
            check_command = shlex.join(
                [
                    sandbox_python(),
                    "-B",
                    "-c",
                    "import os; from src.range_utils import inclusive_count; "
                    f"assert os.getenv('{SECRET_NAME}') is None; "
                    "assert inclusive_count(4, 4) == 1; "
                    "assert inclusive_count(2, 5) == 4; print('verified')",
                ]
            )
            responses = [
                response(
                    ToolCall(id="read", name="read_file", arguments={"path": path})
                ),
                response(
                    ToolCall(
                        id="denied",
                        name="bash",
                        arguments={
                            "command": "printf forbidden > denied.txt",
                            "network_access": True,
                            "justification": "Scripted denied capability probe",
                        },
                    ),
                    ToolCall(
                        id="escape",
                        name="edit_file",
                        arguments={
                            "path": "../outside.txt",
                            "old_text": "outside boundary",
                            "new_text": "escaped",
                        },
                    ),
                ),
            ]
            if mode != "claim_only":
                responses.append(
                    response(
                        ToolCall(
                            id="edit",
                            name="edit_file",
                            arguments={
                                "path": path,
                                "old_text": "absent fragment"
                                if mode == "edit_failure"
                                else original,
                                "new_text": expected,
                            },
                        )
                    )
                )
            responses.append(
                response(
                    ToolCall(
                        id="check", name="bash", arguments={"command": check_command}
                    )
                )
            )

            class ScriptedCodingLLM(SequenceLLM):
                async def generate(
                    self,
                    messages: list[Message],
                    tools: list[dict[str, Any]] | None = None,
                ) -> LLMResponse:
                    if self.responses:
                        return await super().generate(messages, tools)
                    self.calls.append((messages, tools))
                    at_final.set()
                    if mode == "model_failure":
                        raise RuntimeError("scripted provider failure")
                    if mode in ("cancelled", "external_cancel"):
                        try:
                            await asyncio.Event().wait()
                        finally:
                            cleaned.set()
                    return LLMResponse(
                        content="Done; all checks pass (scripted claim).", usage=usage
                    )

            llm = ScriptedCodingLLM(responses)
            runner = CodingTaskRunner(
                workspace=handle.workspace,
                llm=llm,
                budget=RunBudget(max_steps=6),
                permission_handler=SessionPermissionHandler(deny)
                if handler_present
                else None,
                event_handler=capture,
                tracer=Tracer(JsonlTraceSink(artifact / "traces")),
                secret_env_keys=handle.secret_env_keys,
            )
            spec = TaskSpec(
                task_id=f"issue-44-{mode}", name=item.case.name, prompt=item.case.prompt
            )
            tasks_before = asyncio.all_tasks()
            started = perf_counter()
            running = asyncio.create_task(
                runner.run(spec, cancellation_event=cancellation)
            )
            await asyncio.wait_for(at_final.wait(), 15)
            result: TaskResult | None = None
            if mode == "external_cancel":
                running.cancel("scripted caller cancellation")
                with pytest.raises(asyncio.CancelledError, match="scripted caller"):
                    await running
            else:
                if mode == "cancelled":
                    cancellation.set()
                result = await running
            elapsed = perf_counter() - started
            assert asyncio.all_tasks() == tasks_before
            assert cleaned.is_set() == (mode in ("cancelled", "external_cancel"))

            # The model must observe real read, permission, path and shell results.
            observed = {
                m.tool_call_id: json.loads(m.content or "{}")
                for m in llm.calls[-1][0]
                if m.role == "tool"
            }
            assert original.strip() in observed["read"]["stdout"]
            assert observed["denied"]["type"] == (
                "PermissionDenied" if handler_present else "PermissionRequired"
            )
            assert observed["escape"]["type"] == "InvalidArguments"
            assert len(approvals) == (1 if handler_present else 0)
            assert not (work / "denied.txt").exists()
            assert outside.read_bytes() == b"outside boundary\n"
            repaired = mode not in ("edit_failure", "claim_only")
            assert observed["check"]["exit_code"] == (0 if repaired else 1)
            if repaired:
                assert observed["check"]["stdout"] == "verified\n"
            if mode == "edit_failure":
                assert observed["edit"]["type"] == "ValueError"

            checks = [await check.evaluate(handle.workspace) for check in item.checks]
            verified = all(check.passed and check.error is None for check in checks)
            assert verified is repaired
            actual = (work / path).read_bytes()
            assert actual == (expected if repaired else original).encode()
            assert git(work, "rev-parse", "HEAD") == before["head"]
            assert (handle.admin_path / "index").read_bytes() == work_index
            assert git(work, "diff", "--cached", "--exit-code") == ""
            assert git(work, "diff", "--name-only") == (path if repaired else "")
            assert git(work, "ls-files", "--others", "--exclude-standard") == ""
            patch = git(work, "diff", "--no-ext-diff", "--no-color", "--", path)
            expected_patch = "".join(
                difflib.unified_diff(
                    original.splitlines(keepends=True),
                    (expected if repaired else original).splitlines(keepends=True),
                    fromfile=f"a/{path}",
                    tofile=f"b/{path}",
                )
            )
            assert patch[patch.find("--- ") :].strip() == expected_patch.strip()
            assert repository_state(source) == before

            finish = events[-1]
            assert events[0].type == "trace_start" and finish.type == "trace_finish"
            trace_id = finish.data["trace_id"]
            spans = TraceStore(artifact / "traces").read(trace_id)
            assert spans and all(s.end_time is not None for s in spans)
            assert all(s.context.trace_id == trace_id for s in spans)
            permission = next(
                s
                for s in spans
                if s.name == "permission.check"
                and s.attributes["tool_call_id"] == "denied"
            )
            assert permission.attributes["allowed"] is False
            assert permission.attributes["granted_capabilities"] == []
            assert permission.attributes["source"] == (
                "user_denied" if handler_present else "no_handler"
            )
            assert not any(
                s.name == "tool.execute" and s.attributes["tool_call_id"] == "denied"
                for s in spans
            )
            assert not any(
                s.name in ("permission.check", "tool.execute")
                and s.attributes["tool_call_id"] == "escape"
                for s in spans
            )
            assert any(
                s.name == "tool.execute" and s.attributes["tool_call_id"] == "check"
                for s in spans
            )
            usage_data = finish.data.get("usage", {})
            if (
                mode in ("model_failure", "cancelled", "external_cancel")
                or not usage_available
            ):
                assert usage_data == {}
            else:
                assert usage_data == {
                    "input_tokens": 11 * len(llm.calls),
                    "output_tokens": 3 * len(llm.calls),
                }
            if result is not None:
                expected_status = {
                    "model_failure": TaskStatus.RUNTIME_ERROR,
                    "cancelled": TaskStatus.CANCELLED,
                }.get(mode, TaskStatus.COMPLETED)
                assert result.status is expected_status
                assert result.trace_id == trace_id
                assert result.repository.head_revision == before["head"]
                assert result.repository.changed_files == ((path,) if repaired else ())
                assert result.repository.untracked_files == ()
                if expected_status is TaskStatus.COMPLETED:
                    assert (
                        result.final_response
                        == "Done; all checks pass (scripted claim)."
                    )
                else:
                    assert result.final_response is None

            # Keep execution status distinct from deterministic correctness.
            (artifact / "actual.bin").write_bytes(actual)
            (artifact / "expected.bin").write_bytes(expected.encode())
            (artifact / "actual.patch").write_text(patch + "\n" if patch else "")
            (artifact / "expected.patch").write_text(expected_patch)
            report = {
                "contract": "issue-44-offline-v1",
                "harness_revision": git(
                    Path(__file__).resolve().parents[3], "rev-parse", "HEAD"
                ),
                "suite": suite.name,
                "case": item.case.name,
                "task_id": spec.task_id,
                "session_id": None,
                "turn_id": None,
                "task_result": result.model_dump(mode="json") if result else None,
                "external_cancellation": mode == "external_cancel",
                "trace_id": trace_id,
                "elapsed_seconds": elapsed,
                "usage_source": "scripted; not real-provider usage",
                "input_tokens": usage_data.get("input_tokens"),
                "output_tokens": usage_data.get("output_tokens"),
                "cost": None,
                "model_calls": len(llm.calls),
                "tool_results": [e.data for e in events if e.type == "tool_result"],
                "checks": [check.model_dump(mode="json") for check in checks],
                "verified": verified,
                "fixture_head": before["head"],
                "actual_sha256": hashlib.sha256(actual).hexdigest(),
                "expected_sha256": hashlib.sha256(expected.encode()).hexdigest(),
                "source_preserved": True,
                "worktree_head_index_preserved": True,
                "live_status": "LIVE_BLOCKED",
            }
        assert handle.state is WorktreeState.RELEASED
        assert not handle.path.exists()
        return report

    report = asyncio.run(asyncio.wait_for(scenario(), 25))
    assert repository_state(source) == before
    assert git(source, "show-ref") == refs
    assert git(source, "worktree", "list", "--porcelain") == registrations
    report["worktree_cleanup_verified"] = True
    (artifact / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    assert SECRET_VALUE not in "".join(p.read_text() for p in artifact.rglob("*.json*"))
