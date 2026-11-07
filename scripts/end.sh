#!/bin/bash
# Stop the named systemd service even when its configuration is broken.
set -euo pipefail
service=${CAIRN_SERVICE:-cairn.service}
query_timeout=10
stop_timeout=130
progress_interval=5
if (( $# != 0 )) || [[ ! "$service" =~ ^cairn[A-Za-z0-9_.@-]*\.service$ ]]; then
    echo "Usage: ./scripts/end.sh; CAIRN_SERVICE must name a Cairn .service unit" >&2
    exit 2
fi
for command in systemctl timeout; do
    command -v "$command" >/dev/null || { echo "Debian systemd and coreutils are required" >&2; exit 1; }
done
ctl() { timeout --foreground "${query_timeout}s" systemctl "$@"; }
if ! load_state=$(ctl show "$service" --property=LoadState --value); then
    [[ "$load_state" == "not-found" ]] || { echo "Cannot query systemd for $service within ${query_timeout}s" >&2; exit 1; }
fi
if [[ "$load_state" == "not-found" ]]; then
    echo "Service $service does not exist; PID=0, nothing to stop"
    exit 0
fi
pid=$(ctl show "$service" --property=MainPID --value)
state=$(ctl show "$service" --property=ActiveState --value)
echo "Stopping $service: PID=${pid:-0}, state=$state; waiting for graceful shutdown (up to ${stop_timeout}s)..."
# Queue a normal stop; systemd owns cleanup and its TimeoutStopSec policy.
ctl stop --no-block "$service"
deadline=$((SECONDS + stop_timeout))
next_progress=$((SECONDS + progress_interval))
while (( SECONDS < deadline )); do
    pid=$(ctl show "$service" --property=MainPID --value)
    state=$(ctl show "$service" --property=ActiveState --value)
    if [[ "$pid" == "0" && ( "$state" == "inactive" || "$state" == "failed" ) ]]; then
        echo "Stopped $service; PID=0"
        exit 0
    fi
    if (( SECONDS >= next_progress )); then
        echo "Waiting for graceful shutdown: service=$service PID=$pid state=$state"
        next_progress=$((SECONDS + progress_interval))
    fi
    sleep 1
done
echo "Stop wait exceeded ${stop_timeout}s: service=$service PID=$pid state=$state. Inspect systemctl status $service; no additional kill was requested." >&2
exit 1
