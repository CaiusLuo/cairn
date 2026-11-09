#!/bin/bash
# Prepare missing host files, then let systemd supervise the Server.
set -euo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root=$(cd -- "$script_dir/.." && pwd)
action=${1:-start}
service=${CAIRN_SERVICE:-cairn.service}
config=${CAIRN_CONFIG:-/etc/cairn/config.toml}
launcher="$root/.venv/bin/cairn"
query_timeout=10
operation_timeout=130
readiness_timeout=30
if (( $# > 1 )) || [[ ! "$service" =~ ^cairn[A-Za-z0-9_.@-]*\.service$ ]]; then
    echo "Usage: ./scripts/start.sh [start|stop|restart|status|logs]; CAIRN_SERVICE must name a Cairn .service unit" >&2
    exit 2
fi
# systemd does not inherit this shell's configuration overrides.
for variable in "${!CAIRN_@}"; do
    case "$variable" in
        CAIRN_CONFIG|CAIRN_SERVICE) ;;
        *)
            echo "Do not use CAIRN_* overrides with start.sh; put server settings in $config (found $variable)." >&2
            echo "Only CAIRN_CONFIG and CAIRN_SERVICE are supported. Keep secrets in private .env or the service EnvironmentFile." >&2
            exit 2
            ;;
    esac
done
for command in systemctl timeout; do
    command -v "$command" >/dev/null || { echo "Debian systemd and coreutils are required" >&2; exit 1; }
done
ctl() { timeout --foreground "${query_timeout}s" systemctl "$@"; }
case "$action" in
    stop) exec "$script_dir/end.sh" ;;
    status) ctl status --no-pager "$service"; exit ;;
    logs) exec journalctl --unit="$service" --follow ;;
    start|restart) ;;
    *) echo "Usage: ./scripts/start.sh [start|stop|restart|status|logs]" >&2; exit 2 ;;
esac
[[ "$config" = /* ]] || { echo "CAIRN_CONFIG must be absolute" >&2; exit 2; }
[[ -x "$launcher" ]] || { echo "Run uv sync --locked --all-groups in $root first" >&2; exit 1; }
if ! load_state=$(ctl show "$service" --property=LoadState --value); then
    [[ "$load_state" == "not-found" ]] || { echo "Cannot query systemd for $service within ${query_timeout}s" >&2; exit 1; }
fi
case "$load_state" in
    loaded)
        command_line=$(ctl show "$service" --property=ExecStart --value)
        if [[ "$command_line" != *"$launcher --config $config "* ]]; then
            echo "Unit $service uses another executable/configuration; no service was stopped or replaced" >&2
            exit 1
        fi
        service_user=$(ctl show "$service" --property=User --value)
        service_user=${service_user:-root}
        ;;
    not-found) service_user=$(id -un) ;;
    *) echo "Cannot load $service (LoadState=$load_state); inspect systemd before starting" >&2; exit 1 ;;
esac
# Resolve defaults against the unit account, not the shell that invoked sudo.
echo "Preparing $service for systemd User=$service_user"
"$root/.venv/bin/python" -m cairn.server.host prepare "$root" "$config" "$service_user"
if [[ "$service_user" == "$(id -un)" ]]; then
    "$launcher" --config "$config" check-config
else
    runuser -u "$service_user" -- "$launcher" --config "$config" check-config
fi
if [[ "$load_state" == "not-found" ]]; then
    "$root/.venv/bin/python" -m cairn.server.host install-unit "$root" "$config" "$service_user" --service "$service"
    ctl daemon-reload
    ctl enable "$service"
fi
state=$(ctl show "$service" --property=ActiveState --value)
if [[ "$state" != "inactive" && "$state" != "failed" ]]; then
    pid=$(ctl show "$service" --property=MainPID --value)
    "$launcher" --config "$config" check-running --pid "$pid"
fi
echo "Waiting for $service to $action through systemd..."
if ! timeout --foreground "${operation_timeout}s" systemctl "$action" "$service"; then
    echo "systemd $action failed or exceeded ${operation_timeout}s; inspect $service. The systemd job may still be pending." >&2
    exit 1
fi
pid=$(ctl show "$service" --property=MainPID --value)
if [[ "$pid" =~ ^[1-9][0-9]*$ ]] && \
   timeout --foreground "$((readiness_timeout + query_timeout))s" "$launcher" --config "$config" check-running --pid "$pid" --wait "$readiness_timeout"; then
    current_pid=$(ctl show "$service" --property=MainPID --value)
    state=$(ctl show "$service" --property=ActiveState --value)
    if [[ "$current_pid" == "$pid" && "$state" == "active" ]]; then
        echo "Cairn is running in the background: service=$service PID=$pid"
        echo "Logs: ./scripts/start.sh logs | Stop: ./scripts/end.sh"
        exit 0
    fi
fi
echo "Server did not become ready; service=$service PID=${pid:-0}. Inspect: journalctl -u $service -n 50" >&2
ctl status --no-pager "$service" >&2 || true
exit 1
