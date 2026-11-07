#!/bin/bash
# Stop the named systemd service even when its configuration is broken.
set -euo pipefail
service=${CAIRN_SERVICE:-cairn.service}
if (( $# != 0 )) || [[ ! "$service" =~ ^cairn[A-Za-z0-9_.@-]*\.service$ ]]; then
    echo "Usage: ./end.sh; CAIRN_SERVICE must name a Cairn .service unit" >&2
    exit 2
fi
command -v systemctl >/dev/null || { echo "Debian systemd is required" >&2; exit 1; }
load_state=$(systemctl show "$service" --property=LoadState --value 2>/dev/null || true)
if [[ "$load_state" == "not-found" ]]; then
    echo "Service $service does not exist; PID=0, nothing to stop"
    exit 0
fi
if [[ -z "$load_state" ]]; then
    echo "Cannot query systemd for $service" >&2
    exit 1
fi
pid=$(systemctl show "$service" --property=MainPID --value)
state=$(systemctl show "$service" --property=ActiveState --value)
echo "Stopping $service: PID=${pid:-0}, state=$state"
# An explicit systemd stop also cancels Restart=on-failure; never kill a guessed PID.
systemctl stop "$service"
pid=$(systemctl show "$service" --property=MainPID --value)
state=$(systemctl show "$service" --property=ActiveState --value)
if [[ "$pid" != "0" || ( "$state" != "inactive" && "$state" != "failed" ) ]]; then
    echo "Service did not stop: service=$service PID=$pid state=$state" >&2
    exit 1
fi
echo "Stopped $service; PID=0"
