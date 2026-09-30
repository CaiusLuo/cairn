import pytest

from cairn.core.models import ToolCall
from cairn.core.permissions import (
    PermissionCapability,
    PermissionChoice,
    PermissionDecision,
    PermissionRequest,
    SessionPermissionHandler,
    check_permission,
)


@pytest.mark.parametrize(
    "name, arguments",
    [
        ("read_file", {"path": "README.md"}),
        ("edit_file", {"path": "README.md"}),
        *[
            ("bash", {"command": command})
            for command in (
                "pwd",
                "ls",
                "git status --short",
                "uv run pytest",
                "python script.py",
                "printf x > README.md",
                'printf x > "$HOME/outside.txt"',
                "curl example.com",
            )
        ],
    ],
)
def test_baseline_without_prompt(name: str, arguments: dict[str, object]) -> None:
    def unexpected(request: PermissionRequest) -> PermissionChoice:
        pytest.fail("baseline prompted")

    call = ToolCall(id="1", name=name, arguments=arguments)
    result = SessionPermissionHandler(unexpected)(call)
    assert result.policy_decision == PermissionDecision.ALLOW
    assert result.allowed and not result.prompted
    assert result.source == "baseline"
    assert not result.granted_capabilities


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"command": None},
        {"command": 1},
        {"command": ""},
        {"command": " "},
        {"command": "'unterminated"},
        {"command": "\0"},
        {"command": "sudo true"},
        {"command": "pwd", "extra": True},
        {"command": "pwd", "network_access": "true"},
        {"command": "pwd", "network_access": True},
        {"command": "pwd", "network_access": True, "justification": " "},
        {"command": "pwd", "justification": 123},
    ],
)
def test_denied_bash(arguments: dict[str, object]) -> None:
    result = SessionPermissionHandler()(
        ToolCall(id="1", name="bash", arguments=arguments)
    )
    assert result.policy_decision == PermissionDecision.DENY
    assert not result.allowed and not result.prompted
    assert result.source == "hard_deny"


def network_call(command: str = "curl example.com") -> ToolCall:
    return ToolCall(
        id="1",
        name="bash",
        arguments={
            "command": command,
            "network_access": True,
            "justification": "needed",
        },
    )


def test_unknown_tool_denied() -> None:
    assert (
        check_permission(ToolCall(id="1", name="unknown", arguments={}))
        == PermissionDecision.DENY
    )


@pytest.mark.parametrize(
    "choice, source",
    [
        (PermissionChoice.ALLOW_ONCE, "user_once"),
        (PermissionChoice.ALLOW_SESSION, "session_grant"),
        (PermissionChoice.DENY, "user_denied"),
    ],
)
def test_session_semantics(choice: PermissionChoice, source: str) -> None:
    prompts: list[PermissionRequest] = []

    def prompt(request: PermissionRequest) -> PermissionChoice:
        prompts.append(request)
        return choice

    handler = SessionPermissionHandler(prompt)
    first = handler(network_call())
    second = handler(network_call("python other.py"))
    assert first.source == second.source == source
    assert first.prompted
    assert second.prompted == (choice != PermissionChoice.ALLOW_SESSION)
    assert len(prompts) == (1 if choice == PermissionChoice.ALLOW_SESSION else 2)
    assert first.allowed == (choice != PermissionChoice.DENY)
    assert first.granted_capabilities == (
        frozenset()
        if choice == PermissionChoice.DENY
        else frozenset({PermissionCapability.NETWORK})
    )
    local = handler(ToolCall(id="2", name="bash", arguments={"command": "pwd"}))
    assert local.allowed and not local.prompted and not local.granted_capabilities
    assert SessionPermissionHandler(prompt)(network_call()).prompted


def test_missing_handler_denies_capability() -> None:
    result = SessionPermissionHandler()(network_call())
    assert not result.allowed and not result.prompted
    assert result.source == "no_handler"
    assert not result.granted_capabilities
