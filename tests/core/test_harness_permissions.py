from enum import StrEnum
from unittest.mock import Mock

import pytest

from cairn.core.models import ToolCall
from cairn.core.permissions import (
    CapabilityPermissionHandler,
    PermissionCapability,
    PermissionChoice,
    PermissionDecision,
    PermissionRequest,
    PermissionSource,
    SessionPermissionHandler,
    evaluate_permission_policy,
    requested_capability,
)


def publication_request() -> PermissionRequest:
    return PermissionRequest(
        capability=PermissionCapability.NETWORK,
        justification="Publish verified commit to owner/repo as a Draft PR",
        tool_call=ToolCall(
            id="harness-publication",
            name="github_publish",
            arguments={
                "repository": "owner/repo",
                "commit": "a" * 40,
                "head": "codex/issue-20",
                "base": "main",
                "draft": True,
            },
        ),
    )


def test_harness_request_allow_once_requires_each_explicit_approval() -> None:
    request = publication_request()
    prompt = Mock(return_value=PermissionChoice.ALLOW_ONCE)
    handler: CapabilityPermissionHandler = SessionPermissionHandler(prompt)

    first = handler.authorize(request)
    second = handler.authorize(request)

    for result in (first, second):
        assert result.policy_decision is PermissionDecision.ASK
        assert result.allowed and result.prompted
        assert result.source is PermissionSource.USER_ONCE
        assert result.granted_capabilities == frozenset({PermissionCapability.NETWORK})
    assert prompt.call_count == 2
    assert all(call.args == (request,) for call in prompt.call_args_list)
    assert isinstance(handler, SessionPermissionHandler)
    assert not handler.grants


def test_harness_request_reuses_and_resets_existing_network_session_grant() -> None:
    request = publication_request()
    prompt = Mock(return_value=PermissionChoice.ALLOW_SESSION)
    handler = SessionPermissionHandler(prompt)

    first = handler.authorize(request)
    bash = handler(
        ToolCall(
            id="network-bash",
            name="bash",
            arguments={
                "command": "git fetch",
                "network_access": True,
                "justification": "fetch explicitly requested remote",
            },
        )
    )
    second = handler.authorize(request)

    assert first.source is PermissionSource.USER_SESSION
    assert first.prompted
    assert first.granted_capabilities == frozenset({PermissionCapability.NETWORK})
    for result in (bash, second):
        assert result.policy_decision is PermissionDecision.ASK
        assert result.allowed and not result.prompted
        assert result.source is PermissionSource.SESSION_GRANT
        assert result.granted_capabilities == frozenset({PermissionCapability.NETWORK})
    assert prompt.call_count == 1
    assert handler.grants == {PermissionCapability.NETWORK}

    handler.reset_grants()
    assert handler.authorize(request).source is PermissionSource.USER_SESSION
    assert prompt.call_count == 2


@pytest.mark.parametrize(
    "choice, source, prompted",
    [
        (PermissionChoice.DENY, PermissionSource.USER_DENIED, True),
        (None, PermissionSource.NO_HANDLER, False),
    ],
)
def test_harness_denial_grants_no_authority(
    choice: PermissionChoice | None, source: PermissionSource, prompted: bool
) -> None:
    handler = SessionPermissionHandler(
        None if choice is None else Mock(return_value=choice)
    )

    result = handler.authorize(publication_request())

    assert result.policy_decision is PermissionDecision.ASK
    assert not result.allowed
    assert result.prompted is prompted
    assert result.source is source
    assert not result.granted_capabilities
    assert not handler.grants


class AlternateChoice(StrEnum):
    ALLOW_ONCE = "allow_once"
    ALLOW_SESSION = "allow_session"


@pytest.mark.parametrize(
    "choice",
    [None, "allow_once", "allow_session", "deny", AlternateChoice.ALLOW_ONCE],
)
def test_harness_invalid_choice_fails_closed(choice: object) -> None:
    handler = SessionPermissionHandler(Mock(return_value=choice))

    with pytest.raises(ValueError, match="Invalid permission choice"):
        handler.authorize(publication_request())

    assert not handler.grants


class AlternateCapability(StrEnum):
    NETWORK = "network"


@pytest.mark.parametrize(
    "capability", [None, "network", "unknown", AlternateCapability.NETWORK]
)
@pytest.mark.parametrize("existing_grant", [False, True])
def test_harness_invalid_capability_cannot_prompt_or_reuse_session_grant(
    capability: object, existing_grant: bool
) -> None:
    request = publication_request().model_copy(update={"capability": capability})
    prompt = Mock(return_value=PermissionChoice.ALLOW_SESSION)
    handler = SessionPermissionHandler(prompt)
    if existing_grant:
        handler.grants.add(PermissionCapability.NETWORK)
    before = set(handler.grants)

    with pytest.raises(ValueError, match="Invalid permission capability"):
        handler.authorize(request)

    prompt.assert_not_called()
    assert handler.grants == before


def test_harness_request_does_not_change_model_tool_permission_policy() -> None:
    call = publication_request().tool_call
    assert requested_capability(call) is None
    result = evaluate_permission_policy(call)
    assert result.policy_decision is PermissionDecision.ALLOW
    assert result.source is PermissionSource.BASELINE
    assert not result.granted_capabilities

    handler = SessionPermissionHandler(Mock(return_value=PermissionChoice.ALLOW_ONCE))
    assert not handler(call).granted_capabilities
