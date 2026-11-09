"""Isolated shared Server with a real catalogue and offline model."""

from pathlib import Path
from typing import Any

import httpx

from cairn.agent.budget import RunBudget
from cairn.server.bootstrap import runtime_app
from cairn.server.client import RuntimeClient
from tests.support.git import initialize_repository


def control_runtime(root: Path) -> tuple[Any, RuntimeClient]:
    source = root / "source"
    if not source.exists():
        source.mkdir()
        initialize_repository(source)
    app = runtime_app(
        source,
        root / "state",
        root / "worktrees",
        budget=RunBudget(8),
        token="private-test-token",
    )
    return app, RuntimeClient(
        "http://127.0.0.1:8777",
        "private-test-token",
        transport=httpx.ASGITransport(app=app),
        workspace=source,
    )
