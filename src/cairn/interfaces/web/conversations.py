"""Conversation/Run HTTP and WebSocket routes.

These rewrite ordinary chat identities away from the legacy task_id surface.
The existing task routes remain available for development compatibility.
"""

from __future__ import annotations

import asyncio
import secrets
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from cairn.conversation.models import ConversationSource
from cairn.conversation.service import (
    ConversationBusy,
    ConversationRecoveryRequired,
    ConversationService,
)
from cairn.development.handoff import HandoffReviewConflict
from cairn.interfaces.web.auth import operator_request
from cairn.interfaces.web.runtime import request_body
from cairn.shared.lifecycle import settle_owned


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=32000)
    source: ConversationSource = ConversationSource.CLI
    request_id: str | None = Field(default=None, max_length=200)


class RecoveryReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # A missing-Session Run has no Session Revision to submit. The service
    # requires an explicit abandon_without_replay review for that case.
    revision: dict[str, Any] | None = None
    action: Literal["abandon_without_replay", "acknowledge_checkpoint"]
    reviewed_effects: Literal[True]
    note: str = Field(min_length=1, max_length=1024)


class HandoffReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=200)
    action: Literal["adopt_existing", "abandon"]
    expected_updated_at: str = Field(min_length=1, max_length=100)
    note: str = Field(min_length=1, max_length=1024)


class ConversationMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=32000)
    source: ConversationSource = ConversationSource.CLI
    request_id: str = Field(min_length=1, max_length=200)
    model_id: str | None = Field(default=None, min_length=1, max_length=200)


class WechatConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: str = Field(min_length=1, max_length=256)
    sender_id: str = Field(min_length=1, max_length=256)
    prompt: str = Field(min_length=1, max_length=32000)


def conversation_routes(
    service: ConversationService, workspace: Path, runtime_token: str | None = None
) -> list[BaseRoute]:
    def workspace_ok(request: Any) -> bool:
        requested = request.headers.get("X-Cairn-Workspace")
        return requested is None or requested == workspace.resolve().as_uri()

    def host_bearer_ok(request: Any) -> bool:
        if runtime_token is None:
            return False
        value = request.headers.get("Authorization", "")
        return secrets.compare_digest(value, "Bearer " + runtime_token)

    def run_payload(run_id: str) -> dict[str, Any]:
        run = service.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        return {
            "conversation_id": run.conversation_id,
            "run_id": run.run_id,
            "task_id": run.task_id,
            "status": run.status.value,
            "model_id": run.metadata.get("model_id"),
            "mode": run.mode,
            "source": run.source,
            "prompt": run.prompt,
            "created_at": run.created_at,
            "ended_at": run.ended_at,
            "cursor": service.cursor(run_id),
            "trace_id": run.trace_ref,
            "budget": run.budget,
            "checkpoint_receipt": run.checkpoint_receipt,
            "result": run.result,
            "error": run.error,
            "session_id": run.session_id,
            "next_action": (
                "handoff_review"
                if run.error
                in {
                    "development_handoff_requires_review",
                    "development_handoff_failed",
                }
                else "development_handoff_pending"
                if run.error == "development_handoff_pending"
                else None
            ),
        }

    async def handle(request: Request) -> JSONResponse:
        try:
            if not workspace_ok(request):
                return JSONResponse({"error": "workspace_mismatch"}, 409)
            conversation_id = request.path_params.get("conversation_id")
            run_id = request.path_params.get("run_id")
            request_id_value = request.path_params.get("request_id")
            action = request.path_params.get("action")
            if request.url.path.startswith("/api/runtime/handoffs/"):
                assert isinstance(run_id, str)
                if action == "review":
                    if request.method != "POST":
                        return JSONResponse(
                            {"error": "method_not_allowed"}, status_code=405
                        )
                    if not operator_request(request, runtime_token):
                        return JSONResponse({"error": "invalid_origin"}, 403)
                    review_body = HandoffReview.model_validate(
                        await request_body(request)
                    )
                    return JSONResponse(
                        await service.review_handoff(
                            run_id,
                            request_id=review_body.request_id,
                            action=review_body.action,
                            expected_updated_at=review_body.expected_updated_at,
                            note=review_body.note,
                        ),
                        headers={"Cache-Control": "no-store"},
                    )
                if request.method != "GET":
                    return JSONResponse(
                        {"error": "method_not_allowed"}, status_code=405
                    )
                return JSONResponse(
                    service.handoff_report(run_id),
                    headers={"Cache-Control": "no-store"},
                )
            if request.url.path == "/api/runtime/runs":
                return JSONResponse(
                    [
                        run.model_dump(
                            mode="json", exclude={"metadata", "checkpoint_receipt"}
                        )
                        for run in service.recent_runs()
                    ],
                    headers={"Cache-Control": "no-store"},
                )
            if (
                request.method == "GET"
                and request.url.path == "/api/runtime/wechat/conversations"
            ):
                if not host_bearer_ok(request):
                    return JSONResponse(
                        {"error": "host_auth_required"}, status_code=403
                    )
                account_id = request.query_params.get("account_id", "")
                sender_id = request.query_params.get("sender_id", "")
                if not account_id or not sender_id:
                    return JSONResponse({"error": "invalid_request"}, 400)
                conversation = service.get_wechat_conversation(account_id, sender_id)
                if conversation is None:
                    return JSONResponse({"error": "conversation_not_found"}, 404)
                return JSONResponse(
                    {"conversation_id": conversation.conversation_id},
                    headers={"Cache-Control": "no-store"},
                )
            if (
                request.method == "POST"
                and request.url.path == "/api/runtime/wechat/conversations"
            ):
                if not host_bearer_ok(request):
                    return JSONResponse(
                        {"error": "host_auth_required"}, status_code=403
                    )
                wechat_body = WechatConversationCreate.model_validate(
                    await request_body(request)
                )
                created_wechat = service.ensure_wechat_conversation(
                    wechat_body.account_id,
                    wechat_body.sender_id,
                    wechat_body.prompt,
                )
                return JSONResponse(
                    {"conversation_id": created_wechat.conversation_id},
                    headers={"Cache-Control": "no-store"},
                )
            if request_id_value is not None and conversation_id is not None:
                run = service.run_by_request(conversation_id, request_id_value)
                if run is None:
                    return JSONResponse({"error": "run_not_found"}, 404)
                return JSONResponse(
                    run_payload(run.run_id), headers={"Cache-Control": "no-store"}
                )
            if request.method == "POST" and conversation_id is None and run_id is None:
                create_body = ConversationCreate.model_validate(
                    await request_body(request)
                )
                created = service.create_conversation(
                    create_body.prompt,
                    create_body.source,
                    create_body.request_id,
                )
                return JSONResponse(
                    created.model_dump(mode="json"),
                    headers={"Cache-Control": "no-store"},
                )
            if request.method == "POST" and conversation_id is not None:
                assert isinstance(conversation_id, str)
                message_body = ConversationMessage.model_validate(
                    await request_body(request)
                )
                run = service.submit(
                    conversation_id,
                    message_body.prompt,
                    message_body.source,
                    message_body.request_id,
                    model_id=message_body.model_id,
                )
                return JSONResponse(
                    run_payload(run.run_id), headers={"Cache-Control": "no-store"}
                )
            if run_id is not None:
                assert isinstance(run_id, str)
                if action == "cancel":
                    if request.method != "POST":
                        return JSONResponse(
                            {"error": "method_not_allowed"}, status_code=405
                        )
                    cancelled = await service.cancel(run_id)
                    return JSONResponse(
                        run_payload(cancelled.run_id),
                        headers={"Cache-Control": "no-store"},
                    )
                if action == "reconcile":
                    if request.method != "POST":
                        return JSONResponse(
                            {"error": "method_not_allowed"}, status_code=405
                        )
                    if not operator_request(request, runtime_token):
                        return JSONResponse({"error": "invalid_origin"}, 403)
                    review = RecoveryReview.model_validate(await request_body(request))
                    reconciled = service.reconcile(run_id, review.model_dump())
                    return JSONResponse(
                        run_payload(reconciled.run_id),
                        headers={"Cache-Control": "no-store"},
                    )
                if action == "recovery":
                    if request.method != "GET":
                        return JSONResponse(
                            {"error": "method_not_allowed"}, status_code=405
                        )
                    return JSONResponse(
                        service.recovery(run_id),
                        headers={"Cache-Control": "no-store"},
                    )
                if action == "events":
                    if request.method != "GET":
                        return JSONResponse(
                            {"error": "method_not_allowed"}, status_code=405
                        )
                    after = int(request.query_params.get("after", "0"))
                    events = (
                        await service.follow(run_id, after)
                        if request.query_params.get("wait") == "1"
                        else service.events(run_id, after)
                    )
                    return JSONResponse(
                        {
                            "run_id": run_id,
                            "events": [
                                event.model_dump(mode="json") for event in events
                            ],
                            "cursor": events[-1].sequence if events else after,
                        },
                        headers={"Cache-Control": "no-store"},
                    )
                if request.method != "GET":
                    return JSONResponse(
                        {"error": "method_not_allowed"}, status_code=405
                    )
                return JSONResponse(
                    run_payload(run_id), headers={"Cache-Control": "no-store"}
                )
            if conversation_id is not None:
                assert isinstance(conversation_id, str)
                current_conversation = service.get_conversation(conversation_id)
                if current_conversation is None:
                    return JSONResponse({"error": "conversation_not_found"}, 404)
                if action == "history":
                    return JSONResponse(
                        service.history(conversation_id),
                        headers={"Cache-Control": "no-store"},
                    )
                return JSONResponse(
                    {
                        "conversation": current_conversation.model_dump(mode="json"),
                        "runs": [
                            run.model_dump(mode="json")
                            for run in service.runs_for_conversation(conversation_id)
                        ],
                    },
                    headers={"Cache-Control": "no-store"},
                )
            return JSONResponse(
                [
                    {
                        **conversation.model_dump(mode="json"),
                        "latest_run": (
                            service.runs_for_conversation(conversation.conversation_id)[
                                -1
                            ].model_dump(mode="json")
                            if service.runs_for_conversation(
                                conversation.conversation_id
                            )
                            else None
                        ),
                    }
                    for conversation in service.list_conversations()
                ],
                headers={"Cache-Control": "no-store"},
            )
        except KeyError:
            return JSONResponse({"error": "conversation_or_run_not_found"}, 404)
        except ConversationBusy:
            return JSONResponse({"error": "conversation_busy"}, 409)
        except ConversationRecoveryRequired:
            return JSONResponse({"error": "conversation_recovery_required"}, 409)
        except HandoffReviewConflict:
            return JSONResponse({"error": "handoff_version_conflict"}, 409)
        except (ValueError, ValidationError):
            return JSONResponse({"error": "invalid_request"}, 409)
        except asyncio.QueueFull:
            return JSONResponse({"error": "runtime_queue_full"}, 429)
        except RuntimeError:
            return JSONResponse({"error": "runtime_unavailable"}, 503)

    async def stream(websocket: WebSocket) -> None:
        run_id = websocket.path_params["run_id"]
        receiving: asyncio.Task[Any] | None = None
        pending: asyncio.Task[Any] | None = None
        try:
            if not workspace_ok(websocket):
                await websocket.close(code=1008)
                return
            cursor = int(websocket.query_params.get("after", "0"))
            service.events(run_id, cursor)
            async with service.subscribe(run_id) as queue:
                await websocket.accept()

                async def disconnect() -> None:
                    while True:
                        if (await websocket.receive())[
                            "type"
                        ] == "websocket.disconnect":
                            return

                receiving = asyncio.create_task(disconnect())
                await websocket.send_json(
                    {
                        "type": "ready",
                        "cursor": cursor,
                        "last_sequence": service.cursor(run_id),
                    }
                )

                async def send_events(*, replay: bool = False) -> None:
                    nonlocal cursor
                    while batch := service.events(run_id, cursor):
                        for event in batch:
                            # Live model text is already sent through preview.
                            if replay or event.kind != "model_text_delta":
                                await websocket.send_json(
                                    {
                                        "type": "event",
                                        "event": event.model_dump(mode="json"),
                                    }
                                )
                            cursor = event.sequence

                await send_events(replay=True)
                snapshot = service.preview_snapshot(run_id)
                if snapshot:
                    await websocket.send_json(
                        {"type": "preview_snapshot", "text": snapshot}
                    )
                run = service.get_run(run_id)
                if run is not None and run.status.value not in {"queued", "running"}:
                    await websocket.send_json(
                        {"type": "settled", "cursor": service.cursor(run_id)}
                    )
                    return
                while True:
                    pending = asyncio.create_task(queue.get())
                    done, _ = await asyncio.wait(
                        (pending, receiving), return_when=asyncio.FIRST_COMPLETED
                    )
                    if receiving in done:
                        receiving.result()
                        return
                    message = pending.result()
                    if message["type"] == "resync":
                        # The consumer fell behind the bounded in-memory queue.
                        # Reconnect from the durable cursor instead of silently
                        # dropping events.
                        await websocket.close(code=1013)
                        return
                    if message["type"] in {"event", "settled"}:
                        await send_events()
                    if message["type"] == "event":
                        continue
                    if message["type"] == "settled":
                        run = service.get_run(run_id)
                        if run is not None and run.status.value not in {
                            "queued",
                            "running",
                        }:
                            await websocket.send_json(message)
                            return
                    await websocket.send_json(message)
        except (ValueError, KeyError, RuntimeError, WebSocketDisconnect, TimeoutError):
            return
        finally:
            await settle_owned(
                *(task for task in (receiving, pending) if task is not None)
            )
            with suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close()

    return [
        Route(
            "/api/runtime/wechat/conversations",
            handle,
            methods=["GET", "POST"],
        ),
        Route("/api/runtime/conversations", handle, methods=["GET", "POST"]),
        Route(
            "/api/runtime/conversations/{conversation_id}",
            handle,
            methods=["GET"],
        ),
        Route(
            "/api/runtime/conversations/{conversation_id}/runs/by-request/{request_id}",
            handle,
            methods=["GET"],
        ),
        Route(
            "/api/runtime/conversations/{conversation_id}/{action}",
            handle,
            methods=["GET", "POST"],
        ),
        Route("/api/runtime/handoffs/{run_id}", handle, methods=["GET"]),
        Route(
            "/api/runtime/handoffs/{run_id}/{action}",
            handle,
            methods=["GET", "POST"],
        ),
        Route("/api/runtime/runs", handle, methods=["GET"]),
        Route("/api/runtime/runs/{run_id}", handle, methods=["GET"]),
        Route("/api/runtime/runs/{run_id}/{action}", handle, methods=["GET", "POST"]),
        WebSocketRoute("/api/runtime/runs/{run_id}/stream", stream),
    ]


__all__ = ["conversation_routes"]
