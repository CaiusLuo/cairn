"""Bounded GitHub REST I/O; credentials are obtained only in this boundary."""

import asyncio
import json
import math
import re
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import unquote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from pydantic import ValidationError

from cairn.github.models import GitHubRepository, IssuePayload, IssueReference
from cairn.github.source import GitHubSourceError

HTTP_TIMEOUT = 10.0
HTTP_OUTPUT_LIMIT = 1024 * 1024


class GitHubHTTPError(RuntimeError):
    """Sanitized transport failure, optionally retaining the HTTP status."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubHTTPClient(Protocol):
    async def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


class GitHubREST:
    """Fixed-host HTTPS with no proxies or redirects.

    The caller explicitly supplies a credential provider. Its value is never
    stored in task models or exposed in errors. Cancellation waits for the owned
    blocking worker to settle before propagating, including repeated cancels.
    """

    def __init__(
        self,
        *,
        token_provider: Callable[[], str | None],
        timeout: float = HTTP_TIMEOUT,
        output_limit: int = HTTP_OUTPUT_LIMIT,
    ) -> None:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
            or isinstance(output_limit, bool)
            or not isinstance(output_limit, int)
            or output_limit <= 0
        ):
            raise ValueError("GitHub HTTP timeout and output limit must be positive")
        self._token_provider = token_provider
        self._timeout = timeout
        self._output_limit = output_limit

    async def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        worker = asyncio.create_task(
            asyncio.to_thread(self._request_sync, method, path, payload)
        )
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError as primary:
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            # Always observe the outcome; never include worker exception content.
            with suppress(BaseException):
                worker.result()
            raise primary

    def _read_response(self, response: Any, deadline: float) -> bytes:
        captured = bytearray()
        reader = getattr(response, "read1", response.read)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GitHubHTTPError("GitHub HTTP request timed out")
            # urllib's HTTPS response wraps a socket in a buffered reader.
            # Tighten each read to the total remaining deadline when available.
            socket = getattr(
                getattr(getattr(response, "fp", None), "raw", None), "_sock", None
            )
            if socket is not None:
                socket.settimeout(remaining)
            chunk = reader(min(64 * 1024, self._output_limit + 1 - len(captured)))
            if time.monotonic() >= deadline:
                raise GitHubHTTPError("GitHub HTTP request timed out")
            if not chunk:
                return bytes(captured)
            captured.extend(chunk)
            if len(captured) > self._output_limit:
                raise GitHubHTTPError("GitHub response exceeded the size limit")

    def _request_sync(
        self, method: str, path: str, payload: dict[str, Any] | None
    ) -> dict[str, Any]:
        if method not in {"GET", "POST", "PATCH", "PUT", "DELETE"}:
            raise GitHubHTTPError("Unsupported GitHub HTTP method")
        try:
            decoded_path = unquote(path, errors="strict")
        except (TypeError, UnicodeError):
            raise GitHubHTTPError("Invalid GitHub API path") from None
        if (
            re.fullmatch(r"/(?:[A-Za-z0-9_./+-]|%[0-9A-Fa-f]{2})+", path) is None
            or any(
                character in ":?#\\%"
                or character.isspace()
                or ord(character) < 32
                or ord(character) == 127
                for character in decoded_path
            )
            or "//" in decoded_path
            or any(segment in {".", ".."} for segment in decoded_path.split("/"))
        ):
            raise GitHubHTTPError("Invalid GitHub API path")
        if method == "GET" and payload is not None:
            raise GitHubHTTPError("GitHub GET requests cannot contain a payload")
        try:
            data = (
                None
                if payload is None
                else json.dumps(payload, allow_nan=False).encode("utf-8")
            )
        except (TypeError, ValueError):
            raise GitHubHTTPError("Invalid GitHub request payload") from None
        if data is not None and len(data) > self._output_limit:
            raise GitHubHTTPError("GitHub request payload exceeded the size limit")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "cairn",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            token = self._token_provider()
        except Exception:
            raise GitHubHTTPError("GitHub credential provider failed") from None
        if token is not None:
            if (
                not isinstance(token, str)
                or not token
                or any(
                    ord(character) < 33 or ord(character) == 127 for character in token
                )
            ):
                raise GitHubHTTPError("GitHub credential is invalid")
            headers["Authorization"] = f"Bearer {token}"
        try:
            request = Request(
                f"https://api.github.com{path}",
                data=data,
                headers=headers,
                method=method,
            )
            opener = build_opener(ProxyHandler({}), _NoRedirect())
            deadline = time.monotonic() + self._timeout
            with opener.open(request, timeout=self._timeout) as response:
                raw = self._read_response(response, deadline)
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise GitHubHTTPError("GitHub API returned an invalid response object")
            return result
        except HTTPError as exc:
            raise GitHubHTTPError(
                f"GitHub API returned HTTP {exc.code}", status_code=exc.code
            ) from None
        except GitHubHTTPError:
            raise
        except Exception:
            raise GitHubHTTPError("GitHub HTTP request failed") from None


class GitHubRESTIssueReader:
    def __init__(self, client: GitHubHTTPClient) -> None:
        self.client = client

    async def fetch_issue(self, reference: IssueReference) -> IssuePayload:
        path = f"/repos/{reference.repository.full_name}/issues/{reference.number}"
        try:
            data = await self.client.request("GET", path)
            repository_url = data["repository_url"]
            prefix = "https://api.github.com/repos/"
            if not isinstance(repository_url, str) or not repository_url.startswith(
                prefix
            ):
                raise ValueError("Invalid repository URL")
            owner, name = repository_url.removeprefix(prefix).split("/")
            repository = GitHubRepository(owner=owner, name=name)
            if "pull_request" in data and not isinstance(data["pull_request"], dict):
                raise ValueError("Invalid pull request metadata")
            return IssuePayload(
                repository=repository,
                number=data["number"],
                title=data["title"],
                body=data["body"],
                state=data["state"],
                pull_request="pull_request" in data,
                url=data["html_url"],
            )
        except GitHubHTTPError as exc:
            message = (
                "GitHub issue was not found"
                if exc.status_code == 404
                else "GitHub issue request failed"
            )
            raise GitHubSourceError(message) from None
        except (KeyError, TypeError, ValueError, ValidationError):
            raise GitHubSourceError("GitHub issue response schema is invalid") from None
        except Exception:
            raise GitHubSourceError("GitHub issue request failed") from None
