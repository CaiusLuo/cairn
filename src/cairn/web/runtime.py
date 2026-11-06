"""HTTP task routes; these never construct an Agent or open a writer Store."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from cairn.infra.runtime_catalog import RuntimeSource
from cairn.infra.sessions.models import Revision
from cairn.runtime.lifecycle import settle_owned
from cairn.runtime.task_service import TaskService


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=32000)
    source: RuntimeSource
    request_id: str | None = Field(default=None, max_length=200)


class MessageSubmission(Submission):
    request_id: str = Field(min_length=1, max_length=200)
    expected_sequence: int = Field(ge=0)


class RecoveryReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_sequence: int = Field(ge=0)
    revision: Revision
    action: Literal["abandon_without_replay", "acknowledge_checkpoint"]
    reviewed_effects: Literal[True]
    note: str = Field(min_length=1, max_length=1024)


async def request_body(request: Request) -> Any:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 256 * 1024:
            raise ValueError("Runtime request exceeds the bounded UTF-8 payload")
        body.extend(chunk)
    return json.loads(body)


def task_routes(service: TaskService) -> list[BaseRoute]:
    async def handle(request: Request) -> JSONResponse:
        try:
            task_id = request.path_params.get("task_id")
            action = request.path_params.get("action")
            result: Any
            if request.method == "POST" and task_id is None:
                body = Submission.model_validate(await request_body(request))
                result = service.submit(
                    body.prompt, body.source, body.request_id
                ).model_dump(mode="json")
            elif request.method == "POST":
                assert isinstance(task_id, str)
                if action == "messages":
                    message = MessageSubmission.model_validate(
                        await request_body(request)
                    )
                    result = service.message(
                        task_id,
                        message.prompt,
                        message.source,
                        message.request_id,
                        message.expected_sequence,
                    ).model_dump(mode="json")
                elif action == "cancel":
                    result = (await service.cancel(task_id)).model_dump(mode="json")
                elif action == "reconcile":
                    body_review = RecoveryReview.model_validate(
                        await request_body(request)
                    )
                    result = service.reconcile(
                        task_id, body_review.model_dump()
                    ).model_dump(mode="json")
                elif action == "continue":
                    result = service.continue_task(task_id).model_dump(mode="json")
                else:
                    return JSONResponse({"error": "unknown_action"}, 404)
            elif task_id is None:
                result = [task.model_dump(mode="json") for task in service.list_tasks()]
            elif action == "events":
                after = int(request.query_params.get("after", "0"))
                events = (
                    await service.follow(task_id, after)
                    if request.query_params.get("wait") == "1"
                    else service.events(task_id, after)
                )
                result = {
                    "events": [event.model_dump(mode="json") for event in events],
                    "cursor": events[-1].sequence if events else after,
                }
            elif action == "recovery":
                result = service.recovery(task_id)
            elif action == "history":
                result = (
                    service.history_reader(service.inspect(task_id))
                    if service.history_reader
                    else []
                )
            else:
                result = service.inspect(task_id).model_dump(mode="json")
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except KeyError:
            return JSONResponse({"error": "task_not_found"}, 404)
        except (ValueError, ValidationError):
            return JSONResponse({"error": "invalid_request_or_unsafe_continue"}, 409)
        except asyncio.QueueFull:
            return JSONResponse({"error": "runtime_queue_full"}, 429)
        except RuntimeError:
            return JSONResponse({"error": "runtime_unavailable"}, 503)

    async def stream(websocket: WebSocket) -> None:
        task_id = websocket.path_params["task_id"]
        receiving: asyncio.Task[Any] | None = None
        pending: asyncio.Task[Any] | None = None
        try:
            cursor = int(websocket.query_params.get("after", "0"))
            service.events(task_id, cursor)
            async with service.subscribe(task_id) as queue:
                await websocket.accept()

                async def disconnect() -> None:
                    while True:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            return

                receiving = asyncio.create_task(disconnect())
                # Subscribe before replay, then deduplicate durable queue entries.
                while batch := service.events(task_id, cursor):
                    for event in batch:
                        await asyncio.wait_for(
                            websocket.send_json(
                                {
                                    "type": "event",
                                    "event": event.model_dump(mode="json"),
                                }
                            ),
                            5,
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
                    if message["type"] == "event":
                        seq = message["event"]["sequence"]
                        if seq <= cursor:
                            continue
                        cursor = seq
                    await asyncio.wait_for(websocket.send_json(message), 5)
                    if message["type"] in {"resync", "shutdown"}:
                        await websocket.close(code=1013)
                        return
        except (ValueError, KeyError, RuntimeError, WebSocketDisconnect, TimeoutError):
            return
        finally:
            await settle_owned(
                *(task for task in (receiving, pending) if task is not None)
            )
            with suppress(RuntimeError):
                await websocket.close()

    return [
        WebSocketRoute("/api/runtime/tasks/{task_id}/stream", stream),
        Route("/api/runtime/tasks", handle, methods=["GET", "POST"]),
        Route("/api/runtime/tasks/{task_id}", handle, methods=["GET"]),
        Route("/api/runtime/tasks/{task_id}/{action}", handle, methods=["GET", "POST"]),
    ]
