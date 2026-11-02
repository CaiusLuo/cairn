from pathlib import Path

from cairn.infra.tools.defaults import build_default_registry
from cairn.infra.workspace.workspace import Workspace
from tests.support.runtime import RecordingTool


def test_read_only_registry_has_no_effect_tools(tmp_path: Path) -> None:
    registry = build_default_registry(
        Workspace(tmp_path),
        read_only=True,
        extra_tools=(RecordingTool(),),
    )
    assert registry.tools == {}
