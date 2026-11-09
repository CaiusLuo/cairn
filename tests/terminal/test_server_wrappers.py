"""Wrapper behavior with a fake systemctl; never stop a real host unit."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("state", ["active", "inactive", "missing", "broken"])
def test_end_uses_named_unit_without_loading_configuration(
    tmp_path: Path, state: str
) -> None:
    if shutil.which("timeout") is None:
        pytest.skip("requires Debian coreutils")
    commands = tmp_path / "bin"
    commands.mkdir()
    (tmp_path / "state").write_text(state)
    systemctl = commands / "systemctl"
    systemctl.write_text("""#!/bin/bash
set -eu
printf '%s\\n' "$*" >> "$WRAPPER_FIXTURE/calls"
state=$(cat "$WRAPPER_FIXTURE/state")
case "$*" in
    *LoadState*)
        case "$state" in
            missing) echo not-found; exit 1 ;;
            broken) echo error ;;
            *) echo loaded ;;
        esac ;;
    *MainPID*) [[ "$state" == active ]] && echo 4321 || echo 0 ;;
    *ActiveState*) [[ "$state" == active ]] && echo active || echo inactive ;;
    "stop --no-block cairn-isolated.service") echo inactive > "$WRAPPER_FIXTURE/state" ;;
    *) exit 2 ;;
esac
""")
    systemctl.chmod(0o755)
    result = subprocess.run(
        ["bash", str(Path("scripts/end.sh").resolve())],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": str(commands) + os.pathsep + os.environ["PATH"],
            "WRAPPER_FIXTURE": str(tmp_path),
            "CAIRN_SERVICE": "cairn-isolated.service",
            "CAIRN_CONFIG": str(tmp_path / "broken-or-missing.toml"),
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    calls = (tmp_path / "calls").read_text()
    assert "kill" not in calls
    if state == "missing":
        assert "does not exist" in result.stdout
        assert "stop" not in calls
    else:
        assert "stop --no-block cairn-isolated.service" in calls
        assert "PID=0" in result.stdout
    if state == "active":
        assert "PID=4321" in result.stdout
        assert "graceful shutdown" in result.stdout


def test_end_reports_unresponsive_systemd_without_an_unbounded_wait(
    tmp_path: Path,
) -> None:
    (tmp_path / "systemctl").write_text("#!/bin/sh\nexit 2\n")
    (tmp_path / "systemctl").chmod(0o755)
    timeout = tmp_path / "timeout"
    timeout.write_text("#!/bin/sh\nexit 124\n")
    timeout.chmod(0o755)
    result = subprocess.run(
        ["bash", str(Path("scripts/end.sh").resolve())],
        env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode != 0
    assert "Cannot query systemd" in result.stderr


def wrapper_environment() -> dict[str, str]:
    return {
        key: value for key, value in os.environ.items() if not key.startswith("CAIRN_")
    }


@pytest.mark.parametrize(
    "variable",
    [
        "CAIRN_DATA_ROOT",
        "CAIRN_WORKSPACE",
        "CAIRN_PROVIDER_APPROVAL",
        "CAIRN_LLM_API_KEY",
    ],
)
def test_start_rejects_shell_overrides_before_any_host_preparation(
    tmp_path: Path, variable: str
) -> None:
    script = tmp_path / "start.sh"
    shutil.copyfile("scripts/start.sh", script)
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(
        '#!/bin/sh\ntouch "$WRAPPER_FIXTURE/systemctl-called"\nexit 1\n'
    )
    systemctl.chmod(0o755)
    secret = "do-not-print-this-override-value"
    result = subprocess.run(
        ["bash", str(script)],
        env={
            **wrapper_environment(),
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "WRAPPER_FIXTURE": str(tmp_path),
            "CAIRN_CONFIG": str(tmp_path / "host/config.toml"),
            "CAIRN_SERVICE": "cairn-isolated.service",
            variable: secret,
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 2
    assert "Do not use CAIRN_* overrides with start.sh" in result.stderr
    assert variable in result.stderr
    assert secret not in result.stdout + result.stderr
    assert not (tmp_path / "systemctl-called").exists()
    assert not (tmp_path / "host").exists()


def test_start_first_use_prepares_and_starts_selected_unit_with_fake_systemctl(
    tmp_path: Path,
) -> None:
    if shutil.which("timeout") is None:
        pytest.skip("requires Debian coreutils")
    root = tmp_path / "checkout with spaces"
    commands = tmp_path / "bin"
    executables = root / ".venv/bin"
    executables.mkdir(parents=True)
    (root / "scripts").mkdir()
    commands.mkdir()
    shutil.copyfile("scripts/start.sh", root / "scripts/start.sh")
    config = tmp_path / "host with spaces/config.toml"
    stubs = {
        executables / "python": """#!/bin/bash
set -eu
[[ "$1" == -m && "$2" == cairn.server.host && "$5" == "$CAIRN_CONFIG" ]]
case "$3" in
    prepare)
        [[ ! -e "$5" ]]
        mkdir -p -- "$(dirname -- "$5")"
        printf '%s\\n' '[server]' > "$5"
        echo prepare >> "$WRAPPER_FIXTURE/events" ;;
    install-unit)
        [[ "$7" == --service && "$8" == "$CAIRN_SERVICE" ]]
        touch "$WRAPPER_FIXTURE/unit-installed"
        echo install-unit >> "$WRAPPER_FIXTURE/events" ;;
    *) exit 2 ;;
esac
""",
        executables / "cairn": """#!/bin/bash
set -eu
[[ "$1" == --config && "$2" == "$CAIRN_CONFIG" && -s "$2" ]]
case "$3" in
    check-config) echo check-config >> "$WRAPPER_FIXTURE/events" ;;
    check-running)
        [[ "$4" == --pid && "$5" == 4321 && "$6" == --wait && "$7" == 30 ]]
        [[ -f "$WRAPPER_FIXTURE/running" ]]
        echo check-running >> "$WRAPPER_FIXTURE/events" ;;
    *) exit 2 ;;
esac
""",
        commands / "systemctl": """#!/bin/bash
set -eu
case "$*" in
    "show cairn-isolated.service --property=LoadState --value") echo not-found; exit 1 ;;
    "show cairn-isolated.service --property=ActiveState --value")
        if [[ -f "$WRAPPER_FIXTURE/running" ]]; then echo active; else echo inactive; fi ;;
    "show cairn-isolated.service --property=MainPID --value")
        if [[ -f "$WRAPPER_FIXTURE/running" ]]; then echo 4321; else echo 0; fi ;;
    daemon-reload) echo daemon-reload >> "$WRAPPER_FIXTURE/events" ;;
    "enable cairn-isolated.service")
        [[ -f "$WRAPPER_FIXTURE/unit-installed" ]]
        echo enable >> "$WRAPPER_FIXTURE/events" ;;
    "start cairn-isolated.service")
        [[ -s "$CAIRN_CONFIG" && -f "$WRAPPER_FIXTURE/unit-installed" ]]
        touch "$WRAPPER_FIXTURE/running"
        echo start >> "$WRAPPER_FIXTURE/events" ;;
    *) echo "Unexpected fake systemctl arguments: $*" >&2; exit 2 ;;
esac
""",
    }
    for path, text in stubs.items():
        path.write_text(text)
        path.chmod(0o755)
    result = subprocess.run(
        ["bash", str(root / "scripts/start.sh")],
        cwd=tmp_path,
        env={
            **wrapper_environment(),
            "PATH": str(commands) + os.pathsep + os.environ["PATH"],
            "WRAPPER_FIXTURE": str(tmp_path),
            "CAIRN_CONFIG": str(config),
            "CAIRN_SERVICE": "cairn-isolated.service",
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "service=cairn-isolated.service PID=4321" in result.stdout
    assert config.is_file()
    assert (tmp_path / "events").read_text().splitlines() == [
        "prepare",
        "check-config",
        "install-unit",
        "daemon-reload",
        "enable",
        "start",
        "check-running",
    ]
