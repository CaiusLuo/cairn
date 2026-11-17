<p align="center">
  <img src="assets/cairn-github-readme.png" alt="Cairn — personal Agent" width="720">
</p>

Cairn is a personal Agent that runs on a Debian server. Use Web, CLI or messaging
clients to chat, manage personal information and work on code. The server keeps
conversations and tasks running when you close a client.

## What you can do

- **Chat and develop:** keep conversation history, follow progress, and start
  explicit development tasks in separate Git worktrees.
- **Keep knowledge and memory:** save private knowledge clips, ask the owner
  Agent to list or search them, and manage personal memory in Settings.
- **Schedule and review:** create reminders and generate daily or weekly reviews
  from clips, activity and journal entries, with optional WeChat delivery.
- **Connect messaging clients:** use WeChat, Telegram or Feishu alongside Web
  and CLI, with access controlled by the host.

## Install and start

Use Debian with systemd, Python 3.12+, [uv](https://docs.astral.sh/uv/), Node.js
and pnpm. Development tasks also need Git 2.40+. Startup checks the SQLite
library linked to Python.

```sh
git clone https://github.com/CaiusLuo/cairn.git
cd cairn
uv sync --locked --all-groups
pnpm --dir web install --frozen-lockfile
pnpm --dir web build
```

Create `.env` using [.env.example](.env.example), filling in your model, API key
and endpoint. Existing `.cairn/models.toml` provider configurations are also
supported. Then start the server:

```sh
sudo ./scripts/start.sh
```

First startup prepares missing host configuration, provider approval and systemd
files; it preserves existing files. Server settings live in
`/etc/cairn/config.toml`; see the [configuration example](deploy/config.example.toml).
For an offline smoke test, set `offline = true` and remove `provider_approval`
from `[server]`. Offline mode makes no paid model requests.

Open **http://127.0.0.1:8777/chat.html**. Sign in as `cairn`, using the contents
of `data_root/runtime/runtime.token` as the password (`data_root` is in the
server configuration). For a remote server, use an SSH tunnel:

```sh
ssh -N -L 8777:127.0.0.1:8777 YOUR_SERVER
```

To use the CLI, run this on the server from the configured workspace:

```sh
uv run cairn-cli --config /etc/cairn/config.toml
```

## Everyday use

Chat normally for questions and personal assistance. Use `/develop GOAL` when
you want a development task. Web and CLI can continue the same development task
by its Task ID, retaining its session and worktree.

| CLI command | Use |
| --- | --- |
| `/new` | Start a conversation |
| `/status`, `/history` | Check progress and history |
| `/use CONVERSATION_ID` | Switch conversations |
| `/use TASK_ID` | Select an existing development task |
| `/cancel` | Cancel the current run |
| `/wx`, `/wx status`, `/wx verify CODE` | Log in to WeChat and check its status |
| `/schedule history JOB_ID` | Inspect reminder results and delivery failures |

Web provides chat, development tasks, run history and recovery controls.
Settings manages memory, schedules and WeChat. Save clips in Knowledge, then ask
questions such as “What is in my knowledge base?” or “Find my notes about X.”
The owner's Agent reads clips on demand and cites their IDs; archived clips are
excluded. Clips are not automatically added to every conversation or personal
memory, and other senders do not gain access to your private knowledge.

Daily and weekly reviews can run on a schedule. To enable both, add this to
`/etc/cairn/config.toml`, set your authorized WeChat recipient and restart:

```toml
[review]
enabled = true
time = "20:30"
weekly_enabled = true
weekly_time = "20:40"
timezone = "Asia/Shanghai"
recipient = "YOUR_WECHAT_USER_ID"
```

Weekly reviews run on Sunday. WeChat delivery needs the configured recipient to
be logged in and a usable inbound reply context; send the bot a message if that
context has expired. Check report or reminder delivery status separately from
whether generation or execution succeeded. An unknown send outcome is not
retried automatically.

## Messaging channels

Open **`/channels.html`** to configure connections and inspect their status.

| Channel | Setup and current support |
| --- | --- |
| WeChat | QR login; one server owns one WeChat session |
| Telegram | Bot Token; private text messages |
| Feishu | Self-built app ID and secret, WebSocket events (`im.message.receive_v1`); private text messages |
| WeCom, QQ, Discord | Configuration only; adapters cannot start yet |

Set the owner and allowed sender identities in host-controlled configuration.
Allowing someone to chat does not grant them owner privileges. Group chats and
non-text messages are not supported by the Telegram and Feishu adapters. Run
only one Feishu connection per server process, and do not run competing clients
for the same messaging account or bot.

## Operation and usage tips

- Keep model credentials and the Web login token private. Use an SSH tunnel for
  remote access rather than exposing the default listener publicly.
- Ordinary chat does not allocate a development worktree. Start development
  explicitly; shell and network access depend on host configuration.
- Closing CLI or Web leaves the server running. Inspect recovery information
  before continuing interrupted work with an uncertain side effect.
- Use `sudo ./scripts/start.sh status` or `sudo ./scripts/start.sh logs` to
  diagnose issues, `sudo ./scripts/start.sh restart` after configuration changes,
  and `sudo ./scripts/end.sh` to stop the service.

## Development

Run focused behavioral tests and static checks for your changes:

```sh
uv run pytest PATH_TO_TESTS
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests scripts/ci
```

See [Tests](tests/README.md#feedback-cost) for full Debian acceptance and CI
behavior, and [Operational scripts](scripts/README.md) for maintenance commands.
Keep this README focused on setup, everyday use and current limitations;
implementation details and change history belong in code, tests and commits.
