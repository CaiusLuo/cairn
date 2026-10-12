"""Credential-free GitHub boundary models.

This package root stays import-light on purpose: importing it must not load the
HTTP/urllib transport. Import `cairn.infra.github.http`,
`cairn.infra.github.source` or `cairn.infra.github.transport` directly when the
network boundary is actually needed.
"""

from cairn.infra.github.models import (
    GitHubRepository,
    IssuePayload,
    IssueReference,
    IssueTask,
    validate_branch_name,
)

__all__ = [
    "GitHubRepository",
    "IssuePayload",
    "IssueReference",
    "IssueTask",
    "validate_branch_name",
]
