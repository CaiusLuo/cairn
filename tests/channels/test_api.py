"""Host-authorized channel REST surface and its secret-handling contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.testclient import TestClient

from cairn.channels.adapter import ChannelAdapterRegistry
from cairn.channels.manager import ChannelManager
from cairn.channels.store import ChannelStore
from cairn.channels.wechat import LEGACY_WECHAT_ID, wechat_legacy_adapter
from cairn.interfaces.web.app import create_app
from cairn.interfaces.web.config import WebConfig
from cairn.shared.action import Action
from tests.channels.support import (
    CapturingFactory,
    FakeIntake,
    make_manager,
)

PASSPHRASE = "channel-passphrase"
ORIGIN = "http://127.0.0.1"
TOKEN = "host-runtime-token"
TEST_SECRET = "api-" + "secret-" + "value"
OLD_SECRET = "old-" + "secret-" + "value"


def _config(tmp_path: Path) -> WebConfig:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return WebConfig(
        workspace=workspace,
        sessions_dir=tmp_path / "sessions",
        tasks_dir=tmp_path / "tasks",
        knowledge_dir=tmp_path / "knowledge",
        web_root=None,
        auth_passphrase=PASSPHRASE,
    )


def _app(
    tmp_path: Path, *, factory: CapturingFactory | None = None
) -> tuple[Starlette, ChannelManager]:
    manager, _, _, _ = make_manager(tmp_path / "channels", factory=factory)
    web = create_app(
        _config(tmp_path),
        runtime_token=TOKEN,
        channels=manager,
    )
    return web, manager


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/login",
        json={"username": "cairn", "auth_passphrase": PASSPHRASE},
    )
    assert response.status_code == 200


def _create(
    client: TestClient,
    *,
    platform: str = "telegram",
    request_id: str = "create-1",
    display_name: str = "Telegram Home",
    credentials: dict[str, str] | None = None,
    metadata: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
    expect: int = 201,
) -> dict[str, Any]:
    response = client.post(
        f"/api/channels/{platform}/connections",
        json={
            "request_id": request_id,
            "display_name": display_name,
            "enabled": True,
            "owner_identity": "owner",
            "metadata": metadata or {},
            "credentials": credentials,
        },
        headers=headers or {"Origin": ORIGIN},
    )
    assert response.status_code == expect, response.text
    return response.json() if expect < 400 else {}


def test_reads_require_login_and_writes_require_host_origin(tmp_path: Path) -> None:
    web, _ = _app(tmp_path)
    with TestClient(web, base_url=ORIGIN) as anonymous:
        assert anonymous.get("/api/channels").status_code == 401
        assert (
            anonymous.post(
                "/api/channels/telegram/connections",
                json={"request_id": "r", "display_name": "No"},
            ).status_code
            == 401
        )
    with TestClient(web, base_url=ORIGIN) as client:
        _login(client)
        assert client.get("/api/channels").status_code == 200
        response = client.post(
            "/api/channels/telegram/connections",
            json={
                "request_id": "r",
                "display_name": "No Origin",
            },
        )
        assert response.status_code == 403
        assert response.json() == {"error": "invalid_origin"}
        bearer = client.post(
            "/api/channels/telegram/connections",
            json={
                "request_id": "bearer-create",
                "display_name": "Bearer",
            },
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert bearer.status_code == 201


def test_zero_config_lists_all_four_platforms(tmp_path: Path) -> None:
    web, _ = _app(tmp_path)
    with TestClient(web, base_url=ORIGIN) as client:
        _login(client)
        payload = client.get("/api/channels").json()
        assert [row["platform"] for row in payload["platforms"]] == [
            "wechat",
            "qq",
            "telegram",
            "discord",
        ]
        assert all(
            row["display_state"] == "unconfigured" for row in payload["platforms"]
        )
        assert payload["connections"] == []


def test_create_never_returns_the_secret_and_start_stop_reconnect_work(
    tmp_path: Path,
) -> None:
    factory = CapturingFactory()
    web, _ = _app(tmp_path, factory=factory)
    with TestClient(web, base_url=ORIGIN) as client:
        _login(client)
        response = client.post(
            "/api/channels/telegram/connections",
            json={
                "request_id": "create-secret",
                "display_name": "Secret Holder",
                "enabled": True,
                "owner_identity": "owner",
                "allowed_senders": ["guest"],
                "metadata": {"label": "home"},
                "credentials": {"token": TEST_SECRET},
            },
            headers={"Origin": ORIGIN},
        )
        assert response.status_code == 201
        assert TEST_SECRET not in response.text
        body = response.json()
        connection = body["connection"]
        assert connection["credential_ref"] is not None
        assert connection["allowed_senders"] == ["guest"]
        assert body["runtime"]["actual_state"] == "stopped"
        connection_id = connection["connection_id"]

        started = client.post(
            f"/api/channels/connections/{connection_id}/start",
            json={"request_id": "start-1"},
            headers={"Origin": ORIGIN},
        )
        assert started.status_code == 200
        assert started.json()["runtime"]["actual_state"] == "running"
        assert factory.adapters[connection_id].starts == 1

        status = client.get(f"/api/channels/connections/{connection_id}/status")
        assert status.status_code == 200
        assert status.json()["actual_state"] == "running"
        assert status.json()["last_heartbeat_at"] is not None

        stopped = client.post(
            f"/api/channels/connections/{connection_id}/stop",
            json={"request_id": "stop-1"},
            headers={"Origin": ORIGIN},
        )
        assert stopped.status_code == 200
        assert stopped.json()["runtime"]["actual_state"] == "stopped"
        assert stopped.json()["connection"]["desired_state"] == "stopped"

        reconnected = client.post(
            f"/api/channels/connections/{connection_id}/reconnect",
            json={"request_id": "reconnect-1"},
            headers={"Origin": ORIGIN},
        )
        assert reconnected.status_code == 200
        assert reconnected.json()["runtime"]["actual_state"] == "running"
        assert factory.adapters[connection_id].reconnects == 1


def test_request_ids_and_revisions_are_idempotent_and_conflicts_are_explicit(
    tmp_path: Path,
) -> None:
    web, _ = _app(tmp_path)
    with TestClient(web, base_url=ORIGIN) as client:
        _login(client)
        first = _create(client, request_id="same", display_name="First")
        duplicate = _create(client, request_id="same", display_name="First")
        assert (
            duplicate["connection"]["connection_id"]
            == first["connection"]["connection_id"]
        )
        conflict = _create(
            client, request_id="same", display_name="Changed", expect=409
        )
        assert conflict == {}
        response = client.post(
            "/api/channels/telegram/connections",
            json={
                "request_id": "same",
                "display_name": "Changed",
            },
            headers={"Origin": ORIGIN},
        )
        assert response.status_code == 409
        assert response.json()["error"] == "request_id_conflict"

        connection = first["connection"]
        connection_id = connection["connection_id"]
        stale = client.patch(
            f"/api/channels/connections/{connection_id}",
            json={
                "expected_revision": 99,
                "request_id": "stale",
                "display_name": "Stale",
            },
            headers={"Origin": ORIGIN},
        )
        assert stale.status_code == 409
        assert stale.json() == {
            "error": "revision_conflict",
            "current_revision": 1,
        }


def test_update_requires_explicit_secret_replacement_and_keeps_old_secret(
    tmp_path: Path,
) -> None:
    web, manager = _app(tmp_path)
    with TestClient(web, base_url=ORIGIN) as client:
        _login(client)
        created = _create(
            client,
            request_id="update-create",
            credentials={"token": OLD_SECRET},
        )
        connection = created["connection"]
        connection_id = connection["connection_id"]
        old_ref = connection["credential_ref"]

        renamed = client.patch(
            f"/api/channels/connections/{connection_id}",
            json={
                "expected_revision": 1,
                "request_id": "rename",
                "display_name": "Renamed",
            },
            headers={"Origin": ORIGIN},
        )
        assert renamed.status_code == 200
        assert renamed.json()["connection"]["credential_ref"] == old_ref
        assert OLD_SECRET not in renamed.text

        refused = client.patch(
            f"/api/channels/connections/{connection_id}",
            json={
                "expected_revision": 2,
                "request_id": "replace-refused",
                "credentials": {"token": "new-value"},
            },
            headers={"Origin": ORIGIN},
        )
        assert refused.status_code == 422
        assert refused.json()["error"] == "explicit_credential_replacement_required"

        missing = client.patch(
            f"/api/channels/connections/{connection_id}",
            json={
                "expected_revision": 2,
                "request_id": "replace-missing",
                "replace_credentials": True,
            },
            headers={"Origin": ORIGIN},
        )
        assert missing.status_code == 422
        assert missing.json()["error"] == "credentials_required"

        replaced = client.patch(
            f"/api/channels/connections/{connection_id}",
            json={
                "expected_revision": 2,
                "request_id": "replace-ok",
                "credentials": {"token": "new-value"},
                "replace_credentials": True,
            },
            headers={"Origin": ORIGIN},
        )
        assert replaced.status_code == 200
        assert replaced.json()["connection"]["credential_ref"] != old_ref
        assert "new-value" not in replaced.text
        stored = manager.store.load_credentials(
            replaced.json()["connection"]["credential_ref"]
        )
        assert stored == {"token": "new-value"}


def test_delete_requires_confirmation_and_is_idempotent(tmp_path: Path) -> None:
    web, _ = _app(tmp_path)
    with TestClient(web, base_url=ORIGIN) as client:
        _login(client)
        created = _create(client, request_id="delete-create")
        connection_id = created["connection"]["connection_id"]

        refused = client.request(
            "DELETE",
            f"/api/channels/connections/{connection_id}",
            json={
                "expected_revision": 1,
                "request_id": "delete-1",
                "confirm": False,
            },
            headers={"Origin": ORIGIN},
        )
        assert refused.status_code == 428
        assert refused.json()["error"] == "confirmation_required"

        deleted = client.request(
            "DELETE",
            f"/api/channels/connections/{connection_id}",
            json={
                "expected_revision": 1,
                "request_id": "delete-1",
                "confirm": True,
            },
            headers={"Origin": ORIGIN},
        )
        assert deleted.status_code == 200
        assert deleted.json()["audit_retained"] is True
        assert (
            client.get(f"/api/channels/connections/{connection_id}/status").status_code
            == 404
        )

        replay = client.request(
            "DELETE",
            f"/api/channels/connections/{connection_id}",
            json={
                "expected_revision": 1,
                "request_id": "delete-1",
                "confirm": True,
            },
            headers={"Origin": ORIGIN},
        )
        assert replay.status_code == 200
        assert replay.json()["replayed"] is True


def test_legacy_wechat_is_listed_and_refuses_config_mutation(tmp_path: Path) -> None:
    web, manager = _app(tmp_path)
    enabled = {"value": False}
    controller_status = {
        "account_saved": True,
        "running": False,
        "login_pending": False,
    }
    commands: list[str] = []

    class Controller:
        def management_status(self) -> dict[str, object]:
            return dict(controller_status)

        def account_id(self) -> str:
            return "bot-a"

        def recipient_id(self) -> str:
            return "owner"

        def secret_values(self) -> tuple[str, ...]:
            return ("legacy-" + "secret",)

        async def command(self, args: str):  # type: ignore[no-untyped-def]
            commands.append(args)
            if args == "":
                controller_status["running"] = True
            elif args == "stop":
                controller_status["running"] = False
            return Action(message="ok")

        async def deliver_for_account_outcome(
            self, *args: object, **kwargs: object
        ) -> object:
            raise AssertionError("not used")

    manager.attach_legacy(
        wechat_legacy_adapter(
            Controller(),  # type: ignore[arg-type]
            is_enabled=lambda: enabled["value"],
            set_enabled=lambda value: enabled.__setitem__("value", value),
        )
    )
    with TestClient(web, base_url=ORIGIN) as client:
        _login(client)
        payload = client.get("/api/channels").json()
        assert payload["connections"][0]["connection"]["connection_id"] == (
            LEGACY_WECHAT_ID
        )
        assert payload["connections"][0]["connection"]["legacy"] is True

        create = client.post(
            "/api/channels/wechat/connections",
            json={"request_id": "legacy-create", "display_name": "New WeChat"},
            headers={"Origin": ORIGIN},
        )
        assert create.status_code == 409
        assert create.json()["error"] == "legacy_channel_managed"

        patch = client.patch(
            f"/api/channels/connections/{LEGACY_WECHAT_ID}",
            json={
                "expected_revision": 1,
                "request_id": "legacy-update",
                "display_name": "Renamed",
            },
            headers={"Origin": ORIGIN},
        )
        assert patch.status_code == 409
        assert patch.json()["error"] == "legacy_channel_managed"

        delete = client.request(
            "DELETE",
            f"/api/channels/connections/{LEGACY_WECHAT_ID}",
            json={
                "expected_revision": 1,
                "request_id": "legacy-delete",
                "confirm": True,
            },
            headers={"Origin": ORIGIN},
        )
        assert delete.status_code == 409
        assert delete.json()["error"] == "legacy_channel_managed"

        started = client.post(
            f"/api/channels/connections/{LEGACY_WECHAT_ID}/start",
            json={"request_id": "legacy-start"},
            headers={"Origin": ORIGIN},
        )
        assert started.status_code == 200
        assert started.json()["runtime"]["actual_state"] == "running"
        assert commands == [""]
        assert enabled["value"] is True


def test_disabled_connection_refuses_start_with_a_client_error(
    tmp_path: Path,
) -> None:
    web, _ = _app(tmp_path)
    with TestClient(web, base_url=ORIGIN) as client:
        _login(client)
        created = _create(client, request_id="disabled-create")
        connection_id = created["connection"]["connection_id"]
        disabled = client.patch(
            f"/api/channels/connections/{connection_id}",
            json={
                "expected_revision": 1,
                "request_id": "disabled-update",
                "enabled": False,
            },
            headers={"Origin": ORIGIN},
        )
        assert disabled.status_code == 200
        started = client.post(
            f"/api/channels/connections/{connection_id}/start",
            json={"request_id": "disabled-start"},
            headers={"Origin": ORIGIN},
        )
        assert started.status_code == 422
        assert started.json()["error"] == "channel_disabled"


def test_url_credentials_and_secret_metadata_are_rejected(tmp_path: Path) -> None:
    web, _ = _app(tmp_path)
    with TestClient(web, base_url=ORIGIN) as client:
        _login(client)
        url_secret = client.post(
            "/api/channels/discord/connections",
            json={
                "request_id": "ssrf",
                "display_name": "Webhook",
                "credentials": {"webhook_url": "https://example.com/hook"},
            },
            headers={"Origin": ORIGIN},
        )
        assert url_secret.status_code == 422
        assert url_secret.json()["error"] == "invalid_channel_request"

        secret_metadata = client.post(
            "/api/channels/qq/connections",
            json={
                "request_id": "metadata",
                "display_name": "Metadata",
                "metadata": {"token": TEST_SECRET},
            },
            headers={"Origin": ORIGIN},
        )
        assert secret_metadata.status_code == 422
        assert TEST_SECRET not in secret_metadata.text

        assert client.get("/api/channels").json()["connections"] == []


def test_unknown_platform_and_cross_platform_filters(tmp_path: Path) -> None:
    web, _ = _app(tmp_path)
    with TestClient(web, base_url=ORIGIN) as client:
        _login(client)
        assert client.get("/api/channels/unknown/connections").status_code == 422
        assert client.get("/api/channels?platform=unknown").status_code == 422
        _create(client, platform="telegram", request_id="one")
        _create(client, platform="telegram", request_id="two", display_name="Second")
        _create(client, platform="discord", request_id="three")
        telegram = client.get("/api/channels/telegram/connections").json()
        assert len(telegram["connections"]) == 2
        assert all(
            row["connection"]["platform"] == "telegram"
            for row in telegram["connections"]
        )
        discord = client.get("/api/channels?platform=discord").json()
        assert len(discord["connections"]) == 1
        assert [row["platform"] for row in discord["platforms"]] == ["discord"]
        assert discord["platforms"][0]["display_state"] == "silent"


def test_missing_production_adapter_is_explicit_and_config_stays_editable(
    tmp_path: Path,
) -> None:
    manager = ChannelManager(
        store=ChannelStore(tmp_path / "channels"),
        conversations=FakeIntake(),
        adapters=ChannelAdapterRegistry(),
    )
    web = create_app(
        _config(tmp_path),
        runtime_token=TOKEN,
        channels=manager,
    )
    with TestClient(web, base_url=ORIGIN) as client:
        _login(client)
        created = _create(client, request_id="no-adapter-create")
        connection_id = created["connection"]["connection_id"]
        started = client.post(
            f"/api/channels/connections/{connection_id}/start",
            json={"request_id": "no-adapter-start"},
            headers={"Origin": ORIGIN},
        )
        assert started.status_code == 501
        assert started.json() == {"error": "adapter_not_implemented"}

        overview = client.get("/api/channels").json()
        telegram = next(
            row for row in overview["platforms"] if row["platform"] == "telegram"
        )
        assert telegram["display_state"] == "silent"
        assert overview["connections"][0]["runtime"]["actual_state"] != "running"

        renamed = client.patch(
            f"/api/channels/connections/{connection_id}",
            json={
                "expected_revision": 1,
                "request_id": "no-adapter-rename",
                "display_name": "Renamed",
            },
            headers={"Origin": ORIGIN},
        )
        assert renamed.status_code == 200
        deleted = client.request(
            "DELETE",
            f"/api/channels/connections/{connection_id}",
            json={
                "expected_revision": 2,
                "request_id": "no-adapter-delete",
                "confirm": True,
            },
            headers={"Origin": ORIGIN},
        )
        assert deleted.status_code == 200
