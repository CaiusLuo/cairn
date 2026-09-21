import pytest

from cairn.core.models import ToolCall
from cairn.core.permissions import PermissionDecision, check_permission

PERMISSION_CASES: tuple[tuple[ToolCall, PermissionDecision], ...] = (
    (
        ToolCall(id="1", name="other", arguments={}),
        PermissionDecision.ASK,
    ),
    (
        ToolCall(id="2", name="bash", arguments={}),
        PermissionDecision.DENY,
    ),
    (
        ToolCall(id="3", name="bash", arguments={"command": "pwd | cat"}),
        PermissionDecision.ASK,
    ),
    (
        ToolCall(id="4", name="bash", arguments={"command": "'unterminated"}),
        PermissionDecision.DENY,
    ),
    (
        ToolCall(id="5", name="bash", arguments={"command": "   "}),
        PermissionDecision.DENY,
    ),
    (
        ToolCall(id="6", name="bash", arguments={"command": "pwd -P"}),
        PermissionDecision.ALLOW,
    ),
    (
        ToolCall(id="7", name="bash", arguments={"command": "sudo true"}),
        PermissionDecision.DENY,
    ),
    (
        ToolCall(id="8", name="bash", arguments={"command": "git status --short"}),
        PermissionDecision.ALLOW,
    ),
    (
        ToolCall(id="9", name="bash", arguments={"command": "git branch"}),
        PermissionDecision.ASK,
    ),
    (
        ToolCall(id="10", name="bash", arguments={"command": "python -V"}),
        PermissionDecision.ASK,
    ),
)


@pytest.mark.parametrize(("tool_call", "expected"), PERMISSION_CASES)
def test_check_permission(
    tool_call: ToolCall,
    expected: PermissionDecision,
) -> None:
    assert check_permission(tool_call) == expected
