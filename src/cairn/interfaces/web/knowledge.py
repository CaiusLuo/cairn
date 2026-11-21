"""Authenticated HTTP surface for private knowledge clips and documents."""

from __future__ import annotations

import json
from urllib.parse import unquote

from psycopg import Error as PostgresError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from cairn.interfaces.web.auth import operator_request
from cairn.knowledge.document_store import DocumentStore
from cairn.knowledge.documents import MAX_DOCUMENT_TEXT_CHARS, Document
from cairn.knowledge.models import (
    CLIP_CONTENT_MAX_LENGTH,
    CLIP_REQUEST_ID_MAX_LENGTH,
    CLIP_SOURCE_MAX_LENGTH,
    CLIP_TAGS_MAX_COUNT,
    Clip,
    normalize_source,
    normalize_tags,
)
from cairn.knowledge.store import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    KnowledgeRequestConflict,
    KnowledgeStore,
)
from cairn.shared.text_extract import is_supported

# The generic runtime reader caps bodies at 256 KiB. A 100k-character clip can
# exceed that in UTF-8, so this surface has its own bounded reader.
MAX_BODY_BYTES = 1024 * 1024


def _json_headers() -> dict[str, str]:
    return {
        "content-type": "application/json; charset=utf-8",
        "cache-control": "no-store",
    }


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status, headers=_json_headers())


async def _body(request: Request) -> object:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise ValueError("knowledge request is too large")
        body.extend(chunk)
    return json.loads(body)


class ClipCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=CLIP_CONTENT_MAX_LENGTH)
    source: str = Field(default="web", max_length=CLIP_SOURCE_MAX_LENGTH)
    tags: tuple[str, ...] = Field(default=(), max_length=CLIP_TAGS_MAX_COUNT)
    request_id: str | None = Field(
        default=None, min_length=1, max_length=CLIP_REQUEST_ID_MAX_LENGTH
    )

    @field_validator("content")
    @classmethod
    def _content_has_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        return value

    @field_validator("source")
    @classmethod
    def _source_has_text(cls, value: str) -> str:
        return normalize_source(value)

    @field_validator("tags")
    @classmethod
    def _tags_are_bounded(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return normalize_tags(value)

    @field_validator("request_id")
    @classmethod
    def _request_id_has_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("request_id must not be blank")
        return stripped


class ClipUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str | None = Field(
        default=None, min_length=1, max_length=CLIP_CONTENT_MAX_LENGTH
    )
    source: str | None = Field(default=None, max_length=CLIP_SOURCE_MAX_LENGTH)
    tags: tuple[str, ...] | None = Field(default=None, max_length=CLIP_TAGS_MAX_COUNT)

    @field_validator("content")
    @classmethod
    def _content_has_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("content must not be blank")
        return value

    @field_validator("source")
    @classmethod
    def _source_has_text(cls, value: str | None) -> str | None:
        return None if value is None else normalize_source(value)

    @field_validator("tags")
    @classmethod
    def _tags_are_bounded(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        return None if value is None else normalize_tags(value)

    @model_validator(mode="after")
    def _has_a_change(self) -> ClipUpdate:
        if self.content is None and self.source is None and self.tags is None:
            raise ValueError("an update needs content, source or tags")
        return self


def _flag(request: Request, name: str) -> bool:
    return request.query_params.get(name, "").lower() in {"1", "true", "yes"}


def _integer(request: Request, name: str, default: int, low: int, high: int) -> int:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def knowledge_routes(
    store: KnowledgeStore, runtime_token: str | None = None
) -> list[BaseRoute]:
    def clip_payload(clip: Clip) -> dict[str, object]:
        return clip.model_dump(mode="json")

    def listing(request: Request) -> JSONResponse:
        query = request.query_params.get("query")
        if query is None:
            query = request.query_params.get("q")
        clips = store.list_clips(
            query=query,
            include_archived=_flag(request, "include_archived"),
            limit=_integer(request, "limit", DEFAULT_LIST_LIMIT, 1, MAX_LIST_LIMIT),
            offset=_integer(request, "offset", 0, 0, 1_000_000),
        )
        return JSONResponse(
            {"clips": [clip_payload(clip) for clip in clips]}, headers=_json_headers()
        )

    async def handle(request: Request) -> JSONResponse:
        try:
            path = request.url.path
            if path == "/api/knowledge/clips":
                if request.method == "GET":
                    return listing(request)
                if not operator_request(request, runtime_token):
                    return _error(403, "invalid_origin")
                content_type = (
                    request.headers.get("content-type", "").split(";", 1)[0].lower()
                )
                if content_type != "application/json":
                    return _error(415, "unsupported_media_type")
                create = ClipCreate.model_validate(await _body(request))
                clip, _created = store.create(
                    create.content,
                    source=create.source,
                    tags=create.tags,
                    request_id=create.request_id,
                )
                return JSONResponse(clip_payload(clip), headers=_json_headers())
            if path == "/api/knowledge/search":
                if request.method != "GET":
                    return _error(405, "method_not_allowed")
                return listing(request)
            clip_id = request.path_params.get("clip_id", "")
            action = request.path_params.get("action")
            if request.method == "GET":
                if action is not None:
                    return _error(405, "method_not_allowed")
                found = store.get(clip_id)
                if found is None:
                    return _error(404, "clip_not_found")
                return JSONResponse(clip_payload(found), headers=_json_headers())
            if request.method != "POST":
                return _error(405, "method_not_allowed")
            if not operator_request(request, runtime_token):
                return _error(403, "invalid_origin")
            if action in {None, "update"}:
                content_type = (
                    request.headers.get("content-type", "").split(";", 1)[0].lower()
                )
                if content_type != "application/json":
                    return _error(415, "unsupported_media_type")
                update = ClipUpdate.model_validate(await _body(request))
                clip = store.update(
                    clip_id,
                    content=update.content,
                    source=update.source,
                    tags=update.tags,
                )
                return JSONResponse(clip_payload(clip), headers=_json_headers())
            if action == "archive":
                return JSONResponse(
                    clip_payload(store.set_archived(clip_id, archived=True)),
                    headers=_json_headers(),
                )
            if action == "unarchive":
                return JSONResponse(
                    clip_payload(store.set_archived(clip_id, archived=False)),
                    headers=_json_headers(),
                )
            return _error(404, "not_found")
        except json.JSONDecodeError:
            return _error(400, "invalid_json")
        except KnowledgeRequestConflict:
            return _error(409, "request_id_conflict")
        except KeyError:
            return _error(404, "clip_not_found")
        except ValidationError:
            return _error(422, "invalid_knowledge_request")
        except ValueError:
            return _error(422, "invalid_knowledge_request")
        except (OSError, PostgresError):
            return _error(503, "knowledge_store_unavailable")

    return [
        Route("/api/knowledge/clips", handle, methods=["GET", "POST"]),
        Route("/api/knowledge/search", handle, methods=["GET"]),
        Route("/api/knowledge/clips/{clip_id}", handle, methods=["GET", "POST"]),
        Route(
            "/api/knowledge/clips/{clip_id}/{action}",
            handle,
            methods=["GET", "POST"],
        ),
    ]


MAX_DOCUMENT_NAME_CHARS = 255
DEFAULT_DOCUMENT_READ_CHARS = 20_000
MAX_DOCUMENT_READ_CHARS = 100_000


def document_routes(
    store: DocumentStore, runtime_token: str | None = None
) -> list[BaseRoute]:
    def document_payload(document: Document) -> dict[str, object]:
        return document.model_dump(mode="json")

    async def handle(request: Request) -> JSONResponse:
        try:
            path = request.url.path
            if path == "/api/knowledge/documents":
                if request.method == "GET":
                    documents = await run_in_threadpool(
                        store.list_documents,
                        limit=_integer(
                            request, "limit", DEFAULT_LIST_LIMIT, 1, MAX_LIST_LIMIT
                        ),
                        offset=_integer(request, "offset", 0, 0, 1_000_000),
                    )
                    return JSONResponse(
                        {"documents": [document_payload(item) for item in documents]},
                        headers=_json_headers(),
                    )
                if request.method != "POST":
                    return _error(405, "method_not_allowed")
                if not operator_request(request, runtime_token):
                    return _error(403, "invalid_origin")
                # Header values are latin-1; the browser percent-encodes UTF-8.
                name = unquote(request.headers.get("x-cairn-filename", "")).strip()
                if not name or len(name) > MAX_DOCUMENT_NAME_CHARS:
                    return _error(422, "invalid_document_upload")
                media_type = (
                    request.headers.get("content-type", "application/octet-stream")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )
                if not is_supported(name, media_type):
                    return _error(415, "unsupported_document_type")
                body = bytearray()
                async for chunk in request.stream():
                    if len(body) + len(chunk) > store.max_upload_bytes:
                        return _error(413, "document_too_large")
                    body.extend(chunk)
                if not body:
                    return _error(422, "invalid_document_upload")
                document, created = await run_in_threadpool(
                    store.create_upload, name, media_type, bytes(body)
                )
                return JSONResponse(
                    {
                        "document": document_payload(document),
                        "deduplicated": not created,
                    },
                    headers=_json_headers(),
                )
            document_id = request.path_params.get("document_id", "")
            action = request.path_params.get("action")
            if request.method == "GET":
                if action == "text":
                    offset = _integer(request, "offset", 0, 0, MAX_DOCUMENT_TEXT_CHARS)
                    max_chars = _integer(
                        request,
                        "max_chars",
                        DEFAULT_DOCUMENT_READ_CHARS,
                        1,
                        MAX_DOCUMENT_READ_CHARS,
                    )
                    page = await run_in_threadpool(
                        store.document_text,
                        document_id,
                        offset=offset,
                        max_chars=max_chars,
                    )
                    if page is None:
                        return _error(404, "document_not_found")
                    return JSONResponse(
                        {
                            "document": document_payload(page.document),
                            "content": page.content,
                            "offset": page.offset,
                            "total_chars": page.total_chars,
                            "next_offset": page.next_offset,
                        },
                        headers=_json_headers(),
                    )
                if action is not None:
                    return _error(405, "method_not_allowed")
                found = await run_in_threadpool(store.get, document_id)
                if found is None:
                    return _error(404, "document_not_found")
                return JSONResponse(
                    {"document": document_payload(found)},
                    headers=_json_headers(),
                )
            if request.method == "DELETE":
                if action is not None:
                    return _error(405, "method_not_allowed")
                if not operator_request(request, runtime_token):
                    return _error(403, "invalid_origin")
                if not await run_in_threadpool(store.delete, document_id):
                    return _error(404, "document_not_found")
                return JSONResponse({"deleted": True}, headers=_json_headers())
            if request.method != "POST":
                return _error(405, "method_not_allowed")
            if not operator_request(request, runtime_token):
                return _error(403, "invalid_origin")
            if action == "retry":
                retried = await run_in_threadpool(store.retry, document_id)
                if retried is None:
                    # Missing or already processing; retrying cannot race it.
                    return _error(404, "document_not_found")
                return JSONResponse(
                    {"document": document_payload(retried)},
                    headers=_json_headers(),
                )
            return _error(404, "not_found")
        except ValueError:
            return _error(422, "invalid_knowledge_request")
        except (OSError, PostgresError):
            return _error(503, "knowledge_store_unavailable")

    return [
        Route("/api/knowledge/documents", handle, methods=["GET", "POST"]),
        Route(
            "/api/knowledge/documents/{document_id}",
            handle,
            methods=["GET", "DELETE", "POST"],
        ),
        Route(
            "/api/knowledge/documents/{document_id}/{action}",
            handle,
            methods=["GET", "POST"],
        ),
    ]


__all__ = ["document_routes", "knowledge_routes"]
