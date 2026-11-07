"""First-use host files for the systemd wrapper; no server or data migration."""

from __future__ import annotations

import argparse
import grp
import json
import os
import pwd
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict
from pathlib import Path
from typing import Any

import tomlkit
from dotenv import dotenv_values

from cairn.infra.config import default_provider, load_project_providers
from cairn.infra.config.admission import expected_approval
from cairn.infra.config.server import parse_server_config

SYSTEM_UNIT_DIRECTORY = Path("/etc/systemd/system")


def prepare(root: Path, config: Path, user: str) -> None:
    """Validate every prospective policy path before creating any host file."""
    account = pwd.getpwnam(user)
    reject_symlinks(config)
    existing = config.exists()
    document = tomlkit.parse(
        (config if existing else root / "deploy/config.example.toml").read_text()
    )
    server: Any = document.get("server", {})
    if not existing:
        require_file_owner(account)
        if server.get("workspace") == "/workspace/cairn":
            server["workspace"] = str(root)
        # Use the selected service account's home, never the caller's HOME/XDG.
        legacy = Path(account.pw_dir) / ".local/state/cairn"
        if (
            server.get("data_root") == "/var/lib/cairn"
            and legacy.is_dir()
            and any(legacy.iterdir())
        ):
            destination = Path("/var/lib/cairn")
            if destination.is_dir() and any(destination.iterdir()):
                raise ValueError(
                    "Both legacy and /var/lib/cairn state exist; set explicit paths in the example before starting"
                )
            server["data_root"] = str(legacy)
            if server.get("worktrees_root") == "/var/lib/cairn/worktrees":
                server["worktrees_root"] = str(legacy / "worktrees")
        if server.get("provider_approval") == "/etc/cairn/provider-approval.json":
            server["provider_approval"] = str(config.parent / "provider-approval.json")
    if not all(server.get(key) for key in ("data_root", "worktrees_root")):
        raise ValueError(
            "Set explicit server.data_root and server.worktrees_root; systemd startup does not use implicit XDG defaults"
        )
    text = tomlkit.dumps(document)
    settings = parse_server_config(text, path=config)
    paths = settings.paths
    paths.protect_policy(config)
    if settings.offline == (settings.provider_approval is not None):
        raise ValueError(
            "Choose explicit offline=true or provider_approval; no fallback model"
        )
    pending = [] if existing else [(config, text)]
    approval = settings.provider_approval
    if approval is not None:
        reject_symlinks(approval)
        paths.protect_policy(approval)
        if approval == config:
            raise ValueError("Config and provider approval must use different files")
        if not approval.exists():
            require_file_owner(account)
            project = load_project_providers(paths.workspace / ".cairn/models.toml")
            provider = default_provider(
                project, os.environ, dotenv_values(paths.workspace / ".env")
            )
            if not provider.config.model_config:
                raise ValueError("Provider must declare at least one model group")
            record = expected_approval(provider, provider.config.model_config[0])
            pending.append((approval, json.dumps(asdict(record), indent=2) + "\n"))
    for directory in (paths.data_root, paths.worktrees):
        if directory.exists() and directory.stat().st_uid != account.pw_uid:
            raise ValueError(
                f"{directory} is not owned by service user {user}; inspect ownership, no files were changed"
            )
    created: list[tuple[Path, os.stat_result]] = []
    try:
        for path, content in pending:
            require_file_owner(account)
            identity = publish_file(path, content)
            created.append((path, identity))
    except (OSError, ValueError):
        for path, identity in reversed(created):
            if (
                path.exists()
                and path.lstat().st_ino == identity.st_ino
                and path.lstat().st_dev == identity.st_dev
            ):
                path.unlink()
        raise
    for path, _ in created:
        print(f"Created {path}")
    print(
        f"Host configuration prepared for systemd User={user}; data_root={paths.data_root}"
    )


def require_file_owner(account: pwd.struct_passwd) -> None:
    if os.geteuid() != 0:
        raise PermissionError(
            "First-time host preparation needs root; use sudo ./scripts/start.sh"
        )
    if account.pw_uid != os.geteuid():
        raise ValueError(
            f"For dedicated service user {account.pw_name}, prepare explicit host config/approval and owned data directories first; refusing root-owned automatic defaults"
        )


def reject_symlinks(path: Path) -> None:
    if not path.is_absolute():
        raise ValueError(f"Host file path must be absolute: {path}")
    if ".." in path.parts:
        raise ValueError(f"Host file path must not contain '..': {path}")
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError(f"Host file path cannot contain symlinks: {path}")


@contextmanager
def parent_directory(path: Path) -> Iterator[int]:
    """Walk without following parent symlinks, retaining the publication dirfd."""
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in path.parent.parts[1:]:
            with suppress(FileExistsError):
                os.mkdir(name, mode=0o700, dir_fd=descriptor)
            child = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def publish_file(path: Path, content: str, mode: int = 0o600) -> os.stat_result:
    """Publish complete bytes exclusively; never replace an existing host file."""
    reject_symlinks(path)
    with parent_directory(path) as directory:
        temporary = f".{path.name}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
            dir_fd=directory,
        )
        try:
            with os.fdopen(descriptor, "w") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
                identity = os.fstat(output.fileno())
            os.link(
                temporary,
                path.name,
                src_dir_fd=directory,
                dst_dir_fd=directory,
                follow_symlinks=False,
            )
            try:
                os.fsync(directory)
            except OSError:
                os.unlink(path.name, dir_fd=directory)
                raise
            return identity
        finally:
            os.unlink(temporary, dir_fd=directory)


def render_unit(root: Path, config: Path, user: str) -> str:
    account = pwd.getpwnam(user)

    def quote(value: Path) -> str:
        return (
            '"'
            + str(value).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
            + '"'
        )

    unit = (root / "deploy/systemd/cairn.service.in").read_text()
    unit = unit.replace(
        "WorkingDirectory=__CAIRN_ROOT__",
        "WorkingDirectory=" + str(root).replace("%", "%%"),
    )
    unit = unit.replace(
        "ExecStart=__CAIRN_ROOT__/.venv/bin/cairn --config __CAIRN_CONFIG__",
        "ExecStart=" + quote(root / ".venv/bin/cairn") + " --config " + quote(config),
    )
    unit = unit.replace(
        "Group=__CAIRN_USER__", "Group=" + grp.getgrgid(account.pw_gid).gr_name
    )
    unit = unit.replace("__CAIRN_ROOT__", str(root).replace("%", "%%"))
    return unit.replace("__CAIRN_USER__", user)


def install_unit(root: Path, config: Path, service: str, user: str) -> None:
    if os.geteuid() != 0:
        raise PermissionError(
            "Installing a system service needs root; use sudo ./scripts/start.sh"
        )
    if (
        not service.startswith("cairn")
        or not service.endswith(".service")
        or any(
            c
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.@-"
            for c in service
        )
    ):
        raise ValueError("Expected a named Cairn .service unit")
    target = SYSTEM_UNIT_DIRECTORY / service
    publish_file(target, render_unit(root, config, user), 0o644)
    print(f"Installed {target} (User={user})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "install-unit"))
    parser.add_argument("root", type=Path)
    parser.add_argument("config", type=Path)
    parser.add_argument("user")
    parser.add_argument("--service", default="cairn.service")
    args = parser.parse_args()
    try:
        if args.action == "prepare":
            prepare(args.root, args.config, args.user)
        else:
            install_unit(args.root, args.config, args.service, args.user)
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        print(f"Host preparation failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
