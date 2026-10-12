"""Read-only, freshly rebuilt, bounded RepoGraph queries for the experiment."""

import asyncio
import json
from collections.abc import Callable
from dataclasses import asdict
from time import perf_counter
from typing import Any

from cairn.core.memory.models import safe_path
from cairn.core.models import ToolResult
from cairn.infra.repo_graph.builder import RepoGraphBuilder
from cairn.infra.repo_graph.models import BuildLimits, RelationshipStatus
from cairn.infra.tools.base import InvalidArguments, ToolExecutionContext
from cairn.infra.workspace.workspace import Workspace

GraphObserver = Callable[[tuple[str, ...], float, bool], None]


class RepoGraphQueryTool:
    name = "repo_graph"
    description = (
        "Query current Python repository structure without shell execution. "
        "Operations: list_files (optional path_prefix), find_symbols (optional exact "
        "name), module_for_path (path), dependencies_of (path). Each call rebuilds "
        "a bounded snapshot; incomplete snapshots return no facts. Query flags mark "
        "unsupported structure and truncation; no source bodies are returned."
    )

    def __init__(
        self,
        workspace: Workspace,
        *,
        observer: GraphObserver | None = None,
        limits: BuildLimits | None = None,
    ) -> None:
        self.builder = RepoGraphBuilder(
            workspace,
            limits=limits
            or BuildLimits(
                max_entries=256,
                max_depth=10,
                max_file_bytes=64 * 1024,
                max_total_bytes=1024 * 1024,
                max_issues=8,
                max_ast_nodes=5000,
                max_structure_records=1000,
            ),
        )
        self.observer = observer

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": [
                                "list_files",
                                "find_symbols",
                                "module_for_path",
                                "dependencies_of",
                            ],
                        },
                        "path": {"type": "string", "maxLength": 512},
                        "path_prefix": {"type": "string", "maxLength": 512},
                        "name": {"type": "string", "maxLength": 128},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    "required": ["operation"],
                    "additionalProperties": False,
                },
            },
        }

    def validate(self, arguments: dict[str, Any]) -> None:
        operation = arguments.get("operation")
        allowed = {
            "list_files": {"operation", "path_prefix", "limit"},
            "find_symbols": {"operation", "name", "limit"},
            "module_for_path": {"operation", "path"},
            "dependencies_of": {"operation", "path", "limit"},
        }
        if not isinstance(operation, str) or operation not in allowed:
            raise InvalidArguments("Unsupported graph operation")
        if arguments.keys() - allowed[operation]:
            raise InvalidArguments("Unexpected graph argument")
        if "limit" in arguments and (
            type(arguments["limit"]) is not int or not 1 <= arguments["limit"] <= 20
        ):
            raise InvalidArguments("Graph limit must be between 1 and 20")
        if (
            operation in {"module_for_path", "dependencies_of"}
            and "path" not in arguments
        ):
            raise InvalidArguments("Graph path required")
        for key in ("path", "path_prefix"):
            if key in arguments:
                try:
                    if key != "path_prefix" or arguments[key] != ".":
                        safe_path(arguments[key])
                except (TypeError, ValueError):
                    raise InvalidArguments("Invalid graph path") from None
        name = arguments.get("name")
        if "name" in arguments and (
            not isinstance(name, str) or not name.isidentifier() or len(name) > 128
        ):
            raise InvalidArguments("Invalid graph symbol name")

    def initialize(self) -> None:
        started = perf_counter()
        graph = self.builder.build()
        if self.observer is not None:
            self.observer((), perf_counter() - started, graph.report.complete)

    async def execute(
        self, arguments: dict[str, Any], *, context: ToolExecutionContext | None = None
    ) -> ToolResult:
        self.validate(arguments)
        await asyncio.sleep(0)
        started = perf_counter()
        # Always use this query's candidate, never a retained historical snapshot.
        graph = self.builder.refresh()
        elapsed = perf_counter() - started
        await asyncio.sleep(0)
        if not graph.report.complete:
            data: dict[str, Any] = {
                "items": [],
                "complete": False,
                "truncated": False,
                "snapshot_complete": False,
                "freshness": "unknown",
            }
            paths: tuple[str, ...] = ()
        else:
            operation = arguments["operation"]
            limit = arguments.get("limit", 20)
            if operation == "list_files":
                result = asdict(
                    graph.list_files(
                        path_prefix=arguments.get("path_prefix"), limit=limit
                    )
                )
            elif operation == "find_symbols":
                result = asdict(
                    graph.find_symbols(name=arguments.get("name"), limit=limit)
                )
            elif operation == "module_for_path":
                result = asdict(graph.module_for_path(arguments["path"]))
            else:
                result = asdict(graph.dependencies_of(arguments["path"], limit=limit))
            data = result
            data.update(snapshot_complete=True, freshness="rebuilt")
            returned_paths = [record["path"] for record in result["items"]]
            if operation == "dependencies_of":
                returned_paths.extend(
                    record["target_path"]
                    for record in result["items"]
                    if record["status"] is RelationshipStatus.RESOLVED
                    and isinstance(record["target_path"], str)
                )
            paths = tuple(returned_paths)
        payload = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
        if len(payload.encode()) > 8192:
            payload = json.dumps(
                {
                    "items": [],
                    "complete": False,
                    "truncated": True,
                    "snapshot_complete": graph.report.complete,
                    "freshness": "rebuilt" if graph.report.complete else "unknown",
                }
            )
            paths = ()
        if self.observer is not None:
            self.observer(paths, elapsed, graph.report.complete)
        return ToolResult(stdout=payload, exit_code=0)
