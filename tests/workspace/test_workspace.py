from pathlib import Path

import pytest

from cairn.workspace.workspace import Workspace


def test_workspace_normalizes_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "alias").symlink_to(root, target_is_directory=True)
    monkeypatch.chdir(tmp_path)

    workspace = Workspace(Path("alias"))

    assert workspace.root == root.resolve()
    monkeypatch.chdir(root)
    assert workspace.resolve_path("new.txt") == root / "new.txt"


def test_workspace_rejects_missing_directory_without_creating_it(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing" / "nested"

    with pytest.raises(ValueError, match="Workspace does not exist"):
        Workspace(missing)

    assert not missing.parent.exists()


def test_workspace_rejects_file_root(tmp_path: Path) -> None:
    path = tmp_path / "file.txt"
    path.write_text("unchanged", encoding="utf-8")

    with pytest.raises(ValueError, match="Workspace does not exist"):
        Workspace(path)

    assert path.read_text(encoding="utf-8") == "unchanged"


def test_workspace_does_not_manage_directory_lifecycle(tmp_path: Path) -> None:
    root = tmp_path / "owned-by-caller"
    root.mkdir()
    marker = root / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    workspace = Workspace(root)
    workspace.resolve_path("missing/child.txt")
    assert list(root.iterdir()) == [marker]
    del workspace

    assert root.is_dir()
    assert marker.read_text(encoding="utf-8") == "keep"
    assert list(root.iterdir()) == [marker]
