"""Narrow GitHub publication transport for one verified unsigned commit."""

import base64
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol
from urllib.parse import quote, unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cairn.github.http import GitHubHTTPClient, GitHubHTTPError
from cairn.github.models import GitHubRepository, validate_branch_name

MAX_OBJECT_BYTES = 64 * 1024
MAX_NEW_OBJECTS = 1024
MAX_TOTAL_OBJECT_BYTES = 8 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{40}")


class GitHubPublishError(RuntimeError):
    """Sanitized publication failure; recoverable local work remains caller-owned."""


@dataclass(frozen=True, slots=True)
class GitObject:
    sha: str
    kind: Literal["blob", "tree"]
    data: bytes


@dataclass(frozen=True, slots=True)
class PublishedCommit:
    sha: str
    tree: str
    parent: str
    message: str
    author_name: str
    author_email: str
    date: str
    objects: tuple[GitObject, ...]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", hide_input_in_errors=True
    )


def _sha(value: str) -> str:
    if _SHA.fullmatch(value) is None:
        raise ValueError("Only full SHA-1 revisions are supported")
    return value


class PublishDestination(_FrozenModel):
    repository: GitHubRepository
    base_branch: str
    head_branch: str
    base_revision: str

    @field_validator("base_branch", "head_branch")
    @classmethod
    def validate_branch(cls, value: str) -> str:
        validate_branch_name(value)
        if "%" in value:
            raise ValueError(
                "Percent characters are unsupported in publication branches"
            )
        return value

    @field_validator("base_revision")
    @classmethod
    def validate_revision(cls, value: str) -> str:
        return _sha(value)

    @model_validator(mode="after")
    def validate_branches(self) -> "PublishDestination":
        if self.base_branch == self.head_branch:
            raise ValueError("Publication head must differ from the base branch")
        return self


class DraftPRPayload(_FrozenModel):
    destination: PublishDestination
    commit_revision: str
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(max_length=64 * 1024)
    draft: Literal[True] = True

    @field_validator("commit_revision")
    @classmethod
    def validate_revision(cls, value: str) -> str:
        return _sha(value)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        if not value.strip() or any(
            ord(char) < 32 or ord(char) == 127 for char in value
        ):
            raise ValueError("Invalid Draft PR title")
        return value

    @field_validator("draft", mode="before")
    @classmethod
    def require_draft(cls, value: Any) -> Literal[True]:
        if value is not True:
            raise ValueError("Only Draft PR publication is supported")
        return True


class DraftPR(_FrozenModel):
    number: int = Field(gt=0)
    url: str
    head_revision: str
    draft: Literal[True] = True

    @field_validator("head_revision")
    @classmethod
    def validate_revision(cls, value: str) -> str:
        return _sha(value)

    @field_validator("draft", mode="before")
    @classmethod
    def require_draft(cls, value: Any) -> Literal[True]:
        if value is not True:
            raise ValueError("Only Draft PR publication is supported")
        return True


class GitHubPublishTransport(Protocol):
    async def verify_destination(self, destination: PublishDestination) -> None: ...

    async def publish_commit(
        self, destination: PublishDestination, commit: PublishedCommit
    ) -> None: ...

    async def create_draft_pr(self, payload: DraftPRPayload) -> DraftPR: ...


def _hash(kind: str, data: bytes) -> str:
    return hashlib.sha1(f"{kind} {len(data)}\0".encode() + data).hexdigest()


def _tree_entries(data: bytes) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    names: set[str] = set()
    offset = 0
    while offset < len(data):
        mode_end = data.find(b" ", offset)
        name_end = data.find(b"\0", mode_end + 1)
        if mode_end < 0 or name_end < 0 or name_end + 21 > len(data):
            raise ValueError("Invalid Git tree")
        mode = data[offset:mode_end].decode("ascii")
        try:
            name = data[mode_end + 1 : name_end].decode("utf-8")
        except UnicodeDecodeError:
            raise GitHubPublishError(
                "Git tree names must be UTF-8 for publication"
            ) from None
        if mode == "160000":
            raise GitHubPublishError("Git submodules are unsupported for publication")
        if (
            not name
            or name in {".", ".."}
            or "/" in name
            or name in names
            or mode not in {"40000", "040000", "100644", "100755", "120000"}
        ):
            raise ValueError("Unsupported Git tree entry")
        names.add(name)
        entries.append(
            {
                "path": name,
                "mode": "040000" if mode in {"40000", "040000"} else mode,
                "type": "tree" if mode in {"40000", "040000"} else "blob",
                "sha": data[name_end + 1 : name_end + 21].hex(),
            }
        )
        offset = name_end + 21
    return entries


def _prepare_commit(commit: PublishedCommit) -> tuple[dict[str, Any], list[GitObject]]:
    """Validate exact local bytes and order new children before parent trees."""
    try:
        if not isinstance(commit, PublishedCommit):
            raise ValueError("Invalid commit")
        for revision in (commit.sha, commit.tree, commit.parent):
            if not isinstance(revision, str) or _SHA.fullmatch(revision) is None:
                raise GitHubPublishError(
                    "Only full SHA-1 Git publication revisions are supported"
                )
        if (
            not isinstance(commit.message, str)
            or not commit.message.endswith("\n")
            or "\0" in commit.message
            or len(commit.message.encode("utf-8")) > MAX_OBJECT_BYTES
            or not isinstance(commit.objects, tuple)
            or len(commit.objects) > MAX_NEW_OBJECTS
        ):
            raise ValueError("Unsupported commit")
        for identity in (commit.author_name, commit.author_email):
            if (
                not isinstance(identity, str)
                or not identity
                or identity != identity.strip()
                or any(
                    ord(char) < 32 or ord(char) == 127 or char in "<>"
                    for char in identity
                )
            ):
                raise ValueError("Unsupported commit identity")
        date = datetime.fromisoformat(commit.date.replace("Z", "+00:00"))
        if date.utcoffset() != timedelta(0) or date.microsecond:
            raise ValueError("Only whole-second UTC commit dates are supported")
        timestamp = int(date.timestamp())
        person = f"{commit.author_name} <{commit.author_email}>"
        raw = (
            f"tree {commit.tree}\nparent {commit.parent}\n"
            f"author {person} {timestamp} +0000\n"
            f"committer {person} {timestamp} +0000\n\n{commit.message}"
        ).encode()
        if len(raw) > MAX_OBJECT_BYTES or _hash("commit", raw) != commit.sha:
            raise ValueError("Commit metadata does not match the verified revision")
        objects: dict[str, GitObject] = {}
        total = 0
        for obj in commit.objects:
            if (
                not isinstance(obj, GitObject)
                or obj.kind not in {"blob", "tree"}
                or not isinstance(obj.data, bytes)
                or len(obj.data) > MAX_OBJECT_BYTES
                or obj.sha in objects
            ):
                raise ValueError("Unsupported Git object")
            _sha(obj.sha)
            if _hash(obj.kind, obj.data) != obj.sha:
                raise ValueError("Git object hash mismatch")
            total += len(obj.data)
            if total > MAX_TOTAL_OBJECT_BYTES:
                raise ValueError("Git publication exceeds the object limit")
            objects[obj.sha] = obj
        ordered: list[GitObject] = []
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(revision: str, expected_kind: str) -> None:
            obj = objects.get(revision)
            if obj is None:
                return  # Unchanged objects belong to the verified remote base.
            if obj.kind != expected_kind or revision in visiting:
                raise ValueError("Invalid Git object graph")
            if revision in visited:
                return
            visiting.add(revision)
            if obj.kind == "tree":
                for entry in _tree_entries(obj.data):
                    visit(entry["sha"], entry["type"])
            visiting.remove(revision)
            visited.add(revision)
            ordered.append(obj)

        visit(commit.tree, "tree")
        if visited != objects.keys():
            raise ValueError("Publication includes unrelated Git objects")
        metadata = {
            "name": commit.author_name,
            "email": commit.author_email,
            "date": commit.date,
        }
        return (
            {
                "tree": commit.tree,
                "parents": [commit.parent],
                "message": commit.message,
                "author": metadata,
                "committer": dict(metadata),
            },
            ordered,
        )
    except GitHubPublishError:
        raise
    except Exception:
        raise GitHubPublishError(
            "Local Git publication objects are unsupported or invalid"
        ) from None


def _destination(value: PublishDestination) -> PublishDestination:
    try:
        return PublishDestination.model_validate(value.model_dump())
    except Exception:
        raise GitHubPublishError("GitHub publication destination is invalid") from None


def _matches_repository(value: Any, repository: GitHubRepository) -> bool:
    return (
        isinstance(value, str) and value.casefold() == repository.full_name.casefold()
    )


def _validate_pr_url(url: str, repository: GitHubRepository, number: int) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc.casefold() != "github.com"
        or parsed.path.casefold() != f"/{repository.full_name}/pull/{number}".casefold()
        or parsed.query
        or parsed.fragment
        or any(ord(char) < 33 or ord(char) == 127 for char in url)
    ):
        raise ValueError("Draft PR URL does not match its destination")


def _valid_ref_url(value: Any, destination: PublishDestination) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    prefix = f"/repos/{destination.repository.full_name}/git/refs/heads/"
    path = unquote(parsed.path)
    return (
        parsed.scheme == "https"
        and parsed.netloc.casefold() == "api.github.com"
        and path[: len(prefix)].casefold() == prefix.casefold()
        and path[len(prefix) :] == destination.head_branch
        and not parsed.query
        and not parsed.fragment
        and not any(ord(char) < 33 or ord(char) == 127 for char in value)
    )


class GitHubRESTPublisher:
    """Upload one SHA-1 commit and create its branch and Draft PR once.

    The client owns fixed-host authentication and bounded HTTP I/O. This adapter
    never changes an existing ref, follows a remote URL, or retries a mutation.
    Harness permission and verification gates belong to the caller.
    """

    def __init__(self, client: GitHubHTTPClient) -> None:
        self.client = client

    async def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            result = await self.client.request(method, path, payload)
            if not isinstance(result, dict):
                raise ValueError("Invalid response")
            return result
        except GitHubHTTPError as exc:
            raise GitHubHTTPError(
                "GitHub publication request failed", status_code=exc.status_code
            ) from None
        except Exception:
            raise GitHubPublishError("GitHub publication request failed") from None

    async def _verify_repository(self, destination: PublishDestination) -> None:
        data = await self._request("GET", f"/repos/{destination.repository.full_name}")
        if not _matches_repository(data.get("full_name"), destination.repository):
            raise GitHubPublishError(
                "GitHub repository does not match the publication target"
            )

    async def _verify_ref(
        self, destination: PublishDestination, branch: str, revision: str
    ) -> None:
        data = await self._request(
            "GET",
            f"/repos/{destination.repository.full_name}/git/ref/heads/{quote(branch, safe='')}",
        )
        obj = data.get("object")
        if (
            data.get("ref") != f"refs/heads/{branch}"
            or not isinstance(obj, dict)
            or obj.get("type") != "commit"
            or obj.get("sha") != revision
        ):
            raise GitHubPublishError(
                "GitHub branch revision does not match publication evidence"
            )

    async def verify_destination(self, destination: PublishDestination) -> None:
        destination = _destination(destination)
        try:
            await self._verify_repository(destination)
            await self._verify_ref(
                destination, destination.base_branch, destination.base_revision
            )
            try:
                await self._request(
                    "GET",
                    f"/repos/{destination.repository.full_name}/git/ref/heads/{quote(destination.head_branch, safe='')}",
                )
            except GitHubHTTPError as exc:
                if exc.status_code == 404:
                    return
                raise
            raise GitHubPublishError("GitHub publication head branch already exists")
        except GitHubHTTPError:
            raise GitHubPublishError(
                "GitHub publication destination could not be verified"
            ) from None

    async def publish_commit(
        self, destination: PublishDestination, commit: PublishedCommit
    ) -> None:
        destination = _destination(destination)
        metadata, objects = _prepare_commit(commit)
        if commit.parent != destination.base_revision:
            raise GitHubPublishError(
                "Publication commit parent does not match its base revision"
            )
        await self.verify_destination(destination)
        prefix = f"/repos/{destination.repository.full_name}/git"
        try:
            for obj in objects:
                if obj.kind == "blob":
                    data = await self._request(
                        "POST",
                        f"{prefix}/blobs",
                        {
                            "encoding": "base64",
                            "content": base64.b64encode(obj.data).decode("ascii"),
                        },
                    )
                else:
                    data = await self._request(
                        "POST", f"{prefix}/trees", {"tree": _tree_entries(obj.data)}
                    )
                if data.get("sha") != obj.sha:
                    raise GitHubPublishError(
                        "Uploaded Git object revision does not match local evidence"
                    )
            data = await self._request("POST", f"{prefix}/commits", metadata)
            if data.get("sha") != commit.sha:
                raise GitHubPublishError(
                    "Uploaded Git commit revision does not match local evidence"
                )
            ref = f"refs/heads/{destination.head_branch}"
            data = await self._request(
                "POST", f"{prefix}/refs", {"ref": ref, "sha": commit.sha}
            )
            remote_object = data.get("object")
            if (
                data.get("ref") != ref
                or not isinstance(remote_object, dict)
                or remote_object.get("type") != "commit"
                or remote_object.get("sha") != commit.sha
                or not _valid_ref_url(data.get("url"), destination)
            ):
                raise GitHubPublishError(
                    "Published Git branch does not match the requested destination"
                )
        except GitHubHTTPError:
            raise GitHubPublishError("GitHub commit publication failed") from None

    async def create_draft_pr(self, payload: DraftPRPayload) -> DraftPR:
        try:
            payload = DraftPRPayload.model_validate(payload.model_dump())
        except Exception:
            raise GitHubPublishError(
                "Draft PR publication payload is invalid"
            ) from None
        destination = payload.destination
        try:
            await self._verify_repository(destination)
            await self._verify_ref(
                destination, destination.base_branch, destination.base_revision
            )
            await self._verify_ref(
                destination, destination.head_branch, payload.commit_revision
            )
            data = await self._request(
                "POST",
                f"/repos/{destination.repository.full_name}/pulls",
                {
                    "title": payload.title,
                    "body": payload.body,
                    "base": destination.base_branch,
                    "head": destination.head_branch,
                    "draft": True,
                },
            )
        except GitHubHTTPError:
            raise GitHubPublishError("GitHub Draft PR publication failed") from None
        try:
            head, base = data["head"], data["base"]
            if (
                data["draft"] is not True
                or head["sha"] != payload.commit_revision
                or head["ref"] != destination.head_branch
                or base["ref"] != destination.base_branch
                or base["sha"] != destination.base_revision
                or not _matches_repository(
                    head["repo"]["full_name"], destination.repository
                )
                or not _matches_repository(
                    base["repo"]["full_name"], destination.repository
                )
            ):
                raise ValueError("Draft PR identity mismatch")
            result = DraftPR(
                number=data["number"],
                url=data["html_url"],
                head_revision=head["sha"],
                draft=data["draft"],
            )
            _validate_pr_url(result.url, destination.repository, result.number)
            return result
        except Exception:
            raise GitHubPublishError(
                "GitHub Draft PR response does not match publication evidence"
            ) from None
