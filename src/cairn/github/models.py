"""Credential-free identity and task models for the GitHub boundary."""

import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cairn.tasks.models import TaskSpec


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", hide_input_in_errors=True
    )


class GitHubRepository(_FrozenModel):
    owner: str
    name: str

    @field_validator("owner")
    @classmethod
    def validate_owner(cls, value: str) -> str:
        if (
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", value)
            is None
            or "--" in value
        ):
            raise ValueError("Invalid GitHub owner slug")
        return value

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value) is None or value in {
            ".",
            "..",
        }:
            raise ValueError("Invalid GitHub repository slug")
        return value

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    def matches(self, other: "GitHubRepository") -> bool:
        return self.full_name.casefold() == other.full_name.casefold()


class IssueReference(_FrozenModel):
    repository: GitHubRepository
    number: int = Field(gt=0)


def validate_branch_name(value: str) -> str:
    """Validate an explicit Git branch name without invoking a subprocess."""
    if (
        not isinstance(value, str)
        or not value
        or value in {"HEAD", "@"}
        or value.startswith(("-", ".", "/"))
        or value.endswith((".", "/"))
        or any(part in value for part in ("..", "//", "@{"))
        or any(
            character in "~^:?*[\\"
            or character.isspace()
            or ord(character) < 32
            or ord(character) == 127
            for character in value
        )
        or any(
            segment.startswith(".") or segment.endswith(".lock")
            for segment in value.split("/")
        )
    ):
        raise ValueError("Invalid explicit Git branch name")
    return value


def validate_issue_url(url: str, repository: GitHubRepository, number: int) -> None:
    """Require the expected public issue URL; reject credentials and aliases."""
    parsed = urlsplit(url)
    expected_path = f"/{repository.full_name}/issues/{number}"
    if (
        parsed.scheme != "https"
        or parsed.netloc.casefold() != "github.com"
        or parsed.path.casefold() != expected_path.casefold()
        or parsed.query
        or parsed.fragment
        or any(ord(character) < 33 or ord(character) == 127 for character in url)
    ):
        raise ValueError("GitHub issue URL does not match its identity")


class IssuePayload(_FrozenModel):
    repository: GitHubRepository
    number: int = Field(gt=0)
    title: str
    body: str | None
    state: Literal["open", "closed"]
    pull_request: bool
    url: str

    @model_validator(mode="after")
    def validate_url(self) -> "IssuePayload":
        validate_issue_url(self.url, self.repository, self.number)
        return self


class IssueTask(_FrozenModel):
    source: IssueReference
    source_url: str
    title: str
    task: TaskSpec
    base_branch: str
    intended_fix: bool = False

    @field_validator("base_branch")
    @classmethod
    def validate_base_branch(cls, value: str) -> str:
        return validate_branch_name(value)

    @model_validator(mode="after")
    def validate_source_url(self) -> "IssueTask":
        validate_issue_url(self.source_url, self.source.repository, self.source.number)
        return self
