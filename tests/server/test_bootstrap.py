"""Real Debian process admission, shared storage and restart acceptance."""

import json
import socket
import stat
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from cairn.agent.budget import RunBudget
from cairn.agent.context.window import ContextBudget
from cairn.agent.models.catalog import NamedProvider
from cairn.agent.models.runtime import build_provider_runtime
from cairn.agent.models.selection import ModelConfig, ProviderConfig
from cairn.integrations.wechat.state import WechatServeLock
from cairn.server import bootstrap
from cairn.server.config import KnowledgeConfig
from tests.support.git import initialize_repository
from tests.support.pg import database_for
from tests.support.server_process import ROOT, ServerProcess


def configured(tmp_path: Path) -> ServerProcess:
    source = tmp_path / "source"
    source.mkdir()
    initialize_repository(source)
    return ServerProcess(source, tmp_path)


def invoke(server: ServerProcess) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cairn.server", "--config", str(server.config)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_custom_root_web_api_and_restart_share_persistent_state(tmp_path: Path) -> None:
    server = configured(tmp_path)
    try:
        server.start()
        token = (server.state / "runtime.token").read_text().strip()
        headers = {"Authorization": "Bearer " + token}
        with httpx.Client(base_url=server.url, headers=headers, trust_env=False) as api:
            for page in (
                "/chat.html",
                "/tasks.html",
                "/knowledge.html",
                "/channels.html",
                "/model.html",
                "/settings.html",
                "/index.html",
            ):
                assert api.get(page).status_code == 200
            assert api.get("/api/health").status_code == 200
            assert api.get("/api/runtime/tasks").json() == []
            assert server.process is not None
            for candidate, succeeds in ((server.process.pid, True), (1, False)):
                identity = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "cairn.server",
                        "--config",
                        str(server.config),
                        "check-running",
                        "--pid",
                        str(candidate),
                    ],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                assert (identity.returncode == 0) == succeeds, (
                    identity.stdout + identity.stderr
                )
            assert api.get("/api/runtime/bootstrap").json()["data_root"] == str(
                server.data_root
            )
            command = subprocess.run(
                [
                    str(Path(sys.executable).parent / "cairn-cli"),
                    "--config",
                    str(server.config),
                    "runtime",
                    "list",
                ],
                cwd=server.source,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert command.returncode == 0, command.stderr
            assert json.loads(command.stdout) == []
            assert api.get("/api/health").status_code == 200
        assert (server.data_root / "agent/AGENT.md").is_file()
        with database_for(server.root).connect("ops") as db:
            assert db.execute("SELECT COUNT(*) FROM schedule_state").fetchone()[0] == 0
        with database_for(server.root).connect("agent") as db:
            assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert not (server.state / "host-state").exists()
        assert not (server.state / "agent").exists()
        marker = server.data_root / "agent/MEMORY.md"
        marker.write_text("# Persisted personal memory\n")
        server.stop()
        server.start()
        assert marker.read_text() == "# Persisted personal memory\n"
        assert (server.state / "runtime.token").read_text().strip() == token
    finally:
        server.stop()


@pytest.mark.parametrize(
    "problem", ["port", "assets", "provider", "key", "toml", "wechat"]
)
def test_startup_refuses_invalid_or_unavailable_dependencies(
    tmp_path: Path, problem: str
) -> None:
    server = configured(tmp_path)
    held = socket.socket()
    try:
        text = server.config.read_text()
        if problem == "port":
            held.bind(("127.0.0.1", server.port))
            held.listen()
        elif problem == "assets":
            text += 'web_root = "' + str(tmp_path / "missing-assets") + '"\n'
        elif problem == "provider":
            text = text.replace(
                "offline = true",
                'provider_approval = "' + str(tmp_path / "missing-provider.json") + '"',
            )
        elif problem == "key":
            models = server.source / ".cairn/models.toml"
            models.parent.mkdir(exist_ok=True)
            models.write_text(
                '[[providers]]\nname = "example"\nbase_url = "https://example.invalid/v1"\napi_key_env = "CAIRN_P0A_MISSING_KEY"\n[[providers.models]]\nname = "primary"\nmodel_ids = ["openai/test"]\n'
            )
            approval = tmp_path / "provider.json"
            approval.write_text(
                json.dumps(
                    {
                        "name": "example",
                        "endpoint": "https://example.invalid/v1",
                        "group": "primary",
                        "key_env": "CAIRN_P0A_MISSING_KEY",
                        "model_ids": ["openai/test"],
                    }
                )
            )
            text = text.replace(
                "offline = true", 'provider_approval = "' + str(approval) + '"'
            )
        elif problem == "toml":
            text += 'api_key = "sentinel-secret"\n'
        elif problem == "wechat":
            server.state.mkdir(parents=True)
            (server.state / "controls.json").write_text(
                json.dumps({"version": 1, "wechat_enabled": True})
            )
        server.config.write_text(text)
        result = invoke(server)
        assert result.returncode != 0, result.stdout + result.stderr
        assert "sentinel-secret" not in result.stdout + result.stderr
        assert "failed" in (result.stdout + result.stderr).lower()
        if problem in {"port", "assets", "provider", "key", "toml"}:
            assert not (server.state / "sessions").exists()
    finally:
        held.close()


def test_same_wechat_account_is_exclusive_across_processes_and_data_roots(
    tmp_path: Path,
) -> None:
    owner = WechatServeLock(tmp_path / "first")
    owner.acquire(description="test", account_id="fake-p0a-account")
    try:
        code = (
            "from pathlib import Path; "
            "from cairn.integrations.wechat.state import WechatServeLock; "
            "lock=WechatServeLock(Path(" + repr(str(tmp_path / "second")) + ")); "
            "lock.acquire(description='second', account_id='fake-p0a-account')"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode != 0
        assert "already owns this WeChat account" in result.stderr
    finally:
        owner.release()
    successor = WechatServeLock(tmp_path / "second")
    successor.acquire(description="successor", account_id="fake-p0a-account")
    successor.release()


def test_default_worktrees_under_data_root_remain_agent_writable(
    tmp_path: Path,
) -> None:
    from tests.support.server_process import wait_for

    server = configured(tmp_path)
    server.config.write_text(
        "\n".join(
            line
            for line in server.config.read_text().splitlines()
            if not line.startswith("worktrees_root")
        )
        + "\n"
    )
    try:
        server.start()
        token = (server.state / "runtime.token").read_text().strip()
        with httpx.Client(
            base_url=server.url,
            headers={"Authorization": "Bearer " + token},
            trust_env=False,
        ) as api:
            task = api.post(
                "/api/runtime/tasks",
                json={
                    "prompt": "Create the nested worktree artifact",
                    "source": "web",
                    "mode": "development",
                },
            ).json()
            completed = wait_for(
                lambda: (
                    value
                    if (
                        value := api.get("/api/runtime/tasks/" + task["task_id"]).json()
                    )["status"]
                    == "completed"
                    else None
                )
            )
        tree = Path(completed["worktree"])
        assert tree.is_relative_to(server.data_root / "worktrees")
        assert (tree / "runtime-result.txt").is_file()
        assert not (tree / "runtime").exists()
    finally:
        server.stop()


def test_review_policy_is_visible_without_claiming_hot_reload(tmp_path: Path) -> None:
    server = configured(tmp_path)
    try:
        server.start()
        token = (server.state / "runtime.token").read_text().strip()
        with httpx.Client(
            base_url=server.url,
            headers={"Authorization": "Bearer " + token},
            trust_env=False,
        ) as api:
            review = (
                api.get("/api/runtime/management?section=review")
                .raise_for_status()
                .json()["review"]
            )
        assert set(review) >= {
            "timezone",
            "daily",
            "weekly",
            "delivery",
            "config_file",
            "hot_reload",
            "restart_required_for_changes",
        }
        assert review["hot_reload"] is False
        assert review["restart_required_for_changes"] is True
        assert review["config_file"].endswith("server.toml")
        assert token not in json.dumps(review)
    finally:
        server.stop()


@pytest.mark.parametrize(
    "endpoint,key_env,expected",
    [
        ("https://approved.invalid/v1/", "", "fake-chat-key"),
        ("https://approved.invalid/another", "", None),
        ("https://embedding.invalid/v1", "", None),
        (
            "https://embedding.invalid/v1",
            "CAIRN_TEST_EMBEDDING_KEY",
            "fake-embedding-key",
        ),
        ("", "", None),
    ],
)
def test_embedding_credential_is_bound_to_its_configured_endpoint(
    monkeypatch: pytest.MonkeyPatch, endpoint: str, key_env: str, expected: str | None
) -> None:
    monkeypatch.setenv("CAIRN_TEST_EMBEDDING_KEY", "fake-embedding-key")
    provider = NamedProvider(
        "chat",
        ProviderConfig(
            "https://approved.invalid/v1",
            "CAIRN_TEST_CHAT_KEY",
            (ModelConfig("default", ("openai/fake-chat",)),),
        ),
    )
    runtime = build_provider_runtime(provider, "fake-chat-key", ContextBudget())
    config = KnowledgeConfig(
        embedding_model="fake-model",
        embedding_base_url=endpoint,
        embedding_key_env=key_env,
    )

    client = bootstrap._embedding_client(config, runtime)
    assert client.api_key == expected
    assert client.api_base == endpoint.rstrip("/")


def test_dedicated_embedding_credential_is_in_runtime_secret_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_name = "CAIRN_TEST_EMBEDDING_KEY"
    key = "fake-embedding-key"
    monkeypatch.setenv(key_name, key)
    captured: dict[str, object] = {}

    class CapturedExecution(RuntimeError):
        pass

    def observe_execution(*args: object, **kwargs: object) -> None:
        captured.update(kwargs)
        raise CapturedExecution

    monkeypatch.setattr(bootstrap, "ServiceExecution", observe_execution)
    with pytest.raises(CapturedExecution):
        bootstrap.runtime_app(
            tmp_path / "workspace",
            tmp_path / "state",
            tmp_path / "worktrees",
            budget=RunBudget(max_steps=8),
            database=database_for(tmp_path),
            token="fake-token",
            secret_env_keys=frozenset({"CAIRN_TEST_CHAT_KEY"}),
            sensitive_values=("fake-chat-key",),
            knowledge=KnowledgeConfig(embedding_key_env=key_name),
        )

    assert captured["secret_env_keys"] == {"CAIRN_TEST_CHAT_KEY", key_name}
    assert captured["sensitive_values"] == ("fake-token", "fake-chat-key", key)


def test_ocr_extractor_is_built_only_when_enabled() -> None:
    assert bootstrap._ocr_extractor(KnowledgeConfig()) is None
    assert (
        bootstrap._ocr_extractor(KnowledgeConfig(enabled=False, ocr_enabled=True))
        is None
    )

    extractor = bootstrap._ocr_extractor(
        KnowledgeConfig(
            ocr_enabled=True,
            ocr_max_pages=7,
            ocr_dpi=150,
            ocr_max_seconds=45,
        )
    )
    assert extractor is not None
    assert extractor.settings.max_pages == 7
    assert extractor.settings.dpi == 150
    assert extractor.settings.max_seconds == 45.0


@pytest.mark.parametrize("link", ["knowledge", "knowledge/documents"])
def test_startup_rejects_symlinks_in_document_storage(
    tmp_path: Path, link: str
) -> None:
    server = configured(tmp_path)
    outside = tmp_path / "unrelated"
    outside.mkdir(mode=0o755)
    unrelated = outside / ("0" * 64)
    unrelated.write_text("unrelated file")
    initial_mode = stat.S_IMODE(outside.stat().st_mode)
    path = server.state / link
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(outside, target_is_directory=True)

    result = invoke(server)

    assert result.returncode != 0
    assert "document originals directory" in result.stderr
    assert unrelated.read_text() == "unrelated file"
    assert stat.S_IMODE(outside.stat().st_mode) == initial_mode
