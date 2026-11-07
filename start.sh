#!/bin/bash
# One-time host preparation and systemd supervision; never nohup or a PID file.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
action=${1:-start}
service=${CAIRN_SERVICE:-cairn.service}
config=${CAIRN_CONFIG:-/etc/cairn/config.toml}
launcher="$root/.venv/bin/cairn"
if (( $# > 1 )) || [[ ! "$service" =~ ^cairn[A-Za-z0-9_.@-]*\.service$ ]]; then
    echo "Usage: ./start.sh [start|stop|restart|status|logs]; CAIRN_SERVICE must name a Cairn .service unit" >&2
    exit 2
fi
command -v systemctl >/dev/null || { echo "Debian systemd is required" >&2; exit 1; }
case "$action" in
    stop) exec "$root/end.sh" ;;
    status) exec systemctl status --no-pager "$service" ;;
    logs) exec journalctl --unit="$service" --follow ;;
    start|restart) ;;
    *) echo "Usage: ./start.sh [start|stop|restart|status|logs]" >&2; exit 2 ;;
esac
[[ "$config" = /* ]] || { echo "CAIRN_CONFIG must be absolute" >&2; exit 2; }
[[ -x "$launcher" ]] || { echo "Run uv sync --locked --all-groups in $root first" >&2; exit 1; }
load_state=$(systemctl show "$service" --property=LoadState --value 2>/dev/null || true)
if [[ "$load_state" != "loaded" && "$load_state" != "not-found" ]]; then
    echo "Cannot load $service (LoadState=$load_state); inspect systemd before starting" >&2
    exit 1
fi

# Prepare only missing host files. Existing operator configuration is authoritative.
"$root/.venv/bin/python" - "$root" "$config" "$service" <<'PY_BOOTSTRAP'
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

import tomlkit
from dotenv import dotenv_values

from cairn.infra.config import default_provider, load_project_providers
from cairn.infra.config.admission import expected_approval
from cairn.infra.config.server import load_server_config
from cairn.infra.server_paths import default_data_root


def create(path, text, mode=0o600):
    if path.is_symlink():
        raise ValueError(f"Refusing symlink: {path}")
    if path.exists():
        return False
    if os.geteuid() != 0:
        raise PermissionError("First-time host preparation needs root; use sudo ./start.sh")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "w") as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())
    print(f"Created {path}")
    return True


try:
    root, config, service = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    if config.is_symlink():
        raise ValueError("Server configuration cannot be a symlink")
    if not config.exists():
        template = tomlkit.parse((root / "deploy/config.example.toml").read_text())
        server = template["server"]
        if server.get("workspace") == "/workspace/cairn":
            server["workspace"] = str(root)
        # Materialize the existing defaults, rather than switching an installation
        # into a new, empty state directory when the shell/user changes later.
        server.setdefault("data_root", str(default_data_root()))
        server.setdefault("worktrees_root", str(Path(server["data_root"]) / "worktrees"))
        if server.get("provider_approval") == "/etc/cairn/provider-approval.json":
            server["provider_approval"] = str(config.parent / "provider-approval.json")
        create(config, tomlkit.dumps(template))
    settings = load_server_config(config)
    paths = settings.paths
    paths.protect_policy(config)
    approval = settings.provider_approval
    if not settings.offline:
        if approval is None:
            raise ValueError("Set server.provider_approval in the host configuration")
        paths.protect_policy(approval)
        if not approval.exists():
            project = load_project_providers(paths.workspace / ".cairn/models.toml")
            provider = default_provider(project, os.environ, dotenv_values(paths.workspace / ".env"))
            record = expected_approval(provider, provider.config.model_config[0])
            create(approval, json.dumps(asdict(record), indent=2) + "\n")
except (OSError, ValueError, RuntimeError) as error:
    print(f"Host preparation failed: {error}", file=sys.stderr)
    raise SystemExit(1)
PY_BOOTSTRAP

"$launcher" --config "$config" check-config
if [[ "$load_state" == "not-found" ]]; then
    "$root/.venv/bin/python" - "$root" "$config" "$service" <<'PY_UNIT'
import os
import pwd
import sys
from pathlib import Path

root, config, service = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
if os.geteuid() != 0:
    raise SystemExit("Installing a system service needs root; use sudo ./start.sh")


def quote(value):
    return '"' + str(value).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


user = pwd.getpwuid(os.geteuid()).pw_name
unit = (root / "deploy/systemd/cairn.service.in").read_text()
unit = unit.replace("WorkingDirectory=__CAIRN_ROOT__", "WorkingDirectory=" + quote(root))
unit = unit.replace("ExecStart=__CAIRN_ROOT__/.venv/bin/cairn --config __CAIRN_CONFIG__",
                    "ExecStart=" + quote(root / ".venv/bin/cairn") + " --config " + quote(config))
unit = unit.replace("__CAIRN_ROOT__", str(root).replace("%", "%%"))
unit = unit.replace("__CAIRN_USER__", user)
target = Path("/etc/systemd/system") / service
try:
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
except FileExistsError:
    raise SystemExit(f"Unit file already exists but is not loaded: {target}; inspect it first")
with os.fdopen(fd, "w") as output:
    output.write(unit)
    output.flush()
    os.fsync(output.fileno())
print(f"Installed {target} (User={user})")
PY_UNIT
    systemctl daemon-reload
    systemctl enable "$service"
fi
command_line=$(systemctl show "$service" --property=ExecStart --value)
if [[ "$command_line" != *"$launcher --config $config "* ]]; then
    echo "Unit $service uses another executable/configuration; no service was stopped or replaced" >&2
    exit 1
fi
if systemctl is-active --quiet "$service"; then
    pid=$(systemctl show "$service" --property=MainPID --value)
    "$launcher" --config "$config" check-running --pid "$pid"
fi
systemctl "$action" "$service"
deadline=$((SECONDS + 30))
while (( SECONDS < deadline )); do
    pid=$(systemctl show "$service" --property=MainPID --value)
    state=$(systemctl show "$service" --property=ActiveState --value)
    if [[ "$state" == "active" && "$pid" =~ ^[1-9][0-9]*$ ]] && \
       "$launcher" --config "$config" check-running --pid "$pid" >/dev/null 2>&1; then
        echo "Cairn is running in the background: service=$service PID=$pid"
        echo "Logs: ./start.sh logs | Stop: ./end.sh"
        exit 0
    fi
    [[ "$state" != "failed" && "$state" != "inactive" ]] || break
    sleep 1
done
echo "Server did not become ready; service=$service PID=${pid:-0}. Inspect: journalctl -u $service -n 50" >&2
systemctl status --no-pager "$service" >&2 || true
exit 1
