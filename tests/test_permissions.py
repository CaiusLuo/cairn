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
    requested_capability,
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


def test_sudo_guardrail_denies_before_execution() -> None:
    result = evaluate_permission_policy(bash_call("sudo true"))

    assert result.policy_decision == PermissionDecision.DENY
    assert not result.allowed and not result.prompted
    assert result.source == PermissionSource.POLICY_DENY
    assert result.reason == "sudo is not supported"


@pytest.mark.parametrize(
    "command",
    [
        "/usr/bin/sudo true",
        "true; sudo rm -rf /tmp/x",
        "bash -c 'sudo true'",
    ],
)
def test_sudo_guardrail_is_best_effort_only(command: str) -> None:
    """The sandbox is the security boundary; command parsing is not.

    Only a literal leading ``sudo`` token is caught. Shell indirection is
    deliberately out of scope and must not grow into a shell analyzer.
    """
    assert (
        evaluate_permission_policy(bash_call(command)).policy_decision
        == PermissionDecision.ALLOW
    )


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
        {"command": "pwd", "extra": True},
        {"command": "pwd", "network_access": "true"},
        {"command": "pwd", "network_access": True},
        {"command": "pwd", "network_access": True, "justification": " "},
        {"command": "pwd", "justification": 123},
    ],
)
def test_invalid_arguments_are_not_permission_decisions(
    arguments: dict[str, object],
) -> None:
    """Argument validity belongs to the tool contract, not to the policy.

    Malformed calls are neither approved nor denied here: they fall through to
    the baseline and the Bash tool reports an invalid-arguments failure. Shell
    syntax errors are the shell's to report as well.
    """
    call = ToolCall(id="1", name="bash", arguments=arguments)

    assert requested_capability(call) is None
    assert evaluate_permission_policy(call).policy_decision == PermissionDecision.ALLOW


def test_policy_does_not_answer_tool_existence() -> None:
    """Registration is the registry's truth; the policy stays silent about it.

    The loop resolves the tool before consulting the policy, so an unknown name
    becomes ToolNotFound rather than a fabricated permission decision.
    """
    call = ToolCall(id="1", name="unknown", arguments={})

    assert evaluate_permission_policy(call).policy_decision == PermissionDecision.ALLOW


def test_requested_capability_requires_a_complete_request() -> None:
    assert requested_capability(network_call()) == PermissionCapability.NETWORK
    assert (
        requested_capability(
            ToolCall(
                id="1",
                name="read_file",
                arguments={"network_access": True, "justification": "x"},
            )
        )
        is None
    )
    assert requested_capability(bash_call("curl example.com")) is None
    assert (
        requested_capability(bash_call("curl example.com", network_access=True)) is None
    )
    assert (
        requested_capability(
            bash_call("curl example.com", network_access=True, justification="   ")
        )
        is None
    )


@pytest.mark.parametrize(
    "choice, sources",
    [
        (
            PermissionChoice.ALLOW_ONCE,
            (PermissionSource.USER_ONCE, PermissionSource.USER_ONCE),
        ),
        (
            PermissionChoice.ALLOW_SESSION,
            (PermissionSource.USER_SESSION, PermissionSource.SESSION_GRANT),
        ),
        (
            PermissionChoice.DENY,
            (PermissionSource.USER_DENIED, PermissionSource.USER_DENIED),
        ),
    ],
)
def test_session_semantics(
    choice: PermissionChoice, sources: tuple[PermissionSource, ...]
) -> None:
    prompts: list[PermissionRequest] = []

    def prompt(request: PermissionRequest) -> PermissionChoice:
        prompts.append(request)
        return choice

    handler = SessionPermissionHandler(prompt)
    first = handler(network_call())
    second = handler(network_call("python other.py"))
    assert first.source == sources[0]
    assert second.source == sources[1]
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

    assert result.policy_decision == PermissionDecision.ASK
    assert not result.allowed and not result.prompted
    assert result.source == PermissionSource.NO_HANDLER
    assert not result.granted_capabilities
