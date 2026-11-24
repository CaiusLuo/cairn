"""Authenticated Web API for host-managed model configuration.

Reads return the current files and the running provider without credential
values. Writes require a same-origin operator request, validate the whole
prospective configuration, and return a request receipt-shaped snapshot. The
credential is write-only: it is accepted in the request, stored in the
workspace ``.env``, and never included in any response.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from cairn.agent.models.selection import ModelRef
from cairn.interfaces.web.auth import operator_request
from cairn.server.model_settings import (
    ModelSettingsError,
    ProbeModels,
    SaveModelSettings,
)

MAX_BODY_BYTES = 64 * 1024


class _BodyTooLarge(ValueError):
    pass


_FAILURE_STATUS = {
    "invalid_model_settings": 422,
    "invalid_model_selection": 409,
    "model_selection_unavailable": 409,
    "model_config_invalid": 409,
    "model_settings_unavailable": 503,
    "model_config_conflict": 409,
    "host_env_credential": 409,
    "credential_missing": 409,
    "model_settings_rollback_failed": 500,
    "probe_credential_rejected": 409,
    "probe_failed": 502,
    "probe_timeout": 504,
}


class ModelSettingsService(Protocol):
    def read(self) -> dict[str, Any]: ...

    def save(self, request: SaveModelSettings) -> dict[str, Any]: ...

    def select(self, ref: ModelRef) -> dict[str, Any]: ...

    def probe(self, request: ProbeModels) -> dict[str, Any]: ...


class SaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    mode: Literal["create", "update"] = "update"
    active_provider: str = Field(default="", max_length=120)
    provider_name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=1, max_length=2048)
    api_key_env: str | None = Field(default=None, max_length=120)
    api_key: str | None = Field(default=None, max_length=4096)
    active_group: str = Field(default="", max_length=120)
    group_name: str = Field(min_length=1, max_length=120)
    model_ids: list[str] = Field(min_length=1, max_length=100)

    def service_request(self) -> SaveModelSettings:
        return SaveModelSettings(
            mode=self.mode,
            active_provider=self.active_provider,
            provider_name=self.provider_name,
            base_url=self.base_url,
            api_key_env=self.api_key_env,
            api_key=self.api_key,
            active_group=self.active_group,
            group_name=self.group_name,
            model_ids=tuple(self.model_ids),
        )


class ProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    base_url: str = Field(min_length=1, max_length=2048)
    api_key_env: str | None = Field(default=None, max_length=120)
    api_key: str | None = Field(default=None, max_length=4096)

    def service_request(self) -> ProbeModels:
        return ProbeModels(
            base_url=self.base_url,
            api_key_env=self.api_key_env,
            api_key=self.api_key,
        )


class SelectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    provider: str = Field(min_length=1, max_length=120)
    group: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=500)

    def service_ref(self) -> ModelRef:
        return ModelRef(self.provider, self.group, self.model)


def _headers() -> dict[str, str]:
    return {
        "content-type": "application/json; charset=utf-8",
        "cache-control": "no-store",
    }


def _error(status: int, code: str, message: str | None = None) -> JSONResponse:
    payload: dict[str, object] = {"error": code}
    if message:
        payload["message"] = message
    return JSONResponse(payload, status_code=status, headers=_headers())


async def _read_limited(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise _BodyTooLarge("model settings request is too large")
        body.extend(chunk)
    return bytes(body)


async def _body(request: Request) -> object:
    return json.loads(await _read_limited(request))


def model_routes(
    service: ModelSettingsService, runtime_token: str | None
) -> list[BaseRoute]:
    async def handle(request: Request) -> JSONResponse:
        if request.method == "GET":
            try:
                settings = await asyncio.to_thread(service.read)
            except ModelSettingsError as error:
                return _settings_error(error)
            except OSError:
                return _error(503, "model_settings_unavailable")
            return JSONResponse({"settings": settings}, headers=_headers())

        if not operator_request(request, runtime_token):
            return _error(403, "invalid_origin")
        content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type != "application/json":
            return _error(415, "unsupported_media_type")
        try:
            payload = SaveRequest.model_validate(await _body(request))
        except _BodyTooLarge:
            return _error(413, "request_too_large")
        except ValidationError:
            return _error(422, "invalid_model_settings")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _error(400, "invalid_json")
        try:
            result = await asyncio.to_thread(service.save, payload.service_request())
        except ModelSettingsError as error:
            return _settings_error(error)
        except OSError:
            return _error(503, "model_settings_unavailable")
        return JSONResponse(result, headers=_headers())

    async def select(request: Request) -> JSONResponse:
        if not operator_request(request, runtime_token):
            return _error(403, "invalid_origin")
        content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type != "application/json":
            return _error(415, "unsupported_media_type")
        try:
            payload = SelectRequest.model_validate(await _body(request))
        except _BodyTooLarge:
            return _error(413, "request_too_large")
        except ValidationError:
            return _error(422, "invalid_model_selection")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _error(400, "invalid_json")
        try:
            result = await asyncio.to_thread(service.select, payload.service_ref())
        except ModelSettingsError as error:
            return _settings_error(error)
        except OSError:
            return _error(503, "model_settings_unavailable")
        return JSONResponse(result, headers=_headers())

    async def probe(request: Request) -> JSONResponse:
        if not operator_request(request, runtime_token):
            return _error(403, "invalid_origin")
        content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type != "application/json":
            return _error(415, "unsupported_media_type")
        try:
            payload = ProbeRequest.model_validate(await _body(request))
        except _BodyTooLarge:
            return _error(413, "request_too_large")
        except ValidationError:
            return _error(422, "invalid_model_settings")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _error(400, "invalid_json")
        try:
            result = await asyncio.to_thread(service.probe, payload.service_request())
        except ModelSettingsError as error:
            return _settings_error(error)
        except OSError:
            return _error(503, "model_settings_unavailable")
        return JSONResponse(result, headers=_headers())

    return [
        Route("/api/model", handle, methods=["GET", "PUT"]),
        Route("/api/model/select", select, methods=["POST"]),
        Route("/api/model/probe", probe, methods=["POST"]),
    ]


def _settings_error(error: ModelSettingsError) -> JSONResponse:
    return _error(
        _FAILURE_STATUS.get(error.code, 422),
        error.code,
        str(error),
    )
