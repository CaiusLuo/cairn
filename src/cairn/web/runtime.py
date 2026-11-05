"""HTTP task routes; these never construct an Agent or open a writer Store."""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from cairn.infra.runtime_catalog import RuntimeSource
from cairn.runtime.task_service import TaskService


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=32000)
    source: RuntimeSource
    request_id: str | None = Field(default=None, max_length=200)


def task_routes(service: TaskService) -> list[BaseRoute]:
    async def handle(request: Request) -> JSONResponse:
        try:
            task_id = request.path_params.get("task_id")
            action = request.path_params.get("action")
            result: Any
            if request.method == "POST" and task_id is None:
                body = Submission.model_validate(await request.json())
                result = service.submit(
                    body.prompt, body.source, body.request_id
                ).model_dump(mode="json")
            elif request.method == "POST":
                assert isinstance(task_id, str)
                if action == "cancel":
                    result = (await service.cancel(task_id)).model_dump(mode="json")
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

    return [
        Route("/api/runtime/tasks", handle, methods=["GET", "POST"]),
        Route("/api/runtime/tasks/{task_id}", handle, methods=["GET"]),
        Route("/api/runtime/tasks/{task_id}/{action}", handle, methods=["GET", "POST"]),
    ]
