"""Run the production Server with an offline model paused at host-owned gates."""

import asyncio
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

from cairn import server as runtime_server
from cairn.core.models import LLMResponse, Message
from cairn.runtime.offline_model import OfflineDevelopmentModel


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
        return await super().generate(messages, tools)


if __name__ == "__main__":
    with patch.object(runtime_server, "OfflineDevelopmentModel", GatedModel):
        runtime_server.main()
