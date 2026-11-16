"""Authenticated channel connection API.

Reads expose configuration plus the adapter's own runtime state. Writes are
host-authorized, use request receipts and never accept or return secret values
in cleartext beyond the write-only credential field.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from cairn.channels.manager import ChannelConnectionView, ChannelManager
from cairn.channels.models import (
    ALL_PLATFORMS,
    ChannelAdapterError,
    ChannelAdapterNotImplemented,
    ChannelConfirmationRequired,
    ChannelCredentialError,
    ChannelDeliveryUncertain,
    ChannelDisabled,
    ChannelInboundConflict,
    ChannelInboundRejected,
    ChannelLegacyManaged,
    ChannelNotFound,
    ChannelOperationUncertain,
    ChannelPlatform,
    ChannelRequestConflict,
    ChannelRevisionConflict,
)
from cairn.interfaces.web.auth import operator_request

MAX_BODY_BYTES = 64 * 1024
REQUEST_ID_MAX_LENGTH = 128


def _json_headers() -> dict[str, str]:
    return {
        "content-type": "application/json; charset=utf-8",
        "cache-control": "no-store",
    }


def _error(status: int, code: str, **extra: object) -> JSONResponse:
    return JSONResponse(
        {"error": code, **extra}, status_code=status, headers=_json_headers()
    )


def _view_payload(view: ChannelConnectionView) -> dict[str, object]:
    return {
        "connection": view.connection.model_dump(mode="json"),
        "runtime": view.runtime.model_dump(mode="json"),
    }


def _platform(value: str) -> ChannelPlatform:
    try:
        return ChannelPlatform(value)
    except ValueError:
        raise ValueError("unknown channel platform") from None


async def _read_limited(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise ValueError("channel request is too large")
        body.extend(chunk)
    return bytes(body)


async def _body(request: Request) -> object:
    return json.loads(await _read_limited(request))


def _request_id(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("request_id must not be blank")
    return stripped


class CreateChannelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    request_id: str = Field(min_length=1, max_length=REQUEST_ID_MAX_LENGTH)
    display_name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    owner_identity: str | None = Field(default=None, max_length=200)
    allowed_senders: list[str] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)
    credentials: dict[str, str] | None = None

    @field_validator("request_id")
    @classmethod
    def _clean_request_id(cls, value: str) -> str:
        return _request_id(value)

    @field_validator("display_name")
    @classmethod
    def _clean_display_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("display_name must not be blank")
        return stripped

    @field_validator("owner_identity")
    @classmethod
    def _clean_owner(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class UpdateChannelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    expected_revision: int = Field(ge=1)
    request_id: str = Field(min_length=1, max_length=REQUEST_ID_MAX_LENGTH)
    display_name: str | None = Field(default=None, max_length=120)
    enabled: bool | None = None
    owner_identity: str | None = Field(default=None, max_length=200)
    allowed_senders: list[str] | None = None
    metadata: dict[str, object] | None = None
    credentials: dict[str, str] | None = None
    replace_credentials: bool = False

    @field_validator("request_id")
    @classmethod
    def _clean_request_id(cls, value: str) -> str:
        return _request_id(value)

    @field_validator("display_name")
    @classmethod
    def _clean_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("display_name must not be blank")
        return stripped


class DeleteChannelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    expected_revision: int = Field(ge=1)
    request_id: str = Field(min_length=1, max_length=REQUEST_ID_MAX_LENGTH)
    confirm: bool = False

    @field_validator("request_id")
    @classmethod
    def _clean_request_id(cls, value: str) -> str:
        return _request_id(value)


class ChannelActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    request_id: str = Field(min_length=1, max_length=REQUEST_ID_MAX_LENGTH)

    @field_validator("request_id")
    @classmethod
    def _clean_request_id(cls, value: str) -> str:
        return _request_id(value)


def _invalid(exc: ValidationError) -> JSONResponse:
    return _error(422, "invalid_channel_request")


def _write_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, ChannelNotFound):
        return _error(404, "channel_not_found")
    if isinstance(exc, ChannelLegacyManaged):
        return _error(409, "legacy_channel_managed")
    if isinstance(exc, ChannelRevisionConflict):
        return _error(
            409,
            "revision_conflict",
            current_revision=exc.current_revision,
        )
    if isinstance(exc, ChannelRequestConflict):
        return _error(409, "request_id_conflict")
    if isinstance(exc, ChannelOperationUncertain):
        return _error(409, "channel_operation_uncertain")
    if isinstance(exc, ChannelDeliveryUncertain):
        return _error(409, "channel_delivery_uncertain")
    if isinstance(exc, ChannelDisabled):
        return _error(422, "channel_disabled")
    if isinstance(exc, ChannelInboundConflict):
        return _error(409, "channel_inbound_conflict")
    if isinstance(exc, ChannelInboundRejected):
        return _error(422, "channel_inbound_rejected")
    if isinstance(exc, ChannelConfirmationRequired):
        return _error(428, "confirmation_required")
    if isinstance(exc, ChannelCredentialError):
        return _error(409, "channel_credentials_invalid")
    if isinstance(exc, ChannelAdapterNotImplemented):
        return _error(501, "adapter_not_implemented")
    if isinstance(exc, ChannelAdapterError):
        return _error(502, "channel_adapter_error")
    if isinstance(exc, ValueError):
        return _error(422, "invalid_channel_request")
    if isinstance(exc, (OSError, sqlite3.Error)):
        return _error(503, "channel_store_unavailable")
    return _error(500, "channel_internal_error")


async def _validated(
    request: Request, model: type[BaseModel]
) -> tuple[BaseModel | None, JSONResponse | None]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type != "application/json":
        return None, _error(415, "unsupported_media_type")
    try:
        raw = await _read_limited(request)
    except ValueError:
        return None, _error(413, "channel_request_too_large")
    try:
        payload = model.model_validate(json.loads(raw))
    except json.JSONDecodeError:
        return None, _error(400, "invalid_json")
    except ValidationError as exc:
        return None, _invalid(exc)
    return payload, None


def channel_routes(
    manager: ChannelManager, runtime_token: str | None
) -> list[BaseRoute]:
    def allowed(request: Request) -> bool:
        return operator_request(request, runtime_token)

    async def overview(request: Request) -> JSONResponse:
        raw_platform = request.query_params.get("platform")
        try:
            platform = None if not raw_platform else _platform(raw_platform)
        except ValueError:
            return _error(422, "invalid_channel_request")
        try:
            views = manager.list_connection_views(platform)
            summaries = manager.platform_summaries(platform)
        except (OSError, sqlite3.Error):
            return _error(503, "channel_store_unavailable")
        return JSONResponse(
            {
                "platforms": [summary.model_dump(mode="json") for summary in summaries],
                "connections": [_view_payload(view) for view in views],
            },
            headers=_json_headers(),
        )

    async def capabilities(_request: Request) -> JSONResponse:
        try:
            rows = [
                manager.platform_capabilities(platform) for platform in ALL_PLATFORMS
            ]
        except (OSError, sqlite3.Error):
            return _error(503, "channel_store_unavailable")
        return JSONResponse(
            {"platforms": [row.model_dump(mode="json") for row in rows]},
            headers=_json_headers(),
        )

    async def request_receipt(request: Request) -> JSONResponse:
        request_id = request.path_params.get("request_id", "")
        try:
            receipt = manager.request_receipt(request_id)
        except (OSError, sqlite3.Error):
            return _error(503, "channel_store_unavailable")
        except RuntimeError:
            return _error(500, "channel_internal_error")
        if receipt is None:
            return _error(404, "channel_request_not_found")
        return JSONResponse(
            {
                "request_id": receipt.request_id,
                "state": receipt.state,
                "result": receipt.result,
            },
            headers=_json_headers(),
        )

    async def platform_connections(request: Request) -> JSONResponse:
        try:
            platform = _platform(request.path_params.get("platform", ""))
        except ValueError:
            return _error(422, "invalid_channel_request")
        try:
            views = manager.list_connection_views(platform)
        except (OSError, sqlite3.Error):
            return _error(503, "channel_store_unavailable")
        return JSONResponse(
            {
                "platform": platform.value,
                "connections": [_view_payload(view) for view in views],
            },
            headers=_json_headers(),
        )

    async def create_connection(request: Request) -> JSONResponse:
        if not allowed(request):
            return _error(403, "invalid_origin")
        try:
            platform = _platform(request.path_params.get("platform", ""))
        except ValueError:
            return _error(422, "invalid_channel_request")
        payload, problem = await _validated(request, CreateChannelRequest)
        if problem is not None:
            return problem
        assert isinstance(payload, CreateChannelRequest)
        try:
            view = await manager.create_connection(
                platform=platform,
                display_name=payload.display_name,
                enabled=payload.enabled,
                owner_identity=payload.owner_identity,
                allowed_senders=payload.allowed_senders,
                metadata=payload.metadata,
                credentials=payload.credentials,
                request_id=payload.request_id,
            )
        except Exception as exc:
            return _write_error(exc)
        return JSONResponse(
            {"request_id": payload.request_id, **_view_payload(view)},
            status_code=201,
            headers=_json_headers(),
        )

    async def update_connection(request: Request) -> JSONResponse:
        if not allowed(request):
            return _error(403, "invalid_origin")
        connection_id = request.path_params.get("connection_id", "")
        payload, problem = await _validated(request, UpdateChannelRequest)
        if problem is not None:
            return problem
        assert isinstance(payload, UpdateChannelRequest)
        if payload.credentials is not None and not payload.replace_credentials:
            return _error(422, "explicit_credential_replacement_required")
        if payload.credentials is None and payload.replace_credentials:
            return _error(422, "credentials_required")
        fields = payload.model_fields_set
        changes: dict[str, object] = {}
        if "display_name" in fields:
            if payload.display_name is None:
                return _error(422, "invalid_channel_request")
            changes["display_name"] = payload.display_name
        if "enabled" in fields:
            if payload.enabled is None:
                return _error(422, "invalid_channel_request")
            changes["enabled"] = payload.enabled
        if "owner_identity" in fields:
            changes["owner_identity"] = payload.owner_identity
        if "allowed_senders" in fields:
            changes["allowed_senders"] = payload.allowed_senders or []
        if "metadata" in fields:
            changes["metadata"] = payload.metadata or {}
        try:
            view = await manager.update_connection(
                connection_id,
                expected_revision=payload.expected_revision,
                request_id=payload.request_id,
                changes=changes,
                credentials=payload.credentials,
                replace_credentials=payload.replace_credentials,
            )
        except Exception as exc:
            return _write_error(exc)
        return JSONResponse(
            {"request_id": payload.request_id, **_view_payload(view)},
            headers=_json_headers(),
        )

    async def delete_connection(request: Request) -> JSONResponse:
        if not allowed(request):
            return _error(403, "invalid_origin")
        connection_id = request.path_params.get("connection_id", "")
        payload, problem = await _validated(request, DeleteChannelRequest)
        if problem is not None:
            return problem
        assert isinstance(payload, DeleteChannelRequest)
        if not payload.confirm:
            return _error(428, "confirmation_required")
        try:
            result = await manager.delete_connection(
                connection_id,
                expected_revision=payload.expected_revision,
                request_id=payload.request_id,
                confirmed=True,
            )
        except Exception as exc:
            return _write_error(exc)
        return JSONResponse(
            {
                "request_id": payload.request_id,
                "deleted": True,
                "connection_id": result.connection_id,
                "audit_retained": True,
                "replayed": result.replayed,
            },
            headers=_json_headers(),
        )

    def action_route(
        action: str,
    ) -> Callable[[Request], Awaitable[JSONResponse]]:
        async def perform(request: Request) -> JSONResponse:
            if not allowed(request):
                return _error(403, "invalid_origin")
            connection_id = request.path_params.get("connection_id", "")
            payload, problem = await _validated(request, ChannelActionRequest)
            if problem is not None:
                return problem
            assert isinstance(payload, ChannelActionRequest)
            try:
                if action == "start":
                    view = await manager.start_connection(
                        connection_id, payload.request_id
                    )
                elif action == "stop":
                    view = await manager.stop_connection(
                        connection_id, payload.request_id
                    )
                else:
                    view = await manager.reconnect_connection(
                        connection_id, payload.request_id
                    )
            except Exception as exc:
                return _write_error(exc)
            return JSONResponse(
                {"request_id": payload.request_id, **_view_payload(view)},
                headers=_json_headers(),
            )

        return perform

    async def status(request: Request) -> JSONResponse:
        connection_id = request.path_params.get("connection_id", "")
        try:
            runtime = manager.get_runtime_status(connection_id)
        except ChannelNotFound:
            return _error(404, "channel_not_found")
        except (OSError, sqlite3.Error):
            return _error(503, "channel_store_unavailable")
        return JSONResponse(runtime.model_dump(mode="json"), headers=_json_headers())

    routes: list[BaseRoute] = [
        Route("/api/channels", overview, methods=["GET"]),
        Route("/api/channels/capabilities", capabilities, methods=["GET"]),
        Route(
            "/api/channels/requests/{request_id}",
            request_receipt,
            methods=["GET"],
        ),
        Route(
            "/api/channels/connections/{connection_id}",
            update_connection,
            methods=["PATCH"],
        ),
        Route(
            "/api/channels/connections/{connection_id}",
            delete_connection,
            methods=["DELETE"],
        ),
        Route(
            "/api/channels/connections/{connection_id}/status",
            status,
            methods=["GET"],
        ),
        Route(
            "/api/channels/connections/{connection_id}/start",
            action_route("start"),
            methods=["POST"],
        ),
        Route(
            "/api/channels/connections/{connection_id}/stop",
            action_route("stop"),
            methods=["POST"],
        ),
        Route(
            "/api/channels/connections/{connection_id}/reconnect",
            action_route("reconnect"),
            methods=["POST"],
        ),
        Route(
            "/api/channels/{platform}/connections",
            platform_connections,
            methods=["GET"],
        ),
        Route(
            "/api/channels/{platform}/connections",
            create_connection,
            methods=["POST"],
        ),
    ]
    return routes


__all__ = [
    "ChannelActionRequest",
    "CreateChannelRequest",
    "DeleteChannelRequest",
    "UpdateChannelRequest",
    "channel_routes",
]
