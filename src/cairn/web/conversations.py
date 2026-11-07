"""Conversation/Run HTTP and WebSocket routes.

These rewrite ordinary chat identities away from the legacy task_id surface.
The existing task routes remain available for development compatibility.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from cairn.core.domain import ConversationSource
from cairn.runtime.conversation_service import (
    ConversationBusy,
    ConversationRecoveryRequired,
    ConversationService,
)
from cairn.runtime.lifecycle import settle_owned
from cairn.web.runtime import request_body


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=32000)
    source: ConversationSource = ConversationSource.CLI
    request_id: str | None = Field(default=None, max_length=200)


class RecoveryReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: dict[str, Any]
    action: Literal["abandon_without_replay", "acknowledge_checkpoint"]
    reviewed_effects: Literal[True]
    note: str = Field(min_length=1, max_length=1024)


class ConversationMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=32000)
    source: ConversationSource = ConversationSource.CLI
    request_id: str = Field(min_length=1, max_length=200)


def conversation_routes(
    service: ConversationService, workspace: Path
) -> list[BaseRoute]:
    def workspace_ok(request: Any) -> bool:
        requested = request.headers.get("X-Cairn-Workspace")
        return requested is None or requested == workspace.resolve().as_uri()

    def run_payload(run_id: str) -> dict[str, Any]:
        run = service.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        events = service.events(run_id, 0)
        return {
            "conversation_id": run.conversation_id,
            "run_id": run.run_id,
            "task_id": run.task_id,
            "status": run.status.value,
            "cursor": events[-1].sequence if events else 0,
            "trace_id": run.trace_ref,
            "budget": run.budget,
            "checkpoint_receipt": run.checkpoint_receipt,
            "result": run.result,
            "error": run.error,
            "session_id": run.session_id,
            "next_action": (
                "development_handoff_pending"
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
            action = request.path_params.get("action")
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
                for event in service.events(run_id, cursor):
                    await websocket.send_json(
                        {"type": "event", "event": event.model_dump(mode="json")}
                    )
                    cursor = event.sequence
                await websocket.send_json({"type": "ready", "cursor": cursor})
                while True:
                    pending = asyncio.create_task(queue.get())
                    done, _ = await asyncio.wait(
                        (pending, receiving), return_when=asyncio.FIRST_COMPLETED
                    )
                    if receiving in done:
                        receiving.result()
                        return
                    message = pending.result()
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
        Route("/api/runtime/conversations", handle, methods=["GET", "POST"]),
        Route(
            "/api/runtime/conversations/{conversation_id}",
            handle,
            methods=["GET"],
        ),
        Route(
            "/api/runtime/conversations/{conversation_id}/{action}",
            handle,
            methods=["GET", "POST"],
        ),
        Route("/api/runtime/runs/{run_id}", handle, methods=["GET"]),
        Route("/api/runtime/runs/{run_id}/{action}", handle, methods=["GET", "POST"]),
        WebSocketRoute("/api/runtime/runs/{run_id}/stream", stream),
    ]


__all__ = ["conversation_routes"]
