from __future__ import annotations

import subprocess
from pathlib import Path

from cairn.interfaces.cli.update_notice import build_notice, notify, read_last


def commit(repository: Path, name: str, message: str) -> str:
    (repository / name).write_text(message)
    subprocess.run(["git", "-C", str(repository), "add", name], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", message],
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_notice_is_one_bounded_line() -> None:
    commits = [f"abc{i:04d} subject {i}" for i in range(7)]
    notice = build_notice(
        "a" * 40, commits, "3 files changed, 5 insertions(+), 2 deletions(-)"
    )
    assert notice.startswith("Cairn 更新 aaaaaaa")
    assert "abc0000 subject 0" in notice
    assert "另有 2 条" in notice
    assert "3 files changed" in notice
    assert "\n" not in notice


def test_notify_advances_only_after_acceptance(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "-C", str(repository), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Test"], check=True
    )
    first = commit(repository, "a.txt", "first change")
    second = commit(repository, "b.txt", "second change")
    state = tmp_path / "state.json"
    sent: list[tuple[str, str]] = []

    def accepted(text: str, request_id: str) -> dict[str, object]:
        sent.append((text, request_id))
        return {"ok": True, "message": "queued"}

    assert (
        notify(repository, head="HEAD", base=first, state_file=state, send=accepted)
        == 0
    )
    assert read_last(state) == second
    assert len(sent) == 1
    assert "second change" in sent[0][0]
    assert sent[0][1] == f"update-notice-{second}"

    def rejected(text: str, request_id: str) -> dict[str, object]:
        return {"ok": False, "message": "no account"}

    other = tmp_path / "other.json"
    assert (
        notify(repository, head="HEAD", base=first, state_file=other, send=rejected)
        == 1
    )
    assert not other.exists()
