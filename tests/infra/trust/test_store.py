from __future__ import annotations

import sys
from pathlib import Path

import pytest
from rich.prompt import Confirm

from cairn.infra.trust.store import TrustedWorkspaceStore, TrustStoreError


def test_trusted_workspace_survives_restart_and_revokes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = TrustedWorkspaceStore(tmp_path / "agent")
    assert store.is_trusted(workspace) is False
    store.remember(workspace)
    assert store.is_trusted(workspace) is True
    assert TrustedWorkspaceStore(tmp_path / "agent").is_trusted(workspace) is True
    assert store.revoke(workspace) is True
    assert store.is_trusted(workspace) is False


def test_trusted_workspace_identity_change_requires_reapproval(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = TrustedWorkspaceStore(tmp_path / "agent")
    store.remember(workspace)
    moved = tmp_path / "moved-workspace"
    workspace.rename(moved)
    workspace.symlink_to(moved, target_is_directory=True)
    assert store.is_trusted(workspace) is False


def test_damaged_trust_store_fails_closed(tmp_path: Path) -> None:
    store = TrustedWorkspaceStore(tmp_path / "agent")
    store.root.mkdir(parents=True)
    store.path.write_text("{not json", encoding="utf-8")
    with pytest.raises(TrustStoreError):
        store.is_trusted(tmp_path)


def test_confirm_trust_persists_first_approval_and_reuses_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairn.terminal import output

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CAIRN_AGENT_HOME", str(tmp_path / "agent"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    asks = 0

    def approve(*_args: object, **_kwargs: object) -> bool:
        nonlocal asks
        asks += 1
        return True

    monkeypatch.setattr(Confirm, "ask", approve)
    assert output.confirm_workspace_trust(workspace) is True
    assert asks == 1
    assert output.confirm_workspace_trust(workspace) is True
    assert asks == 1
