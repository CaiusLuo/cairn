# mypy: ignore-errors
"""Tiny stdio MCP server used by offline tests (JSON-RPC, newline delimited)."""

import json
import os
import sys


def main() -> None:
    marker = os.environ.get("MCP_TEST_MARKER")
    if marker:
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("started\n")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "demo", "version": "0.0"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo text back",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    }
                ]
            }
        elif method == "tools/call":
            params = message.get("params") or {}
            if params.get("name") != "echo":
                result = {
                    "content": [{"type": "text", "text": "unknown tool"}],
                    "isError": True,
                }
            else:
                arguments = params.get("arguments") or {}
                result = {
                    "content": [
                        {"type": "text", "text": str(arguments.get("text", ""))}
                    ],
                    "isError": False,
                }
        else:
            if request_id is None:
                continue
            sys.stdout.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32601, "message": "method not found"},
                    }
                )
                + "\n"
            )
            sys.stdout.flush()
            continue
        if request_id is not None:
            sys.stdout.write(
                json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result})
                + "\n"
            )
            sys.stdout.flush()


if __name__ == "__main__":
    main()
