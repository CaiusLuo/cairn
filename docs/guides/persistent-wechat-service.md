# Persistent WeChat service (Debian systemd)

In the interactive `/wx` flow, starting WeChat also starts this service as a
detached host process with its own process group and a host-private
`background-owner.json` containing the pid and a stable session label. Closing
the CLI/SSH UI detaches only; it does not stop the background owner. A later
`/wx` status/resume discovers the same pid/session, and only `/wx stop` sends
SIGTERM and waits for the owner's cleanup. The detached process reuses the same
WeChat account, state directory, queues and scheduler store. If the process died,
the next start treats prior `started` work under the existing interrupted
recovery policy; it never claims the old process stayed alive.

This service hosts the existing `cairn wechat serve` entry point so it is not
owned by an SSH PTY. It does not make the foreground `uv run cairn` + in-process
`/wx` mode a daemon.

## Prerequisites

1. Complete the first `cairn wechat login` in a foreground terminal. The account
   token remains in the host-private state directory (`0600`), never in the unit
   file, argv or repository.
2. Prepare provider configuration and credentials in the same host/XDG state and
   workspace `.env`/`.cairn/models.toml` locations used by the foreground command.
   Do not put API keys in the unit or in `ExecStart`.
3. Verify one foreground `cairn wechat serve ...` invocation before enabling the
   service. Use the same workspace, sender, provider approval and state directory.

## Install

Render the template and install it:

```bash
cd /workspace/cairn
sed \
  -e 's|__CAIRN_ROOT__|/workspace/cairn|g' \
  -e 's|__CAIRN_USER__|cairn|g' \
  -e 's|__CAIRN_HOME__|/home/cairn|g' \
  -e 's|__CAIRN_XDG_STATE_HOME__|/home/cairn/.local/state|g' \
  -e 's|__WECHAT_SENDER__|YOUR_SENDER_ID|g' \
  -e 's|__PROVIDER_NAME__|YOUR_PROVIDER|g' \
  -e 's|__PROVIDER_ENDPOINT__|https://provider.example/v1|g' \
  -e 's|__PROVIDER_GROUP__|YOUR_GROUP|g' \
  -e 's|__PROVIDER_KEY_ENV__|YOUR_API_KEY_ENV|g' \
  -e 's|__PROVIDER_MODELS__|model-id|g' \
  deploy/systemd/cairn-wechat.service.in \
  | sudo tee /etc/systemd/system/cairn-wechat.service
sudo systemctl daemon-reload
sudo systemctl enable --now cairn-wechat.service
```

The unit runs the service as an explicit user with explicit `HOME` and
`XDG_STATE_HOME`; use the same account/state directory that contains the
already-bound WeChat account and queues. Do not point it at a new empty state
directory.

## Operations

```bash
sudo systemctl status cairn-wechat.service
sudo journalctl -u cairn-wechat.service -f
sudo systemctl stop cairn-wechat.service
sudo systemctl restart cairn-wechat.service
```

`Restart=on-failure` provides bounded restart with `RestartSec=5` and
`StartLimitIntervalSec/Burst`; `systemctl stop` sends SIGTERM, the service stops
admitting work, settles its owned worker/chat/classifier/scheduler cleanup and
releases the host state lock. It is not restarted after a normal stop.

`cairn wechat serve` keeps the existing single-instance state-directory lock.
If the service is already running, foreground `/wx` is not a second consumer;
stop the service first. Do not delete the lock file to bypass a live owner.

SIGTERM/SIGINT are mapped to the existing `serve_wechat` stop-event and owned
cleanup. Queued messages survive restart; a task already `started` is marked
interrupted according to existing recovery policy and is not replayed. Do not
automatically recreate missed scheduled tasks.

## SSH acceptance

After enabling, disconnect the test SSH/PTY session and verify WeChat still
answers:

```bash
# from another terminal, while the first SSH session is closed
sudo systemctl status cairn-wechat.service
sudo journalctl -u cairn-wechat.service -n 50 --no-pager
# send a normal progress/chat message from the trusted WeChat sender
```

The service must still be active and reply. This does not test the foreground
`uv run cairn` + `/wx` mode; that mode remains foreground-only.
