"""Default private Agent state location for trust and personal memory."""

import os
from pathlib import Path


def default_agent_home() -> Path:
    override = os.environ.get("CAIRN_AGENT_HOME")
    if override:
        return Path(override)
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "cairn" / "agent"
