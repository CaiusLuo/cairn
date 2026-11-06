#!/bin/bash
# systemd is the only background owner. No nohup, PID file or process killing.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
action=${1:-start}
if (( $# > 1 )); then
    echo "Usage: ./start.sh [start|stop|restart|status|logs]" >&2
    exit 2
fi
service=${CAIRN_SERVICE:-cairn.service}
config=${CAIRN_CONFIG:-/etc/cairn/config.toml}
case "$service" in
    cairn*.service) ;;
    *) echo "CAIRN_SERVICE must name a Cairn .service unit" >&2; exit 2 ;;
esac
case "$action" in
    start|restart)
        [[ "$config" = /* ]] || { echo "CAIRN_CONFIG must be absolute" >&2; exit 2; }
        config=$(realpath -e -- "$config")
        launcher="$root/.venv/bin/cairn"
        [[ -x "$launcher" ]] || { echo "Run uv sync --locked --all-groups first" >&2; exit 1; }
        "$launcher" --config "$config" check-config
        command=$(systemctl show "$service" --property=ExecStart --value)
        case "$command" in
            *"--config $config "*) ;;
            *) echo "Unit $service does not use --config $config. Inspect/install the unit; no service was stopped." >&2; exit 1 ;;
        esac
        if systemctl is-active --quiet "$service"; then
            pid=$(systemctl show "$service" --property=MainPID --value)
            "$launcher" --config "$config" check-running --pid "$pid"
        fi
        exec systemctl "$action" "$service"
        ;;
    stop|status) exec systemctl "$action" "$service" ;;
    logs) exec journalctl --unit="$service" --follow ;;
    *) echo "Usage: ./start.sh [start|stop|restart|status|logs]" >&2; exit 2 ;;
esac
