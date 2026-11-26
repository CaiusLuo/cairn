<p align="center">
  <img src="assets/cairn-poster.png" alt="Cairn — 自托管个人 Agent" width="900">
</p>

<p align="center">
  <a href="README.md">English</a> · <strong>简体中文</strong>
</p>

![发送消息并等待回复](assets/screenshots/chat-send-demo.gif)

*在 Web 对话中点击发送，回复会带上知识来源和执行轨迹。仅使用演示数据。*

Cairn 是运行在 Debian 服务器上的个人 Agent。通过 Web、CLI 或消息客户端聊天、管理个人信息和处理代码。关闭客户端后，服务器上的对话和任务仍会继续运行。

## 一眼看懂

Cairn 是自托管的个人 AI Agent。Web、CLI 和消息渠道共享同一份对话与任务状态；关闭客户端不会中断服务器上的工作。

1. **对话** — 提问、跟进和查看历史；在不同客户端继续同一段对话。
2. **交给它一项任务** — 开发任务在独立 Git 工作树中运行，执行轨迹可追踪、结果可恢复。
3. **沉淀知识** — 保存私有片段、上传文档，并在回答中引用原始来源。
4. **自动复盘** — 日报和周报汇总对话、知识和文档，并按计划推送到已连接的渠道。
5. **连接常用渠道** — 微信、飞书、Telegram、QQ、Discord 使用统一的宿主权限模型；凭据只写入、不回显。

![带知识来源的对话](assets/screenshots/chat-conversation.png)

*回答可以带知识库来源和执行轨迹；同一段对话可在 Web、CLI 或微信继续。截图仅使用演示数据，不包含真实账号、令牌、聊天内容或个人记忆。*

<details>
<summary>更多界面：空白对话、日报列表、飞书连接、亮暗切换</summary>

![空白对话](assets/screenshots/chat-welcome.png)

*从空白对话开始，提供快捷入口。*

![日报列表](assets/screenshots/daily-reviews.png)

*日报列表保留每个版本的生成状态和来源范围。*

![飞书连接](assets/screenshots/feishu-connection.png)

*飞书连接配置；凭据只写入、不回显。*

![在亮色与暗色主题之间切换](assets/screenshots/theme-toggle-demo.gif)

*在亮色与暗色主题之间切换，新主题从切换按钮处扩散展开。*

</details>

## 使用声明

Cairn 是自托管的个人 Agent。你需要对自己接入的账号、数据、凭据和服务器负责，并且只处理你有权访问的数据与系统。模型输出可能出错，请复核重要操作并做好备份。不要在缺少 TLS 和访问控制的情况下暴露 Web 监听地址，并妥善保管模型凭据和运行时令牌。

本 README 中的截图仅使用演示数据，不包含真实账号、令牌、聊天内容或个人记忆。

## 能力一览

- **对话与开发**：保留对话历史、跟进进度，并在独立 Git 工作树中发起明确的开发任务。
- **附加文件**：在 Web 对话中把文本、PDF、Word、Excel、PowerPoint 或 HTML 文件附加到单轮对话；Agent 通过仅限该轮的工具读取文件。
- **知识与记忆**：保存私有知识片段，让所有者的 Agent 列出或搜索，并在设置中管理个人记忆。
- **定时与复盘**：创建提醒，基于片段、活动和日志生成日报或周报，并按计划投递到已连接的渠道。
- **连接消息客户端**：在 Web 和 CLI 之外使用微信、飞书、Telegram、QQ 或 Discord；访问权限由宿主控制。

## 安装与启动

使用 Debian + systemd、Python 3.12+、带 `vector` 和 `pg_trgm` 扩展的 PostgreSQL 17、[uv](https://docs.astral.sh/uv/)、Node.js 和 pnpm。开发任务还需要 Git 2.40+。

```sh
git clone https://github.com/CaiusLuo/cairn.git
cd cairn
uv sync --locked --all-groups
pnpm --dir web install --frozen-lockfile
pnpm --dir web build
```

为 Cairn 创建一个数据库和角色，然后让服务器连接它：

```sh
sudo apt install postgresql postgresql-17-pgvector
sudo -u postgres psql -c "CREATE ROLE cairn LOGIN PASSWORD 'YOUR_PASSWORD'"
sudo -u postgres createdb -O cairn cairn
sudo -u postgres psql -d cairn -c "CREATE EXTENSION IF NOT EXISTS vector"
sudo -u postgres psql -d cairn -c "CREATE EXTENSION IF NOT EXISTS pg_trgm"
```

在 `.env` 中添加 `CAIRN_DATABASE_URL=postgresql://cairn:...@localhost:5432/cairn`。`CAIRN_DATABASE_NAMESPACE`（默认 `cairn`）选择 schema 前缀，因此一个数据库可以承载多个互相隔离的 Cairn 实例。

参考 [.env.example](.env.example) 创建 `.env`，填写模型、API key 和 endpoint。也支持已有的 `.cairn/models.toml` 提供方配置。然后启动服务器：

```sh
sudo ./scripts/start.sh
```

首次启动会准备缺失的宿主配置、提供方审批和 systemd 文件，并保留已有文件。服务器设置位于 `/etc/cairn/config.toml`；参见[配置示例](deploy/config.example.toml)。离线冒烟测试可以设置 `offline = true`，并从 `[server]` 中移除 `provider_approval`。离线模式不会发起付费模型请求。

打开 **http://127.0.0.1:8777/chat.html**。使用 `data_root/runtime/runtime.token` 的内容作为密码，以 `cairn` 登录（`data_root` 在服务器配置中）。远程使用时，请把监听地址放在带 TLS 的反向代理后面。如果使用 nginx，为 `/api/runtime/attachments` 允许 10 MiB，为 `/api/knowledge/documents` 允许 25 MiB（或你配置的文档上传上限）。也可以通过 SSH 隧道连接：

```sh
ssh -N -L 8777:127.0.0.1:8777 YOUR_SERVER
```

在服务器上，从配置的工作区运行 CLI：

```sh
uv run cairn-cli --config /etc/cairn/config.toml
```

### 模型配置

登录后打开 **/model.html**，无需在服务器上编辑 TOML 即可配置提供方。可从国内
OpenAI 兼容预设中选择：Magpie、阿里云百炼/DashScope、DeepSeek、Kimi、智谱、
火山方舟、MiniMax、硅基流动，也可以填写自定义 HTTPS endpoint。使用「探测模型」
发现模型 ID，或每行填写一个模型 ID；也可以把提供方文档中的 curl 命令粘贴到
「从 curl 导入」，自动识别 Base URL、API Key 和模型 ID。curl 只在浏览器内解析，
不会上传。API Key 只写入、不回显：它保存在工作区 `.env` 中，不会返回给浏览器。

在**模型**页面选择默认提供方、模型组和模型，或在对话框的模型菜单中选择本次请求使用
的模型。已有 `.cairn/models.toml` 仍受支持，`/provider use` 和 `/model use` 可在
CLI 中切换默认模型。

### 从旧版 SQLite 升级

运行过旧版本的宿主会保留旧 SQLite 文件，直到执行导入。停止服务，先演练导入，再正式执行：

```sh
sudo systemctl stop cairn
uv run cairn-pg-import --state ~/.local/state/cairn --dry-run
uv run cairn-pg-import --state ~/.local/state/cairn
sudo ./scripts/start.sh
```

使用与服务相同的 `CAIRN_DATABASE_URL` 运行导入，或传入 `--database-url`。导入只读旧文件，把渠道凭据移动到 `<state>/channels/credentials`，按行数和 sha256 校验每一张表，并在任何不一致时回滚。两次演练应报告相同的表和哈希。`--replace` 会覆盖之前的导入。旧文件保留在原位以便回滚。如果旧版本留下了 `runtime/sessions-chat/sessions.sqlite3`，请先处理或归档；导入不会在两份分离的聊天历史之间自行选择。

## 日常使用

日常提问和个人协助直接聊天即可。需要开发任务时使用 `/develop GOAL`。Web 和 CLI 可以通过 Task ID 继续同一个开发任务，并保留其会话和工作树。

![开发任务](assets/screenshots/development-task.png)

*开发任务在独立 Git 工作树中运行；任务卡片保留状态和可恢复的执行时间线。*

| CLI 命令 | 用途 |
| --- | --- |
| `/new` | 开始新对话 |
| `/status`、`/history` | 查看进度和历史 |
| `/use CONVERSATION_ID` | 切换对话 |
| `/use TASK_ID` | 选择已有的开发任务 |
| `/cancel` | 取消当前运行 |
| `/wx`、`/wx status`、`/wx verify CODE` | 登录微信并查看状态 |
| `/schedule history JOB_ID` | 查看提醒结果和投递失败 |

Web 提供对话、开发任务、运行历史和恢复控制。设置页管理记忆、定时任务和微信。所有者的 Agent 会在对话中自动保存稳定的个人信息和偏好，可在设置中查看、纠正或忘记；不会保存凭据或一次性任务状态。在知识库中保存片段，然后提问，例如“我的知识库里有什么？”或“找一下关于 X 的笔记。”所有者的 Agent 会按需读取片段并引用片段 ID；已归档片段会被排除。片段不会自动加入每段对话或个人记忆，其他发送者也无法访问你的私有知识。

![记忆设置](assets/screenshots/settings.png)

*设置页统一管理记忆、定时任务和微信；规则变更需要确认。*

知识库也接受上传文档。打开 **Knowledge → 文档**，上传 PDF、Word、Excel、PowerPoint、HTML、Markdown 或纯文本（单文件 25 MiB）。Cairn 会抽取文本、切分为可引用的 chunk，并建立混合索引：PostgreSQL 全文检索、中文友好的 trigram 匹配，以及可选的向量。所有者的 Agent 使用 `knowledge_search` 搜索文档，使用 `document_read` 阅读，并引用 `document_id` 和 `chunk_id`。上传内容只对所有者可见。删除文档会同时删除其 chunk、图谱行和原始文件。重复上传会按内容哈希识别。配置的抽取和切分上限可能导致检索覆盖的文本少于预览内容。

![知识文档](assets/screenshots/knowledge-documents.png)

*文档上传后会显示解析与索引状态，并可通过全文、中文 trigram 和可选的向量检索。*

在 Web 对话中，搜索过文档库的完成回复最多会在答案下方显示三个来源。打开来源可以阅读精确的 chunk。使用 **用此片段追问** 把来源加入草稿，准备好后再发送。如果来源对应的文档或 chunk 已不存在，该来源会变为不可用。

扫描版 PDF 没有文本层。需要处理这些页面时，安装可选的 OCR 依赖并启用：

```sh
uv sync --extra ocr   # 或：pip install 'cairn[ocr]'
```

```toml
[knowledge]
ocr_enabled = true
ocr_max_pages = 30
ocr_dpi = 200
ocr_max_seconds = 120
```

只有没有可用文本层的页面会在本地用 RapidOCR 渲染和识别；普通 PDF 不会加载模型。OCR 使用两个 CPU 线程，并在每份文档处理完后释放模型。超大页面会以较低分辨率渲染，避免单页耗尽服务器内存。超过单文档页数上限或时间预算的页面会在抽取文本中报告。未安装该依赖时，`ocr_enabled = true` 会让文档以明确错误失败，而不是调用提供方。

向量检索需要嵌入模型。没有嵌入模型时，词法检索仍然可用：

```toml
[knowledge]
embedding_model = "qwen3.7-text-embedding-flash"
embedding_base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
# 空值会在 base URL 匹配时复用已审批的提供方 key。
embedding_key_env = ""
```

使用其他 endpoint 时，把 `embedding_key_env` 设为服务环境中对应凭据的名称。默认每次嵌入请求最多包含 10 个 chunk。使用服务器配置和凭据环境运行 `cairn knowledge-probe`，可以验证嵌入模型并打印维度。当前聊天模型会抽取文档之间的实体关系。如果图谱抽取或嵌入失败，文档仍可通过词法检索，Cairn 会在文档上记录失败原因。

日报和周报可以按计划运行。要同时启用两者，在 `/etc/cairn/config.toml` 中加入以下内容，设置授权的微信接收者并重启：

```toml
[review]
enabled = true
time = "20:30"
weekly_enabled = true
weekly_time = "20:40"
timezone = "Asia/Shanghai"
recipient = "YOUR_WECHAT_USER_ID"
```

每份报告覆盖周期内有价值的对话内容、新增的知识片段和已就绪文档，以及从文档库召回的相关旧知识。对话要点和召回知识与已验证完成的工作分开引用，避免把背景信息呈现为新完成的工作。周报复用周期内的日报，并用原始记录补齐缺失日期。

周报在周日运行。定时报告会推送到每个已启用、正在运行、具有所有者身份且此前接受过私聊的 IM 连接：报告会回到那个确切的会话。微信使用发件箱，需要配置的接收者已登录且有可用的入站回复上下文；如果上下文已过期，给 bot 发一条消息即可。其他渠道需要 bot 已启动，并至少有一条已接受的所有者消息，才能进行第一次定时推送。已停止、未配置或没有所有者的渠道会被跳过，而不是发送到猜测的目标。请把报告投递状态与生成是否成功分开检查。未知的发送结果不会自动重试。

![周报详情](assets/screenshots/weekly-review-detail.png)

*周报详情展示正文、投递状态和来源清单；日报和周报都可以按计划生成并推送。*

## 消息渠道

![渠道连接](assets/screenshots/channels-overview.png)

*渠道配置和连接状态集中在一个页面；凭据只写入、不回显。*

打开 **`/channels.html`** 配置连接并查看状态。

| 渠道 | 配置与当前支持 |
| --- | --- |
| 微信 | 扫码登录；一个服务器只拥有一个微信会话 |
| Telegram | Bot Token；私聊文本消息 |
| 飞书 | 自建应用 App ID 和 Secret，WebSocket 事件（`im.message.receive_v1`）；私聊文本消息 |
| QQ | App ID 和 Client Secret，启用 WebSocket 的 QQ bot，私聊文本消息 |
| Discord | Bot Token，Discord Gateway，私聊文本消息 |
| 企业微信 | 仅配置 |

扫码登录的账号就是所有者。通过 `/wx allow USER_ID` 或在设置页添加可信发送者。可信发送者可以聊天，但不能创建开发任务，也不能读取所有者的私有知识或记忆。Telegram、飞书、QQ 和 Discord 接受私聊文本消息。使用 QQ bot 作用域的 `user_openid` 或 Discord 数字用户 ID 作为所有者身份。Discord 需要安装 bot，并允许来自共同服务器的私聊。QQ 需要在 QQ 开放平台启用 WebSocket 和 C2C 事件。QQ 回复使用原消息的回复窗口。长回复会包含预览，完整结果在 Cairn Web 中查看。每个 Server 进程运行一个飞书连接，每个消息账号或 bot 运行一个客户端。

## 运维与使用提示

- 妥善保管模型凭据和 Web 登录令牌。远程暴露 Web 监听地址时，请放在带 TLS 的反向代理后面并限制访问。
- 备份状态的两部分：`pg_dump --format=custom <database>` 和 `data_root`（附件、渠道凭据和运行时令牌）。
- 普通聊天不会分配开发工作树。开发任务需要显式发起；shell 和网络访问取决于宿主配置。
- 关闭 CLI 或 Web 不会停止服务器。继续有不确定副作用的中断任务前，先查看恢复信息。
- 使用 `sudo ./scripts/start.sh status` 或 `sudo ./scripts/start.sh logs` 诊断问题；配置变更后使用 `sudo ./scripts/start.sh restart`；使用 `sudo ./scripts/end.sh` 停止服务。
- 部署后运行 `uv run python -m cairn.interfaces.cli.update_notice --since <上一个 SHA>`，通过既有定时任务把本次提交的一行摘要推送到微信。上次推送的提交记录在 `<state>/update-notice.json`，之后省略 `--since` 即可续接。微信未登录时不会排队，部署不受影响。

## 开发

针对改动运行聚焦的行为测试和静态检查：

```sh
uv run pytest PATH_TO_TESTS
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests scripts/ci
```

完整 Debian 验收和 CI 行为见[测试说明](tests/README.md#feedback-cost)，维护命令见[运维脚本](scripts/README.md)。README 只保留安装、日常使用和当前限制；实现细节和变更历史放在代码、测试和提交中。

## 许可证

Cairn 使用 [GNU General Public License v3.0 only](LICENSE)（GPL-3.0-only）授权。GPL 允许商用。分发 Cairn 或修改后的版本时，必须保留 [NOTICE](NOTICE) 中的版权和许可声明，并以同一许可证提供对应源码。需要保留的署名是：“Cairn — Copyright (C) 2026 CaiusLuo — https://github.com/CaiusLuo/cairn”。完整条款见 [LICENSE](LICENSE)。
