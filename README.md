<p align="center">
  <img src="assets/cairn-poster.png" alt="Cairn — self-hosted personal Agent" width="900">
</p>

<p align="center">
  <strong>English</strong> · <a href="README.zh-CN.md">简体中文</a>
</p>

https://github.com/user-attachments/assets/23328968-2fae-4fb0-a7c8-7710bfae6d0b

*39-second product tour. The video uses demo data only.*

Cairn is a personal Agent that runs on a Debian server. Use Web, CLI or messaging
clients to chat, manage personal information and work on code. The server keeps
conversations and tasks running when you close a client.

## At a glance

Cairn is a self-hosted personal AI Agent. Web, CLI and messaging clients share
the same conversation and task state; closing a client does not stop work on the
server.

1. **Chat** — ask, follow up and review history; continue the same conversation
   across clients.
2. **Delegate a task** — development tasks run in an isolated Git worktree, with
   a traceable and recoverable execution timeline.
3. **Build knowledge** — save private clips, upload documents, and cite the
   original sources in answers.
4. **Review automatically** — daily and weekly reports summarize conversations,
   knowledge and documents, then push to connected channels on schedule.
5. **Connect your channels** — WeChat, Feishu, Telegram, QQ and Discord share one
   host-controlled access model; credentials are write-only and never echoed
   back.

![Chat with cited knowledge](assets/screenshots/chat-conversation.png)

*Answers can include knowledge sources and an execution trace. The same
conversation can continue in Web, CLI or WeChat. Screenshots use demo data only;
they contain no real accounts, tokens, chat messages or personal memory.*

<details>
<summary>More screens: empty chat, daily reviews, Feishu connection</summary>

![Chat welcome](assets/screenshots/chat-welcome.png)

*Start from an empty conversation with quick-entry actions.*

![Daily reviews](assets/screenshots/daily-reviews.png)

*Daily reports keep each version's generation status and source scope.*

![Feishu connection](assets/screenshots/feishu-connection.png)

*Feishu connection settings; credentials are write-only and never echoed back.*

</details>

## Usage notice

Cairn is a self-hosted personal Agent. You are responsible for the accounts,
data, credentials and servers it accesses. Use it only with data and systems you
are authorized to access. Model output can be wrong; review important actions
and keep backups. Do not expose the Web listener without TLS and access control,
and keep model credentials and the runtime token private.

The screenshots in this README use demo data only. They contain no real
accounts, tokens, chat messages or personal memory.

## What you can do

- **Chat and develop:** keep conversation history, follow progress, and start
  explicit development tasks in separate Git worktrees.
- **Attach files:** in Web chat, attach text, PDF, Word, Excel, PowerPoint or
  HTML files to one turn; the Agent reads them through a tool scoped to that
  turn.
- **Keep knowledge and memory:** save private knowledge clips, ask the owner
  Agent to list or search them, and manage personal memory in Settings.
- **Schedule and review:** create reminders and generate daily or weekly reviews
  from clips, activity and journal entries, with scheduled delivery to connected
  channels.
- **Connect messaging clients:** use WeChat, Feishu, Telegram, QQ or Discord
  alongside Web and CLI, with access controlled by the host.

## Install and start

Use Debian with systemd, Python 3.12+, PostgreSQL 17 with the `vector` and
`pg_trgm` extensions, [uv](https://docs.astral.sh/uv/), Node.js and pnpm.
Development tasks also need Git 2.40+.

```sh
git clone https://github.com/CaiusLuo/cairn.git
cd cairn
uv sync --locked --all-groups
pnpm --dir web install --frozen-lockfile
pnpm --dir web build
```

Create one database and role for Cairn, then point the server at it:

```sh
sudo apt install postgresql postgresql-17-pgvector
sudo -u postgres psql -c "CREATE ROLE cairn LOGIN PASSWORD 'YOUR_PASSWORD'"
sudo -u postgres createdb -O cairn cairn
sudo -u postgres psql -d cairn -c "CREATE EXTENSION IF NOT EXISTS vector"
sudo -u postgres psql -d cairn -c "CREATE EXTENSION IF NOT EXISTS pg_trgm"
```

Add `CAIRN_DATABASE_URL=postgresql://cairn:...@localhost:5432/cairn` to `.env`.
`CAIRN_DATABASE_NAMESPACE` (default `cairn`) selects the schema prefix, so one
database can host several isolated Cairn instances.

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
server configuration). For remote use, put the listener behind a reverse proxy
with TLS. If you use nginx, allow 10 MiB for `/api/runtime/attachments`
and 25 MiB for `/api/knowledge/documents` (or your configured document upload
limit). You can also connect through an SSH tunnel:

```sh
ssh -N -L 8777:127.0.0.1:8777 YOUR_SERVER
```

To use the CLI, run this on the server from the configured workspace:

```sh
uv run cairn-cli --config /etc/cairn/config.toml
```

### Model configuration

After signing in, open **/model.html** to configure the provider without editing
TOML on the server. Choose a preset for a domestic OpenAI-compatible provider —
Magpie, Alibaba Cloud Bailian/DashScope, DeepSeek, Kimi, Zhipu, Volcengine Ark,
MiniMax or SiliconFlow — or enter a custom HTTPS endpoint. Use **获取模型列表**
to discover model IDs, or type them one per line. You can also paste a curl
command from the provider's documentation into **从 curl 自动填充** to detect the
Base URL, API key and model ID automatically. The curl is parsed in the browser
only and is not uploaded. The API key is write-only: it is stored in the
workspace `.env` and is never returned to the browser.

Saving preserves the other providers, model groups and TOML comments. The saved
provider and group become the first entries, and Cairn regenerates the provider
approval for **every model group of that provider**. Run the displayed command
to apply the change:

```sh
sudo ./scripts/start.sh restart
```

After the restart, the chat model menu lists every approved model grouped by
model group. The selected model is used for that request only; no further
configuration edit or restart is needed. The currently running turn keeps its
model until the restart. Existing `.cairn/models.toml` files remain supported,
and `/model use` can also switch the default model within any approved group.

### Upgrade from a legacy SQLite release

A host that ran a previous release keeps its legacy SQLite files until they are
imported. Stop the service, rehearse the import, then publish it:

```sh
sudo systemctl stop cairn
uv run cairn-pg-import --state ~/.local/state/cairn --dry-run
uv run cairn-pg-import --state ~/.local/state/cairn
sudo ./scripts/start.sh
```

Run the import with the same `CAIRN_DATABASE_URL` as the service, or pass
`--database-url`. The import reads legacy files read-only, moves channel
credentials into `<state>/channels/credentials`, compares every table by row
count and sha256, and rolls back on any mismatch. Two rehearsals report the same
tables and hashes. `--replace` overwrites an earlier import. Legacy files stay
in place for rollback. If an older release left
`runtime/sessions-chat/sessions.sqlite3`, resolve or archive it first; the import
refuses to choose between separated chat histories.

## Everyday use

Chat normally for questions and personal assistance. Use `/develop GOAL` when
you want a development task. Web and CLI can continue the same development task
by its Task ID, retaining its session and worktree.

![Development task](assets/screenshots/development-task.png)

*Development tasks have an isolated worktree, an execution timeline, and a
recoverable result.*

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

Knowledge also accepts uploaded documents. Open **Knowledge → 文档 (Documents)**
and upload PDF, Word, Excel, PowerPoint, HTML, Markdown or plain text (25 MiB per
file). Cairn extracts the text, splits it into citeable chunks, and builds a
hybrid index: PostgreSQL full-text, Chinese-friendly trigram matching, and
optional vectors. The owner's Agent searches documents with `knowledge_search`
and reads them with `document_read`, citing `document_id` and `chunk_id`.
Uploads are only visible to the owner. Deleting a document removes its chunks,
graph rows and original file. Duplicate uploads are recognized by content hash.
The configured extraction and chunk limits can make search cover less text than
the preview.

![Knowledge documents](assets/screenshots/knowledge-documents.png)

*Uploaded documents show parsing and indexing status and are searchable through
full-text, Chinese trigram and optional vector retrieval.*

In Web chat, completed replies that searched the document library can show up
to three sources below the answer. Open a source to read its exact chunk. Use
**用此片段追问 (Ask about this source)** to add the source to your draft, then
send it when ready. A source becomes unavailable if its document or chunk no
longer exists.

Scanned PDFs have no text layer. Install the optional OCR extra and enable it
when those pages matter:

```sh
uv sync --extra ocr   # or: pip install 'cairn[ocr]'
```

```toml
[knowledge]
ocr_enabled = true
ocr_max_pages = 30
ocr_dpi = 200
ocr_max_seconds = 120
```

Only pages without a usable text layer are rendered and recognized locally with
RapidOCR; normal PDFs never load the model. OCR uses two CPU threads and the
model is released after each document. Very large page dimensions are rendered
at a reduced resolution so one page cannot exhaust server memory. Pages above
the per-document page limit or time budget are reported in the extracted text.
Without the extra, `ocr_enabled = true` fails the document with a clear error
instead of calling a provider.

Vector search needs an embedding model. Without one, lexical search still works:

```toml
[knowledge]
embedding_model = "qwen3.7-text-embedding-flash"
embedding_base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
# Empty reuses the approved provider key when the base URLs match.
embedding_key_env = ""
```

For a different endpoint, set `embedding_key_env` to the name of its credential
in the service environment. Requests use up to 10 chunks per embedding batch
by default. Run `cairn knowledge-probe` with the server's configuration and
credential environment to verify the embedding model and print its dimension.
The current chat model extracts relationships between entities across documents.
If graph extraction or embedding fails, the document stays searchable
lexically and Cairn records the failure on the document.

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

Each report covers the period's valuable Conversation content, newly added
knowledge Clips and ready documents, and related older knowledge recalled from
the document library. Conversation highlights and recalled knowledge are cited
separately from verified completion, so background information is not presented
as new work. Weekly reviews reuse the period's daily reports and fill missing
days from raw records.

Weekly reviews run on Sunday. Scheduled reports are pushed to every enabled,
running IM connection that has an owner identity and a previously accepted
private conversation: the report goes back to that exact conversation. WeChat
uses the outbox and needs the configured recipient to be logged in with a usable
inbound reply context; send the bot a message if that context has expired. Other
channels need their bot started and at least one accepted owner message before
the first scheduled push. A channel that is stopped, unconfigured or without an
owner is skipped rather than sent to a guessed target. Check report delivery
status separately from whether generation succeeded. An unknown send outcome is
not retried automatically.

![Weekly review detail](assets/screenshots/weekly-review-detail.png)

*Weekly review details show the report, delivery status and source list; both
daily and weekly reports can be generated and pushed on schedule.*

## Messaging channels

![Channel connections](assets/screenshots/channels-overview.png)

*Channel configuration and connection status in one place; credentials are
write-only and never echoed back.*

Open **`/channels.html`** to configure connections and inspect their status.

| Channel | Setup and current support |
| --- | --- |
| WeChat | QR login; one server owns one WeChat session |
| Telegram | Bot Token; private text messages |
| Feishu | Self-built app ID and secret, WebSocket events (`im.message.receive_v1`); private text messages |
| QQ | App ID and Client Secret, WebSocket-enabled QQ bot, private text messages |
| Discord | Bot Token, Discord Gateway, private text messages |
| WeCom | Configuration only |

The QR-login account is the owner. Add trusted senders with `/wx allow USER_ID`
or from Settings. A trusted sender can chat, but cannot create development tasks
or read the owner's private knowledge or memory. Telegram, Feishu, QQ and
Discord accept private text messages. Use your QQ bot-scoped `user_openid` or
numeric Discord user ID as the owner identity. For Discord, install the bot
and allow direct messages from a shared server. For QQ, enable WebSocket and
C2C events in QQ Open Platform. QQ replies use the original message's reply
window. Long replies include a preview with the full result in Cairn Web.
Run one Feishu connection per Server process and one client per messaging
account or bot.

## Operation and usage tips

- Keep model credentials and the Web login token private. If you expose the Web
  listener remotely, put it behind a reverse proxy with TLS and restrict access.
- Back up both halves of the state: `pg_dump --format=custom <database>` and
  `data_root` (attachments, channel credentials and the runtime token).
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

## License

Cairn is licensed under the [GNU General Public License v3.0 only](LICENSE)
(GPL-3.0-only). Commercial use is permitted under the GPL. If you distribute
Cairn or a modified version, you must preserve the copyright and license notices
in [NOTICE](NOTICE) and make the corresponding source available under the same
license. The required attribution is: “Cairn — Copyright (C) 2026
CaiusLuo — https://github.com/CaiusLuo/cairn”.
See [LICENSE](LICENSE) for the full terms.
