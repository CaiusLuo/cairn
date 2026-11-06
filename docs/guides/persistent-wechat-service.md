# Debian WeChat deployment

The existing template supervises the standalone `cairn wechat serve` process.
It does not start a shared Runtime or configure the Runtime bridge. For shared
mode, first prepare the Runtime and use the bridge command in the
[WeChat guide](wechat-ilink.md); a combined systemd setup is not supplied.

## Prepare and install

Run as the intended service user: install the checkout's dependencies, configure
the workspace provider, complete `cairn wechat login` and verify a foreground
receiver with the same workspace, sender and private state. Use one receiver
owner per account/state. Do not embed credential values in the unit or argv.

Render `deploy/systemd/cairn-wechat.service.in`, replacing all placeholders:

```sh
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

Choose an existing service user with checkout access. The template sets explicit
`HOME` and `XDG_STATE_HOME`; they must locate the account/state used at login.
Optional `/etc/cairn/cairn-wechat.env` is host-private configuration, not a
repository file. Protect it and the account files from other users.

## Operate

```sh
sudo systemctl status cairn-wechat.service
sudo journalctl -u cairn-wechat.service -n 50 --no-pager
sudo journalctl -u cairn-wechat.service -f
sudo systemctl stop cairn-wechat.service
sudo systemctl restart cairn-wechat.service
```

The unit uses SIGTERM, owned cleanup and `Restart=on-failure` with bounded restart
frequency. Closing an SSH session does not stop a systemd service. Verify status
and a real reply after detaching; an installed unit alone does not establish
service health.

The local CLI's `/wx` flow instead starts a detached supervisor. `/wx status`
queries its private control socket; `background-owner.json` identifies the owner
and `background.log` holds its output. `/wx stop` stops that owner. Do not launch
the systemd receiver concurrently with it using the same state directory.

If a process dies, inspect the queue and surviving effects before recovering
started work. Neither a restart nor a deleted lock proves that work is safe to
repeat. See [operations and recovery](pilot-runbook.md).
