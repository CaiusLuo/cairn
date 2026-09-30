import pytest

from cairn.core.models import ToolCall
from cairn.core.permissions import (
    PermissionCapability,
    PermissionChoice,
    PermissionDecision,
    PermissionRequest,
    PermissionSource,
    SessionPermissionHandler,
    evaluate_permission_policy,
)


def bash_call(command: str, **extra: object) -> ToolCall:
    return ToolCall(id="1", name="bash", arguments={"command": command, **extra})


def network_call(command: str = "curl example.com") -> ToolCall:
    return bash_call(
        command,
        network_access=True,
        justification="needed",
    )


@pytest.mark.parametrize(
    "name, arguments",
    [
        ("read_file", {"path": "README.md"}),
        ("edit_file", {"path": "README.md"}),
        ("bash", {"command": "git status --short"}),
        ("bash", {"command": "uv run pytest"}),
        ("bash", {"command": "printf x > README.md"}),
    ],
)
def test_baseline_without_prompt(name: str, arguments: dict[str, object]) -> None:
    call = ToolCall(id="1", name=name, arguments=arguments)

    result = evaluate_permission_policy(call)

    assert result.policy_decision == PermissionDecision.ALLOW
    assert result.allowed and not result.prompted
    assert result.source == PermissionSource.BASELINE
    assert not result.granted_capabilities

    def unexpected(request: PermissionRequest) -> PermissionChoice:
        pytest.fail("baseline operation asked for approval")

    # The approval handler is never consulted for baseline ALLOW decisions.
    prompt_only = SessionPermissionHandler(unexpected)(call)
    assert prompt_only.allowed and not prompt_only.prompted


def test_session_grant_is_scoped_and_resets_for_new_handler() -> None:
    prompts: list[PermissionRequest] = []

    def prompt(request: PermissionRequest) -> PermissionChoice:
        prompts.append(request)
        return PermissionChoice.ALLOW_SESSION

    handler = SessionPermissionHandler(prompt)
    first = handler(network_call())
    second = handler(network_call("python other.py"))
    assert first.source == PermissionSource.USER_SESSION
    assert second.source == PermissionSource.SESSION_GRANT
    assert first.prompted
    assert not second.prompted
    assert len(prompts) == 1
    assert first.allowed and second.allowed
    assert first.granted_capabilities == frozenset({PermissionCapability.NETWORK})
    assert second.granted_capabilities == frozenset({PermissionCapability.NETWORK})

    local = handler(bash_call("curl example.com", network_access=False))
    assert local.allowed and not local.prompted and not local.granted_capabilities
    assert SessionPermissionHandler(prompt)(network_call()).prompted
