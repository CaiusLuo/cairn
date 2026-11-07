# Operational scripts

Run from the repository root after `uv sync --locked --all-groups`.

- [start.sh](start.sh) prepares missing host files and starts the systemd service.
- [end.sh](end.sh) reports and stops the selected systemd service.
- [ci/check_server_wheel.py](ci/check_server_wheel.py) installs a built wheel in
  a temporary environment and checks bundled Web/API startup outside the checkout.
  It uses an offline model and removes its isolated state after a clean stop.

Keep credentials in host configuration; do not include tokens in command text or reports.
