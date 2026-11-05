"""Explicit offline acceptance model using the real write_file tool."""

import json
from typing import Any

from cairn.core.models import LLMResponse, LLMUsage, Message, ToolCall


class OfflineDevelopmentModel:
    async def generate(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None
    ) -> LLMResponse:
        facts = [message for message in messages if message.role == "tool"]
        if facts:
            result = json.loads(facts[-1].content or "{}")
            return LLMResponse(
                content="离线开发链路完成: runtime-result.txt 已保存。"
                if result.get("exit_code") == 0
                else "工具未完成, 请检查权限或错误记录。",
                usage=LLMUsage(input_tokens=20, output_tokens=10),
            )
        return LLMResponse(
            tool_calls=[
                ToolCall(
                    id="offline-write",
                    name="write_file",
                    arguments={
                        "path": "runtime-result.txt",
                        "content": "Cairn shared runtime task completed\n",
                        "expected_content": "",
                    },
                )
            ],
            usage=LLMUsage(input_tokens=20, output_tokens=10),
        )
