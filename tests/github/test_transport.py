import asyncio
import base64
import os
import subprocess
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import unquote

import pytest
from pydantic import ValidationError

from cairn.github.http import GitHubHTTPError
from cairn.github.models import GitHubRepository
from cairn.github.transport import (
    MAX_OBJECT_BYTES,
    DraftPRPayload,
    GitHubPublishError,
    GitHubRESTPublisher,
    GitObject,
    PublishDestination,
    PublishedCommit,
)

DATE = "2026-10-10T00:00:00Z"
GIT_ENV = {
    "PATH": os.defpath,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "Cairn Test",
    "GIT_AUTHOR_EMAIL": "cairn@example.invalid",
    "GIT_COMMITTER_NAME": "Cairn Test",
    "GIT_COMMITTER_EMAIL": "cairn@example.invalid",
    "GIT_AUTHOR_DATE": DATE,
    "GIT_COMMITTER_DATE": DATE,
}


def git(root: Path, *args: str, data: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), "-c", f"core.hooksPath={os.devnull}", *args],
        input=data,
        capture_output=True,
        env=GIT_ENV,
        check=True,
    ).stdout


def hash_object(root: Path, kind: str, data: bytes) -> str:
    return (
        git(root, "hash-object", "-w", "-t", kind, "--stdin", data=data)
        .decode()
        .strip()
    )


def raw_commit(commit: PublishedCommit) -> bytes:
    timestamp = int(
        datetime.fromisoformat(commit.date.replace("Z", "+00:00")).timestamp()
    )
    identity = f"{commit.author_name} <{commit.author_email}>"
    return (
        f"tree {commit.tree}\nparent {commit.parent}\n"
        f"author {identity} {timestamp} +0000\n"
        f"committer {identity} {timestamp} +0000\n\n{commit.message}"
    ).encode()


class FakeGitHubAPI:
    """Fake HTTP endpoints backed by actual Git SHA-1 object storage."""

    def __init__(self, root: Path, destination: PublishDestination) -> None:
        self.root = root
        self.destination = destination
        self.repository = destination.repository.full_name
        self.fork: Any = False
        self.refs = {destination.base_branch: destination.base_revision}
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.head_error = 404
        self.conflict_at_creation = False
        self.response_override: dict[str, dict[str, Any]] = {}
        self.failure: BaseException | None = None

    async def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append((method, path, payload))
        if self.failure is not None:
            raise self.failure
        prefix = f"/repos/{self.destination.repository.full_name}"
        assert path.startswith(prefix)
        endpoint = path.removeprefix(prefix)
        if method == "GET":
            assert payload is None
            if endpoint == "":
                return {"full_name": self.repository, "fork": self.fork}
            assert endpoint.startswith("/git/ref/heads/")
            branch = unquote(endpoint.removeprefix("/git/ref/heads/"))
            if branch not in self.refs:
                raise GitHubHTTPError(
                    "remote secret payload", status_code=self.head_error
                )
            return {
                "ref": f"refs/heads/{branch}",
                "object": {"type": "commit", "sha": self.refs[branch]},
            }
        assert method == "POST" and payload is not None
        assert "force" not in payload
        if endpoint == "/git/blobs":
            assert payload["encoding"] == "base64"
            sha = hash_object(
                self.root, "blob", base64.b64decode(payload["content"], validate=True)
            )
            result: dict[str, Any] = {"sha": sha}
        elif endpoint == "/git/trees":
            assert "base_tree" not in payload
            for entry in payload["tree"]:
                assert (
                    git(self.root, "cat-file", "-t", entry["sha"]).decode().strip()
                    == entry["type"]
                )
            entries = sorted(
                payload["tree"],
                key=lambda item: (
                    item["path"].encode() + (b"/" if item["type"] == "tree" else b"\0")
                ),
            )
            raw = b"".join(
                entry["mode"].lstrip("0").encode()
                + b" "
                + entry["path"].encode()
                + b"\0"
                + bytes.fromhex(entry["sha"])
                for entry in entries
            )
            sha = hash_object(self.root, "tree", raw)
            result = {"sha": sha}
        elif endpoint == "/git/commits":
            assert payload["author"] == payload["committer"]
            assert git(self.root, "cat-file", "-t", payload["tree"]).strip() == b"tree"
            assert (
                git(self.root, "cat-file", "-t", payload["parents"][0]).strip()
                == b"commit"
            )
            commit = PublishedCommit(
                sha="0" * 40,
                tree=payload["tree"],
                parent=payload["parents"][0],
                message=payload["message"],
                author_name=payload["author"]["name"],
                author_email=payload["author"]["email"],
                date=payload["author"]["date"],
                objects=(),
            )
            sha = hash_object(self.root, "commit", raw_commit(commit))
            result = {"sha": sha}
        elif endpoint == "/git/refs":
            branch = payload["ref"].removeprefix("refs/heads/")
            if self.conflict_at_creation:
                self.refs[branch] = "f" * 40
            if branch in self.refs:
                raise GitHubHTTPError("remote secret conflict", status_code=422)
            self.refs[branch] = payload["sha"]
            result = {
                "ref": payload["ref"],
                "object": {"type": "commit", "sha": payload["sha"]},
                "url": f"https://api.github.com{prefix}/git/refs/heads/{branch}",
            }
        elif endpoint == "/pulls":
            assert payload["draft"] is True
            result = {
                "number": 7,
                "html_url": f"https://github.com{prefix.removeprefix('/repos')}/pull/7",
                "draft": True,
                "head": {
                    "sha": self.refs[payload["head"]],
                    "ref": payload["head"],
                    "repo": {"full_name": self.repository},
                },
                "base": {
                    "sha": self.refs[payload["base"]],
                    "ref": payload["base"],
                    "repo": {"full_name": self.repository},
                },
            }
        else:
            pytest.fail(f"Unexpected mutation endpoint {endpoint}")
        result.update(self.response_override.get(endpoint, {}))
        return result


@pytest.fixture
def publication(
    tmp_path: Path,
) -> tuple[PublishedCommit, PublishDestination, FakeGitHubAPI]:
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    (source / "kept.txt").write_text("unchanged\n")
    (source / "deleted.txt").write_text("delete me\n")
    git(source, "add", "--", "kept.txt", "deleted.txt")
    git(source, "commit", "-m", "base")
    parent = git(source, "rev-parse", "HEAD").decode().strip()
    (source / "deleted.txt").unlink()
    nested = source / "nested"
    nested.mkdir()
    (nested / "binary.bin").write_bytes(b"\0\xff\xfe\x80binary\0data")
    (nested / "script.sh").write_text("#!/bin/sh\nexit 0\n")
    (nested / "script.sh").chmod(0o755)
    (source / "link").symlink_to("kept.txt")
    git(source, "add", "--all")
    git(source, "commit", "-m", "verified change")
    sha = git(source, "rev-parse", "HEAD").decode().strip()
    tree = git(source, "rev-parse", "HEAD^{tree}").decode().strip()
    objects = []
    for line in git(source, "rev-list", "--objects", sha, f"^{parent}").splitlines():
        revision = line.split(b" ", 1)[0].decode()
        kind = git(source, "cat-file", "-t", revision).decode().strip()
        if kind in {"tree", "blob"}:
            objects.append(
                GitObject(
                    revision,
                    cast(Literal["tree", "blob"], kind),
                    git(source, "cat-file", kind, revision),
                )
            )
    commit = PublishedCommit(
        sha,
        tree,
        parent,
        "verified change\n",
        "Cairn Test",
        "cairn@example.invalid",
        DATE,
        tuple(objects),
    )
    assert raw_commit(commit) == git(source, "cat-file", "commit", sha)
    destination = PublishDestination(
        repository=GitHubRepository(owner="Owner", name="Repo"),
        base_branch="main",
        head_branch="codex/issue-20",
        base_revision=parent,
    )
    server = tmp_path / "server.git"
    server.mkdir()
    git(server, "init", "--bare")
    pack = git(
        source, "pack-objects", "--stdout", "--revs", data=f"{parent}\n".encode()
    )
    git(server, "index-pack", "--stdin", data=pack)
    return commit, destination, FakeGitHubAPI(server, destination)


def pr_payload(
    commit: PublishedCommit, destination: PublishDestination
) -> DraftPRPayload:
    return DraftPRPayload(
        destination=destination,
        commit_revision=commit.sha,
        title="Fix issue #20",
        body="Verified local checks passed.",
    )


def test_publication_reconstructs_exact_nested_binary_modes_and_deleted_tree(
    publication: tuple[PublishedCommit, PublishDestination, FakeGitHubAPI],
) -> None:
    commit, destination, api = publication
    transport = GitHubRESTPublisher(api)

    async def scenario() -> None:
        await transport.verify_destination(destination)
        await transport.publish_commit(destination, commit)
        result = await transport.create_draft_pr(pr_payload(commit, destination))
        assert result.number == 7 and result.draft is True
        assert result.head_revision == commit.sha
        assert result.url == "https://github.com/Owner/Repo/pull/7"

    asyncio.run(scenario())
    assert git(api.root, "cat-file", "commit", commit.sha) == raw_commit(commit)
    tree = git(api.root, "ls-tree", "-r", commit.tree)
    assert b"deleted.txt" not in tree
    assert b"100755 blob" in tree and b"120000 blob" in tree
    assert b"nested/binary.bin" in tree and b"kept.txt" in tree
    posts = [item for item in api.calls if item[0] == "POST"]
    assert len([item for item in posts if item[1].endswith("/refs")]) == 1
    assert len([item for item in posts if item[1].endswith("/pulls")]) == 1


@pytest.mark.parametrize("failure", ["repo", "base", "head", "head-http"])
def test_destination_mismatch_never_mutates(
    publication: tuple[PublishedCommit, PublishDestination, FakeGitHubAPI], failure: str
) -> None:
    commit, destination, api = publication
    if failure == "repo":
        api.repository = "Elsewhere/Repo"
    elif failure == "base":
        api.refs[destination.base_branch] = "b" * 40
    elif failure == "head":
        api.refs[destination.head_branch] = commit.sha
    else:
        api.head_error = 500
    with pytest.raises(GitHubPublishError):
        asyncio.run(GitHubRESTPublisher(api).publish_commit(destination, commit))
    assert not any(method == "POST" for method, _, _ in api.calls)


def test_explicit_matching_fork_and_canonical_repository_case_are_supported(
    publication: tuple[PublishedCommit, PublishDestination, FakeGitHubAPI],
) -> None:
    commit, destination, api = publication
    api.fork = True
    api.repository = "owner/repo"
    api.response_override["/git/refs"] = {
        "url": "https://api.github.com/repos/owner/repo/git/refs/heads/codex%2Fissue-20"
    }
    transport = GitHubRESTPublisher(api)
    asyncio.run(transport.publish_commit(destination, commit))
    assert api.refs[destination.head_branch] == commit.sha
    result = asyncio.run(transport.create_draft_pr(pr_payload(commit, destination)))
    assert result.head_revision == commit.sha and result.draft is True


@pytest.mark.parametrize("endpoint", ["blobs", "trees", "commits"])
def test_uploaded_sha_mismatch_stops_without_ref_or_retry(
    publication: tuple[PublishedCommit, PublishDestination, FakeGitHubAPI],
    endpoint: str,
) -> None:
    commit, destination, api = publication
    api.response_override[f"/git/{endpoint}"] = {"sha": "c" * 40}
    with pytest.raises(GitHubPublishError, match="revision"):
        asyncio.run(GitHubRESTPublisher(api).publish_commit(destination, commit))
    assert destination.head_branch not in api.refs
    assert (
        len(
            [
                path
                for method, path, _ in api.calls
                if method == "POST" and path.endswith(f"/{endpoint}")
            ]
        )
        == 1
    )


@pytest.mark.parametrize(
    "bad",
    [
        "sha256",
        "commit-sha",
        "parent",
        "object-sha",
        "object-limit",
        "tree-utf8",
        "gitlink",
        "date",
        "message",
    ],
)
def test_invalid_local_commit_rejected_before_http(
    publication: tuple[PublishedCommit, PublishDestination, FakeGitHubAPI], bad: str
) -> None:
    commit, destination, api = publication
    if bad == "sha256":
        commit = replace(commit, sha="a" * 64)
    elif bad == "commit-sha":
        commit = replace(commit, sha="a" * 40)
    elif bad == "parent":
        commit = replace(commit, parent="b" * 40)
        commit = replace(
            commit, sha=hash_object(api.root, "commit", raw_commit(commit))
        )
    elif bad in {"object-sha", "object-limit"}:
        obj = commit.objects[0]
        obj = (
            replace(obj, sha="c" * 40)
            if bad == "object-sha"
            else replace(obj, data=b"x" * (MAX_OBJECT_BYTES + 1))
        )
        commit = replace(commit, objects=(obj, *commit.objects[1:]))
    elif bad in {"tree-utf8", "gitlink"}:
        raw = (
            b"100644 \xff\0" if bad == "tree-utf8" else b"160000 module\0"
        ) + bytes.fromhex(commit.parent)
        tree_sha = hash_object(api.root, "tree", raw)
        commit = replace(
            commit, tree=tree_sha, objects=(GitObject(tree_sha, "tree", raw),)
        )
        commit = replace(
            commit, sha=hash_object(api.root, "commit", raw_commit(commit))
        )
    elif bad == "date":
        commit = replace(commit, date="2026-10-10T08:00:00+08:00")
    else:
        commit = replace(commit, message="missing trailing newline")
    with pytest.raises(GitHubPublishError):
        asyncio.run(GitHubRESTPublisher(api).publish_commit(destination, commit))
    assert not api.calls


@pytest.mark.parametrize(
    "branch",
    [
        "main",
        "../escape",
        "branch.lock",
        "has space",
        "@{-1}",
        "bad?query",
        "-flag",
        "ambiguous%2Fbranch",
    ],
)
def test_invalid_destination_branch_rejected(branch: str) -> None:
    with pytest.raises(ValidationError):
        PublishDestination(
            repository=GitHubRepository(owner="Owner", name="Repo"),
            base_branch="main",
            head_branch=branch,
            base_revision="a" * 40,
        )


@pytest.mark.parametrize("field", ["base", "head"])
def test_pr_preflight_revision_drift_never_posts(
    publication: tuple[PublishedCommit, PublishDestination, FakeGitHubAPI], field: str
) -> None:
    commit, destination, api = publication
    api.refs[destination.head_branch] = commit.sha
    api.refs[
        destination.base_branch if field == "base" else destination.head_branch
    ] = "d" * 40
    with pytest.raises(GitHubPublishError, match="revision"):
        asyncio.run(
            GitHubRESTPublisher(api).create_draft_pr(pr_payload(commit, destination))
        )
    assert not any(method == "POST" for method, _, _ in api.calls)


@pytest.mark.parametrize(
    "bad",
    [
        "nondraft",
        "truthy-draft",
        "sha",
        "repo",
        "base",
        "url-host",
        "url-query",
        "url-credentials",
        "number",
    ],
)
def test_pr_response_identity_validation(
    publication: tuple[PublishedCommit, PublishDestination, FakeGitHubAPI], bad: str
) -> None:
    commit, destination, api = publication
    api.refs[destination.head_branch] = commit.sha
    override: dict[str, Any] = {}
    if bad in {"nondraft", "truthy-draft"}:
        override["draft"] = False if bad == "nondraft" else 1
    elif bad in {"sha", "repo"}:
        override["head"] = {
            "sha": "e" * 40 if bad == "sha" else commit.sha,
            "ref": destination.head_branch,
            "repo": {
                "full_name": "Elsewhere/Repo"
                if bad == "repo"
                else destination.repository.full_name
            },
        }
    elif bad == "base":
        override["base"] = {
            "sha": "f" * 40,
            "ref": destination.base_branch,
            "repo": {"full_name": destination.repository.full_name},
        }
    elif bad.startswith("url"):
        override["html_url"] = {
            "url-host": "https://evil.invalid/Owner/Repo/pull/7",
            "url-query": "https://github.com/Owner/Repo/pull/7?token=secret",
            "url-credentials": "https://secret@github.com/Owner/Repo/pull/7",
        }[bad]
    else:
        override["number"] = True
    api.response_override["/pulls"] = override
    with pytest.raises(GitHubPublishError, match="response"):
        asyncio.run(
            GitHubRESTPublisher(api).create_draft_pr(pr_payload(commit, destination))
        )
    assert len([path for method, path, _ in api.calls if method == "POST"]) == 1


def test_ref_response_must_confirm_explicit_repository(
    publication: tuple[PublishedCommit, PublishDestination, FakeGitHubAPI],
) -> None:
    commit, destination, api = publication
    api.response_override["/git/refs"] = {
        "url": "https://api.github.com/repos/Other/Repo/git/refs/heads/codex/issue-20"
    }
    with pytest.raises(GitHubPublishError, match="destination"):
        asyncio.run(GitHubRESTPublisher(api).publish_commit(destination, commit))
    assert (
        len(
            [
                path
                for method, path, _ in api.calls
                if method == "POST" and path.endswith("/refs")
            ]
        )
        == 1
    )


def test_concurrent_head_creation_never_updates_existing_ref_or_retries(
    publication: tuple[PublishedCommit, PublishDestination, FakeGitHubAPI],
) -> None:
    commit, destination, api = publication
    api.conflict_at_creation = True
    with pytest.raises(GitHubPublishError, match="publication failed"):
        asyncio.run(GitHubRESTPublisher(api).publish_commit(destination, commit))
    assert api.refs[destination.head_branch] == "f" * 40
    assert (
        len(
            [
                path
                for method, path, _ in api.calls
                if method == "POST" and path.endswith("/refs")
            ]
        )
        == 1
    )
    assert all(method in {"GET", "POST"} for method, _, _ in api.calls)


@pytest.mark.parametrize(
    "failure",
    [
        ValueError("credential-secret"),
        GitHubHTTPError("credential-secret", status_code=403),
    ],
)
def test_transport_errors_never_echo_remote_payload(
    publication: tuple[PublishedCommit, PublishDestination, FakeGitHubAPI],
    failure: BaseException,
) -> None:
    commit, destination, api = publication
    api.failure = failure
    with pytest.raises(GitHubPublishError) as error:
        asyncio.run(GitHubRESTPublisher(api).publish_commit(destination, commit))
    assert "credential-secret" not in str(error.value)
    assert len(api.calls) == 1
