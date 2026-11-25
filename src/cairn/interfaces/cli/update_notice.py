"""Queue a WeChat summary of deployed commits through the running Server.

The deploy step runs this after ``scripts/start.sh restart``. It reuses the
existing ``/schedule remind`` command, so the Server's normal at-most-once
delivery and retry handling apply and no new send path exists. Notifications
are best effort: a failed queue keeps the previous commit in the state file so
the next deploy retries the same range.

Run with ``uv run python -m cairn.interfaces.cli.update_notice``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen

DEFAULT_URL = "http://127.0.0.1:8777"
DEFAULT_TOKEN_FILE = Path("/root/.local/state/cairn/runtime/runtime.token")
DEFAULT_STATE_FILE = Path("/root/.local/state/cairn/update-notice.json")
MAX_COMMITS = 5
MAX_SUBJECT = 90


def git(workspace: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(workspace), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def build_notice(sha: str, commits: list[str], shortstat: str) -> str:
    """One line, because /schedule joins its prompt from whitespace."""
    shown = [_clip(row, MAX_SUBJECT) for row in commits[:MAX_COMMITS]]
    if len(commits) > MAX_COMMITS:
        shown.append(f"另有 {len(commits) - MAX_COMMITS} 条")
    parts = [f"Cairn 更新 {sha[:7]}", *shown]
    if shortstat:
        parts.append(shortstat)
    return " · ".join(parts)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def read_last(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    value = data.get("last") if isinstance(data, dict) else None
    return value if isinstance(value, str) and value else None


def write_last(path: Path, sha: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"last": sha}) + "\n")
    temporary.replace(path)


def queue_notice(url: str, token: str, text: str, request_id: str) -> dict[str, object]:
    when = (datetime.now(UTC) + timedelta(seconds=2)).isoformat()
    body = json.dumps(
        {
            "command": "/schedule",
            "args": f"remind once {when} {text}",
            "request_id": request_id,
            "source": "cli",
        }
    ).encode()
    request = Request(
        url.rstrip("/") + "/api/runtime/commands",
        data=body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        result = json.load(response)
    return result if isinstance(result, dict) else {"ok": False}


def notify(
    workspace: Path,
    *,
    head: str,
    base: str | None,
    state_file: Path,
    send: Callable[[str, str], dict[str, object]],
    output: Callable[[str], None] = print,
    dry_run: bool = False,
) -> int:
    sha = git(workspace, "rev-parse", head)
    since = base or read_last(state_file) or f"{sha}^"
    commits = [
        row
        for row in git(
            workspace, "log", "--format=%h %s", f"{since}..{sha}"
        ).splitlines()
        if row
    ]
    if not commits:
        output("没有新的提交, 跳过推送。")
        return 0
    notice = build_notice(
        sha, commits, git(workspace, "diff", "--shortstat", f"{since}..{sha}")
    )
    result = send(notice, f"update-notice-{sha}")
    if not result.get("ok"):
        output(f"推送未受理: {result.get('message', result)}")
        return 1
    if not dry_run:
        write_last(state_file, sha)
    output(str(result.get("message", "已提交微信推送。")))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--since", help="notify commits after this revision")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    token = "" if args.dry_run else args.token_file.read_text().strip()

    def send(text: str, request_id: str) -> dict[str, object]:
        if args.dry_run:
            print(text)
            return {"ok": True, "message": "dry run"}
        return queue_notice(args.url, token, text, request_id)

    return notify(
        args.workspace,
        head=args.head,
        base=args.since,
        state_file=args.state_file,
        send=send,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
