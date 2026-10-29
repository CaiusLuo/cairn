"""Offline E2E for the server single-task headless entry.

Uses the real ``CodingTaskRunner`` / ``run_turn`` boundary with a scripted model;
no provider, credentials or network. Exercises authorization, shell denial/grant,
budget, cancellation, credential redaction and the machine-readable result.
"""

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from cairn.core.budget import RunBudget
from cairn.core.context import ContextBudget
from cairn.core.llm import LLMClient
from cairn.core.models import LLMResponse, Message, ToolCall
from cairn.infra.config import ConfigLayout
from cairn.infra.observability.sinks import JsonlTraceSink, RedactingTraceSink
from cairn.infra.observability.tracer import Tracer
from cairn.terminal.headless import (
    HeadlessResult,
    HeadlessStatus,
    ProviderApproval,
    run_headless_task,
    task_app,
)
from tests.support.runtime import SequenceLLM
from tests.support.sandbox import require_local_shell

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _set_color_env(monkeypatch: pytest.MonkeyPatch, force: bool) -> None:
    for name in ("FORCE_COLOR", "PY_COLORS", "CLICOLOR_FORCE"):
        monkeypatch.delenv(name, raising=False)
    if force:
        # Rich also needs a color-capable TERM and no NO_COLOR override.
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
        monkeypatch.setenv("TERM", "xterm-256color")
    else:
        # Deterministically colorless regardless of the ambient environment.
        monkeypatch.setenv("NO_COLOR", "1")
        monkeypatch.setenv("TERM", "dumb")


def _tracer(tmp_path: Path) -> Tracer:
    return Tracer(JsonlTraceSink(tmp_path / "traces"))


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    return ws


def _run(**kwargs: object):  # type: ignore[no-untyped-def]
    return asyncio.run(run_headless_task(**kwargs))  # type: ignore[arg-type]


def test_unauthorized_launch_has_no_side_effects(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    llm = SequenceLLM([LLMResponse(content="should never run")])
    result = _run(
        workspace=ws,
        prompt="do something",
        grant_workspace_trust=False,
        grant_shell=True,
        llm=llm,
        budget=RunBudget(max_steps=2),
    )
    assert result.status is HeadlessStatus.UNAUTHORIZED
    assert result.error_category == "workspace_trust_required"
    assert result.verified is False
    assert llm.calls == []  # the model was never consulted


def test_authorized_file_edit_completes_but_not_verified(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    original = "def f():\n    return 1\n"
    fixed = "def f():\n    return 2\n"
    llm = SequenceLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="edit-1",
                        name="edit_file",
                        arguments={
                            "path": "src/mod.py",
                            "old_text": original,
                            "new_text": fixed,
                        },
                    )
                ]
            ),
            LLMResponse(content="done"),
        ]
    )
    result = _run(
        workspace=ws,
        prompt="make f return 2",
        grant_workspace_trust=True,
        grant_shell=False,
        llm=llm,
        budget=RunBudget(max_steps=4),
        tracer=_tracer(tmp_path),
    )
    assert result.status is HeadlessStatus.COMPLETED
    assert result.verified is False  # normal completion is not verification
    assert result.shell_authorized is False
    assert result.permission_denied is False
    assert result.trace_id is not None
    assert result.trace_persisted is True
    assert (ws / "src" / "mod.py").read_text(encoding="utf-8") == fixed
    assert (tmp_path / "traces" / f"{result.trace_id}.jsonl").is_file()


def test_shell_denied_when_not_authorized(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    llm = SequenceLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="sh", name="bash", arguments={"command": "touch pwned.txt"}
                    )
                ]
            ),
            LLMResponse(content="done"),
        ]
    )
    result = _run(
        workspace=ws,
        prompt="touch a file",
        grant_workspace_trust=True,
        grant_shell=False,
        llm=llm,
        budget=RunBudget(max_steps=4),
    )
    assert result.permission_denied is True
    assert "bash" in result.denied_tools
    assert not (ws / "pwned.txt").exists()  # denial precedes any effect


def test_shell_runs_from_workspace_when_authorized(tmp_path: Path) -> None:
    require_local_shell()
    ws = _workspace(tmp_path)
    llm = SequenceLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="sh",
                        name="bash",
                        arguments={"command": "printf hi > ran.txt"},
                    )
                ]
            ),
            LLMResponse(content="done"),
        ]
    )
    result = _run(
        workspace=ws,
        prompt="write ran.txt",
        grant_workspace_trust=True,
        grant_shell=True,
        llm=llm,
        budget=RunBudget(max_steps=4),
    )
    assert result.status is HeadlessStatus.COMPLETED
    assert result.permission_denied is False
    assert (ws / "ran.txt").read_text(encoding="utf-8") == "hi"


def test_budget_exhausted_is_distinguished(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    llm = SequenceLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id=f"r{i}",
                        name="read_file",
                        arguments={"path": "missing.txt"},
                    )
                ]
            )
            for i in range(10)
        ]
    )
    result = _run(
        workspace=ws,
        prompt="loop forever",
        grant_workspace_trust=True,
        grant_shell=False,
        llm=llm,
        budget=RunBudget(max_steps=2),
    )
    assert result.status is HeadlessStatus.BUDGET_EXHAUSTED
    assert result.verified is False


def test_cooperative_cancellation_is_distinguished(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    event = asyncio.Event()
    event.set()
    llm = SequenceLLM([LLMResponse(content="never")])
    result = _run(
        workspace=ws,
        prompt="cancel me",
        grant_workspace_trust=True,
        grant_shell=False,
        llm=llm,
        budget=RunBudget(max_steps=4),
        cancellation_event=event,
    )
    assert result.status is HeadlessStatus.CANCELLED


def test_configured_secret_is_redacted_from_result(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    secret = "sk-live-super-secret-xyz"

    class _LeakyLLM(LLMClient):
        model = "leaky"

        async def generate(
            self, messages: list[Message], tools: list[dict[str, object]] | None = None
        ) -> LLMResponse:
            raise RuntimeError(f"provider rejected key {secret}")

    result = _run(
        workspace=ws,
        prompt="run",
        grant_workspace_trust=True,
        grant_shell=False,
        llm=_LeakyLLM(),
        budget=RunBudget(max_steps=2),
        secret_values=(secret,),
    )
    assert result.status is HeadlessStatus.RUNTIME_ERROR
    assert secret not in result.model_dump_json()


def test_cli_unauthorized_is_offline_and_scriptable(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        task_app,
        ["--workspace", str(ws), "--prompt", "x", "--json"],
    )
    assert result.exit_code == 5
    payload = json.loads(result.stdout)
    assert payload["status"] == "unauthorized"
    assert payload["error_category"] == "workspace_trust_required"
    assert payload["verified"] is False
    # Refused before any model/credential access; nothing was written.
    assert sorted(p.name for p in ws.iterdir()) == ["src"]


@pytest.mark.parametrize("force_color", [False, True], ids=["uncolored", "colored"])
def test_cli_help_is_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, force_color: bool
) -> None:
    _set_color_env(monkeypatch, force_color)
    result = CliRunner().invoke(task_app, ["--help"])
    assert result.exit_code == 0
    if force_color:
        assert "\x1b[" in result.stdout  # the colored case is actually colored
    else:
        assert "\x1b[" not in result.stdout
    assert "--trust-workspace" in _strip_ansi(result.stdout)


# --- Review-driven coverage ---------------------------------------------------

import types  # noqa: E402

import cairn.terminal.headless as headless  # noqa: E402
from cairn.core.context import ContextBuilder  # noqa: E402
from cairn.core.tasks import TaskSpec  # noqa: E402
from cairn.core.tasks import TaskStatus as _TaskStatus  # noqa: E402


def _legacy_toml(ws: Path) -> None:
    cfg = ws / ".cairn"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "models.toml").write_text(
        'base_url = "https://x.test/v1"\n'
        'api_key_env = "CAIRN_TEST_KEY"\n'
        "\n"
        "[[models]]\n"
        'name = "primary"\n'
        'model_ids = ["openai/test"]\n',
        encoding="utf-8",
    )


def test_config_is_read_from_workspace_not_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _workspace(tmp_path)
    _legacy_toml(ws)
    seen: list[Path] = []

    def fake_load(path: Path):  # type: ignore[no-untyped-def]
        seen.append(path)
        raise ValueError("stop-after-path")

    monkeypatch.setattr(headless, "load_project_providers", fake_load)
    result = CliRunner().invoke(
        task_app,
        ["--workspace", str(ws), "--prompt", "x", "--trust-workspace", "--json"],
    )
    assert result.exit_code == 1  # configuration_error, but path was observed first
    assert seen and seen[0] == ws / ".cairn" / "models.toml"
    payload = json.loads(result.stdout)
    assert payload["error_category"] == "configuration_error"


def test_provider_approval_required_before_credential_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _workspace(tmp_path)
    _legacy_toml(ws)
    resolved: list[str] = []

    def fail_resolve(*a: object, **k: object) -> str:
        resolved.append("called")
        raise AssertionError("credential must not be resolved before approval")

    monkeypatch.setattr(headless, "resolve_provider_api_key", fail_resolve)
    result = CliRunner().invoke(
        task_app,
        ["--workspace", str(ws), "--prompt", "x", "--trust-workspace", "--json"],
    )
    assert result.exit_code == 5
    payload = json.loads(result.stdout)
    assert payload["status"] == "unauthorized"
    assert payload["error_category"] == "provider_approval_required"
    assert resolved == []  # no credential resolution without approval


def test_run_headless_task_propagates_external_cancellation(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)

    class _BlockLLM(LLMClient):
        model = "block"

        async def generate(
            self, messages: list[Message], tools: list[dict[str, object]] | None = None
        ) -> LLMResponse:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    async def scenario() -> None:
        task = asyncio.create_task(
            run_headless_task(
                workspace=ws,
                prompt="block",
                grant_workspace_trust=True,
                grant_shell=False,
                llm=_BlockLLM(),
                budget=RunBudget(max_steps=2),
            )
        )
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_run_headless_task_forwards_model_executor_and_context_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _workspace(tmp_path)
    captured: dict[str, object] = {}
    sentinel_executor = object()
    sentinel_builder = ContextBuilder()

    class _FakeRunner:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        async def run(self, spec: TaskSpec, *, cancellation_event=None):  # type: ignore[no-untyped-def]
            return types.SimpleNamespace(
                task_id=spec.task_id,
                status=_TaskStatus.COMPLETED,
                final_response="ok",
                trace_id="t",
                error=None,
            )

    monkeypatch.setattr(headless, "CodingTaskRunner", _FakeRunner)
    result = asyncio.run(
        run_headless_task(
            workspace=ws,
            prompt="x",
            grant_workspace_trust=True,
            grant_shell=False,
            llm=SequenceLLM([]),
            budget=RunBudget(max_steps=2),
            context_builder=sentinel_builder,
            model_executor=sentinel_executor,  # type: ignore[arg-type]
        )
    )
    assert result.status is HeadlessStatus.COMPLETED
    assert captured["model_executor"] is sentinel_executor
    assert captured["context_builder"] is sentinel_builder


def test_run_via_config_uses_build_provider_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _workspace(tmp_path)
    fake_llm = object()
    fake_runtime = types.SimpleNamespace(
        llm=fake_llm,
        context_builder=ContextBuilder(),
        model_executor=object(),
    )
    calls: dict[str, object] = {}

    def fake_load(path: Path):  # type: ignore[no-untyped-def]
        return types.SimpleNamespace(layout=ConfigLayout.ENV, providers=())

    def fake_default(project, environ, env_file_values):  # type: ignore[no-untyped-def]
        config = types.SimpleNamespace(
            model_config=[types.SimpleNamespace(name="g", model_ids=["m"])],
            base_url="https://x.test/v1",
            api_key_env="K",
        )
        return types.SimpleNamespace(name="p", config=config)

    def fake_build(provider, credential, context_budget, *, group=None):  # type: ignore[no-untyped-def]
        calls["group"] = group
        return fake_runtime

    captured: dict[str, object] = {}

    async def fake_run(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return HeadlessResult(
            task_id=kwargs["task_id"] or "t",
            status=HeadlessStatus.COMPLETED,
            workspace=str(kwargs["workspace"]),
        )

    monkeypatch.setattr(headless, "load_project_providers", fake_load)
    monkeypatch.setattr(headless, "default_provider", fake_default)
    monkeypatch.setattr(headless, "resolve_provider_api_key", lambda *a, **k: "key")
    monkeypatch.setattr(
        headless, "resolve_context_budget", lambda *a, **k: ContextBudget()
    )
    monkeypatch.setattr(headless, "build_provider_runtime", fake_build)
    monkeypatch.setattr(headless, "run_headless_task", fake_run)

    result = headless._run_via_config(
        workspace=ws,
        prompt="p",
        task_id="t1",
        grant_shell=False,
        approval=ProviderApproval("p", "https://x.test/v1", "g", "K", ("m",)),
        max_steps=7,
        trace_dir=tmp_path / "traces",
    )
    assert result.status is HeadlessStatus.COMPLETED
    assert calls["group"] == "g"
    assert captured["llm"] is fake_llm
    assert captured["model_executor"] is fake_runtime.model_executor
    assert captured["context_builder"] is fake_runtime.context_builder
    assert captured["budget"].max_steps == 7  # type: ignore[attr-defined]


def test_provider_approval_mismatch_rejected_before_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _workspace(tmp_path)
    _legacy_toml(ws)  # base_url https://x.test/v1, group "primary"
    resolved: list[str] = []

    def fail_resolve(*a: object, **k: object) -> str:
        resolved.append("called")
        raise AssertionError("must not resolve before a matching approval")

    monkeypatch.setattr(headless, "resolve_provider_api_key", fail_resolve)
    result = CliRunner().invoke(
        task_app,
        [
            "--workspace",
            str(ws),
            "--prompt",
            "x",
            "--trust-workspace",
            "--approve-provider-name",
            "default",
            "--approve-provider-endpoint",
            "https://wrong.test/v1",
            "--approve-provider-group",
            "primary",
            "--approve-provider-key-env",
            "CAIRN_TEST_KEY",
            "--approve-provider-models",
            "openai/test",
            "--json",
        ],
    )
    assert result.exit_code == 5
    payload = json.loads(result.stdout)
    assert payload["error_category"] == "provider_approval_mismatch"
    assert resolved == []


def test_provider_approval_match_allows_credential_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _workspace(tmp_path)
    _legacy_toml(ws)
    calls: list[str] = []

    def stop_resolve(config, *a, **k):  # type: ignore[no-untyped-def]
        calls.append(config.base_url)
        raise ValueError("stop-after-approval")

    monkeypatch.setattr(headless, "resolve_provider_api_key", stop_resolve)
    result = CliRunner().invoke(
        task_app,
        [
            "--workspace",
            str(ws),
            "--prompt",
            "x",
            "--trust-workspace",
            "--approve-provider-name",
            "default",
            "--approve-provider-endpoint",
            "https://x.test/v1",
            "--approve-provider-group",
            "primary",
            "--approve-provider-key-env",
            "CAIRN_TEST_KEY",
            "--approve-provider-models",
            "openai/test",
            "--json",
        ],
    )
    assert result.exit_code == 1
    assert calls == ["https://x.test/v1"]  # exactly the approved endpoint resolved


def _write_env(ws: Path, base_url: str, model: str) -> None:
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "src").mkdir(exist_ok=True)
    (ws / ".env").write_text(
        f"CAIRN_BASE_URL={base_url}\nCAIRN_LLM_MODEL={model}\nCAIRN_LLM_API_KEY=k\n",
        encoding="utf-8",
    )


def test_dotenv_and_provider_read_from_each_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("CAIRN_BASE_URL", "CAIRN_LLM_MODEL", "CAIRN_LLM_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    ws1 = tmp_path / "p1" / "ws"
    ws2 = tmp_path / "p2" / "ws"
    _write_env(ws1, "https://one.test/v1", "model-one")
    _write_env(ws2, "https://two.test/v1", "model-two")
    seen: list[tuple[str, str | None]] = []

    def fake_build(provider, credential, context_budget, *, group=None):  # type: ignore[no-untyped-def]
        seen.append((provider.config.base_url, group))
        return types.SimpleNamespace(
            llm=object(), context_builder=ContextBuilder(), model_executor=object()
        )

    async def fake_run(**kwargs):  # type: ignore[no-untyped-def]
        return HeadlessResult(
            task_id="t",
            status=HeadlessStatus.COMPLETED,
            workspace=str(kwargs["workspace"]),
        )

    monkeypatch.setattr(headless, "build_provider_runtime", fake_build)
    monkeypatch.setattr(headless, "run_headless_task", fake_run)

    for ws, base, model in (
        (ws1, "https://one.test/v1", "model-one"),
        (ws2, "https://two.test/v1", "model-two"),
    ):
        headless._run_via_config(
            workspace=ws,
            prompt="p",
            task_id="t",
            grant_shell=False,
            approval=ProviderApproval(
                "default", base, model, "CAIRN_LLM_API_KEY", (model,)
            ),
            max_steps=3,
            trace_dir=tmp_path / "traces",
        )
    assert seen == [
        ("https://one.test/v1", "model-one"),
        ("https://two.test/v1", "model-two"),
    ]


@pytest.mark.parametrize("force_color", [False, True], ids=["uncolored", "colored"])
def test_registered_under_task_subcommand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, force_color: bool
) -> None:
    from cairn.terminal.cli import app as cairn_cli

    _set_color_env(monkeypatch, force_color)
    result = CliRunner().invoke(cairn_cli, ["task", "run", "--help"])
    assert result.exit_code == 0
    if force_color:
        assert "\x1b[" in result.stdout
    else:
        assert "\x1b[" not in result.stdout
    assert "--trust-workspace" in _strip_ansi(result.stdout)


def test_provider_approval_binds_ordered_model_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _workspace(tmp_path)
    _legacy_toml(ws)  # model_ids = ["openai/test"]
    resolved: list[str] = []

    def fail_resolve(*a: object, **k: object) -> str:
        resolved.append("called")
        raise AssertionError("must not resolve on model-id mismatch")

    monkeypatch.setattr(headless, "resolve_provider_api_key", fail_resolve)
    result = CliRunner().invoke(
        task_app,
        [
            "--workspace",
            str(ws),
            "--prompt",
            "x",
            "--trust-workspace",
            "--approve-provider-name",
            "default",
            "--approve-provider-endpoint",
            "https://x.test/v1",
            "--approve-provider-group",
            "primary",
            "--approve-provider-key-env",
            "CAIRN_TEST_KEY",
            "--approve-provider-models",
            "openai/other",
            "--json",
        ],
    )
    assert result.exit_code == 5
    assert json.loads(result.stdout)["error_category"] == "provider_approval_mismatch"
    assert resolved == []


def test_redacting_trace_sink_unit() -> None:
    from cairn.infra.observability.models import (
        SpanStatus,
        new_trace_context,
        start_span,
    )

    secret = "SECRET-VALUE-abc"
    collected: list[object] = []

    class _Sink:
        def emit(self, span: object) -> None:
            collected.append(span)

    span = start_span(
        "llm.generate",
        new_trace_context(),
        {"command": f"echo {secret}", "count": 3, "nested": [f"x{secret}"]},
    )
    span.end_time = span.start_time
    span.status = SpanStatus.OK
    span.error = f"RuntimeError: provider said {secret}"
    RedactingTraceSink(_Sink(), (secret,)).emit(span)
    written = collected[0]
    dump = written.model_dump_json()  # type: ignore[attr-defined]
    assert secret not in dump
    assert "[redacted]" in dump
    # Non-string values survive.
    assert written.attributes["count"] == 3  # type: ignore[attr-defined]


def test_trace_jsonl_redacts_configured_secret(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    secret = "SK-LIVE-{{LEAK-9f3c2a}}"

    class _LeakyLLM(LLMClient):
        model = "leaky"

        async def generate(
            self, messages: list[Message], tools: list[dict[str, object]] | None = None
        ) -> LLMResponse:
            raise RuntimeError(f"provider rejected key {secret}")

    tracer = Tracer(JsonlTraceSink(tmp_path / "traces"))
    result = _run(
        workspace=ws,
        prompt="run",
        grant_workspace_trust=True,
        grant_shell=False,
        llm=_LeakyLLM(),
        budget=RunBudget(max_steps=2),
        tracer=tracer,
        secret_values=(secret,),
    )
    assert result.status is HeadlessStatus.RUNTIME_ERROR
    assert secret not in result.model_dump_json()
    trace_files = list((tmp_path / "traces").glob("*.jsonl"))
    assert trace_files, "expected a persisted trace"
    trace_text = "".join(path.read_text(encoding="utf-8") for path in trace_files)
    assert secret not in trace_text  # the saved trace must not leak it
    assert "[redacted]" in trace_text


def test_cli_keyboardinterrupt_emits_cancelled_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _workspace(tmp_path)

    def boom(**kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(headless, "_run_via_config", boom)
    result = CliRunner().invoke(
        task_app,
        ["--workspace", str(ws), "--prompt", "x", "--trust-workspace", "--json"],
    )
    assert result.exit_code == 4
    payload = json.loads(result.stdout)
    assert payload["status"] == "cancelled"
    assert payload["error_category"] == "cancelled"


def test_cli_asyncio_cancellation_emits_cancelled_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _workspace(tmp_path)

    def boom(**kwargs: object) -> object:
        raise asyncio.CancelledError

    monkeypatch.setattr(headless, "_run_via_config", boom)
    result = CliRunner().invoke(
        task_app,
        ["--workspace", str(ws), "--prompt", "x", "--trust-workspace", "--json"],
    )
    assert result.exit_code == 4
    assert json.loads(result.stdout)["status"] == "cancelled"


def test_cli_missing_prompt_is_machine_readable(
    tmp_path: Path,
) -> None:
    ws = _workspace(tmp_path)
    result = CliRunner().invoke(task_app, ["--workspace", str(ws), "--json"])
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["status"] == "invalid_request"
    assert payload["error_category"] == "missing_prompt"


def test_cancelled_json_reuses_real_runner_task_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _workspace(tmp_path)
    captured: dict[str, str] = {}

    fake_provider = types.SimpleNamespace(
        name="default",
        config=types.SimpleNamespace(
            base_url="https://x.test/v1",
            api_key_env="K",
            model_config=[types.SimpleNamespace(name="g", model_ids=["m"])],
        ),
    )
    monkeypatch.setattr(
        headless,
        "load_project_providers",
        lambda path: types.SimpleNamespace(layout=ConfigLayout.ENV, providers=()),
    )
    monkeypatch.setattr(headless, "default_provider", lambda *a, **k: fake_provider)
    monkeypatch.setattr(headless, "resolve_provider_api_key", lambda *a, **k: "k")
    monkeypatch.setattr(
        headless, "resolve_context_budget", lambda *a, **k: ContextBudget()
    )
    monkeypatch.setattr(
        headless,
        "build_provider_runtime",
        lambda *a, **k: types.SimpleNamespace(
            llm=object(), context_builder=ContextBuilder(), model_executor=object()
        ),
    )

    class _FakeRunner:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def run(self, spec: TaskSpec, *, cancellation_event=None):  # type: ignore[no-untyped-def]
            captured["spec_task_id"] = spec.task_id
            raise KeyboardInterrupt

    monkeypatch.setattr(headless, "CodingTaskRunner", _FakeRunner)
    result = CliRunner().invoke(
        task_app,
        [
            "--workspace",
            str(ws),
            "--prompt",
            "x",
            "--trust-workspace",
            "--approve-provider-name",
            "default",
            "--approve-provider-endpoint",
            "https://x.test/v1",
            "--approve-provider-group",
            "g",
            "--approve-provider-key-env",
            "K",
            "--approve-provider-models",
            "m",
            "--json",
        ],
    )
    assert result.exit_code == 4
    payload = json.loads(result.stdout)
    assert payload["status"] == "cancelled"
    # The id the runner actually received equals the id in the cancelled result.
    assert captured["spec_task_id"] == payload["task_id"]
    assert payload["task_id"].startswith("task-")


def test_cli_prompt_file_invalid_utf8_is_invalid_request(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    bad = tmp_path / "bad_prompt.txt"
    bad.write_bytes(b"\xff\xfe\x00not-utf8")
    result = CliRunner().invoke(
        task_app,
        ["--workspace", str(ws), "--prompt-file", str(bad), "--json"],
    )
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["status"] == "invalid_request"
    assert payload["error_category"] == "prompt_file_unreadable"


def test_interrupted_task_observes_trace_id_for_real_sink(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    traces = tmp_path / "traces"
    tracer = Tracer(JsonlTraceSink(traces))
    trace_ref: dict[str, Any] = {}

    class _BlockLLM(LLMClient):
        model = "block"

        async def generate(
            self, messages: list[Message], tools: list[dict[str, object]] | None = None
        ) -> LLMResponse:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    async def scenario() -> None:
        task = asyncio.create_task(
            run_headless_task(
                workspace=ws,
                prompt="block",
                grant_workspace_trust=True,
                grant_shell=False,
                llm=_BlockLLM(),
                budget=RunBudget(max_steps=2),
                tracer=tracer,
                trace_ref=trace_ref,
            )
        )
        for _ in range(300):
            if trace_ref.get("trace_id"):
                break
            await asyncio.sleep(0.01)
        assert trace_ref.get("trace_id"), "trace_start was not observed"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    trace_id = trace_ref["trace_id"]
    trace_file = traces / f"{trace_id}.jsonl"
    assert trace_file.is_file(), "observed trace id must point to a real trace file"
    assert trace_file.stat().st_size > 0
    assert trace_ref.get("persisted") is True  # real sink persisted


def test_cli_cancellation_returns_trace_id_pointing_to_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _workspace(tmp_path)
    traces = tmp_path / "traces"
    traces.mkdir()
    trace_id = "abc123def456"

    def fake_run_via_config(**kwargs: object) -> object:
        ref = kwargs.get("trace_ref")
        assert isinstance(ref, dict)
        ref["trace_id"] = trace_id
        ref["persisted"] = True
        (traces / f"{trace_id}.jsonl").write_text('{"span": "x"}\n', encoding="utf-8")
        raise KeyboardInterrupt

    monkeypatch.setattr(headless, "_run_via_config", fake_run_via_config)
    result = CliRunner().invoke(
        task_app,
        ["--workspace", str(ws), "--prompt", "x", "--trust-workspace", "--json"],
    )
    assert result.exit_code == 4
    payload = json.loads(result.stdout)
    assert payload["status"] == "cancelled"
    assert payload["trace_id"] == trace_id
    assert payload["trace_persisted"] is True
    assert (traces / f"{trace_id}.jsonl").is_file()


def test_completed_returns_trace_id_but_not_persisted_when_sink_fails(
    tmp_path: Path,
) -> None:
    ws = _workspace(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    tracer = Tracer(JsonlTraceSink(blocker / "traces"))  # mkdir under a file fails
    result = _run(
        workspace=ws,
        prompt="x",
        grant_workspace_trust=True,
        grant_shell=False,
        llm=SequenceLLM([LLMResponse(content="done")]),
        budget=RunBudget(max_steps=2),
        tracer=tracer,
    )
    assert result.status is HeadlessStatus.COMPLETED
    assert result.trace_id is not None  # id kept for correlation
    assert result.trace_persisted is False  # no trace file was written
    assert not (blocker / "traces").exists()
    # No raw sink error text leaks into the result.
    assert "mkdir" not in result.model_dump_json().lower()


def test_interrupted_task_with_failing_sink_reports_not_persisted(
    tmp_path: Path,
) -> None:
    ws = _workspace(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    tracer = Tracer(JsonlTraceSink(blocker / "traces"))
    trace_ref: dict[str, Any] = {}

    class _BlockLLM(LLMClient):
        model = "block"

        async def generate(
            self, messages: list[Message], tools: list[dict[str, object]] | None = None
        ) -> LLMResponse:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    async def scenario() -> None:
        task = asyncio.create_task(
            run_headless_task(
                workspace=ws,
                prompt="block",
                grant_workspace_trust=True,
                grant_shell=False,
                llm=_BlockLLM(),
                budget=RunBudget(max_steps=2),
                tracer=tracer,
                trace_ref=trace_ref,
            )
        )
        for _ in range(300):
            if trace_ref.get("trace_id"):
                break
            await asyncio.sleep(0.01)
        assert trace_ref.get("trace_id")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert trace_ref.get("trace_id")
    assert trace_ref.get("persisted") is False
