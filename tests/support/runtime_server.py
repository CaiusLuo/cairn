"""Run the production Server with an offline model paused at host-owned gates."""

import asyncio
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

from cairn.agent.models.messages import LLMResponse, LLMUsage, Message, ToolCall
from cairn.agent.models.offline import OfflineDevelopmentModel
from cairn.server import bootstrap as runtime_server


class GatedModel(OfflineDevelopmentModel):
    async def generate(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None
    ) -> LLMResponse:
        gates = Path(os.environ["CAIRN_TEST_GATES"])
        phase = "after" if any(m.role == "tool" for m in messages) else "before"
        gate = gates / phase
        if gate.exists():
            (gates / (phase + ".entered")).touch()
            while gate.exists():
                await asyncio.sleep(0.02)
        names = {tool["function"]["name"] for tool in tools or ()}
        if (
            os.environ.get("CAIRN_TEST_CHAT_TOOL") == "1"
            and "read_file" in names
            and not any(message.role == "tool" for message in messages)
        ):
            return LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="gated-read",
                        name="read_file",
                        arguments={"path": "tracked.txt"},
                    )
                ],
                usage=LLMUsage(input_tokens=5, output_tokens=2),
            )
        return await super().generate(messages, tools)


if __name__ == "__main__":
    if os.environ.get("CAIRN_TEST_ALLOW_INSECURE_LOCAL_WECHAT") == "1":
        from cairn.integrations.wechat.session import WechatSessionController

        original_init = WechatSessionController.__init__

        def insecure_init(self: Any, *args: Any, **kwargs: Any) -> None:
            kwargs["allow_insecure_local_http"] = True
            original_init(self, *args, **kwargs)

        WechatSessionController.__init__ = insecure_init  # type: ignore[method-assign]

    with patch.object(runtime_server, "OfflineDevelopmentModel", GatedModel):
        runtime_server.main()
