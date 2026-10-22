"""Host capability checks for descriptor-relative, symlink-safe file access.

Two properties matter, and they pull in opposite directions:

* importing Cairn must never fail because a POSIX flag is absent — ``cairn
  --help`` has to work everywhere;
* an operation that depends on such a capability must refuse *before* any
  filesystem effect, rather than running with the protection switched off.

The simulation here deletes the flags and the ``dir_fd``/``follow_symlinks``
support from ``os`` and then imports the package, so the import stage itself is
covered. A monkeypatch applied after import would not prove that.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from cairn.infra import platform


def _simulate_missing_host_flags() -> str:
    """A script that removes the capabilities before importing Cairn."""
    return """
import os
for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC", "O_NONBLOCK"):
    if hasattr(os, name):
        delattr(os, name)
os.supports_dir_fd = set()
os.supports_follow_symlinks = set()

import cairn.terminal.cli as cli
assert hasattr(cli, "app"), "CLI entry point missing"

from cairn.infra import platform
for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC"):
    try:
        platform.flag(name)
    except platform.CapabilityMissing:
        pass
    else:
        raise SystemExit(f"{name} did not refuse")
for check in (platform.require_dir_fd, platform.require_no_follow_stat):
    try:
        check()
    except platform.CapabilityMissing:
        pass
    else:
        raise SystemExit(f"{check.__name__} did not refuse")
print("SIMULATED-OK")
"""


def test_import_survives_a_host_without_posix_flags() -> None:
    """``cairn --help`` must import even when the flags do not exist."""
    project_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-c", _simulate_missing_host_flags()],
        cwd=project_root,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(project_root / "src")},
    )
    assert result.returncode == 0, result.stderr
    assert "SIMULATED-OK" in result.stdout


def test_a_missing_flag_refuses_instead_of_degrading() -> None:
    """The operation refuses; it never substitutes 0 for the flag."""
    name = "O_NOFOLLOW"
    original = getattr(os, name, None)
    try:
        if original is not None:
            delattr(os, name)
        with pytest.raises(platform.CapabilityMissing, match=name):
            platform.flag(name)
        with pytest.raises(platform.CapabilityMissing, match=name):
            platform.open_flags(name)
    finally:
        if original is not None:
            setattr(os, name, original)


def test_a_present_flag_is_returned_unchanged() -> None:
    assert platform.flag("O_RDONLY") == os.O_RDONLY
    combined = platform.open_flags("O_DIRECTORY")
    assert combined == os.O_RDONLY | os.O_DIRECTORY


def test_missing_dir_fd_support_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "_HOST_DIR_FD", False)
    with pytest.raises(platform.CapabilityMissing, match="dir_fd"):
        platform.require_dir_fd()


def test_missing_no_follow_stat_support_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "_HOST_NO_FOLLOW_STAT", False)
    with pytest.raises(platform.CapabilityMissing, match="follow_symlinks"):
        platform.require_no_follow_stat()


def test_open_root_refuses_before_opening_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No descriptor is opened when the capability is missing."""
    from cairn.infra.descriptors import open_root

    opened: list[object] = []

    def record(*args: object, **kwargs: object) -> int:
        opened.append(args)
        raise AssertionError("os.open must not be reached")

    monkeypatch.setattr(platform, "require_dir_fd", _raise_missing)
    monkeypatch.setattr(os, "open", record)
    with pytest.raises(platform.CapabilityMissing):
        open_root(tmp_path)
    assert opened == []


def _raise_missing() -> None:
    raise platform.CapabilityMissing("simulated")


def test_symlink_protection_still_holds_on_this_host(tmp_path: Path) -> None:
    """The protection the capability exists for is still enforced."""
    from cairn.infra.descriptors import open_root

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    # A symlinked component is refused rather than followed.
    with pytest.raises(OSError):
        open_root(link)
