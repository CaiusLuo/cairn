# Project Memory：实现调查与 Cairn V1 建议

调查基于源代码快照，而非产品宣传。learn-cc 使用本地干净 checkout
`0dcafa2ae053a1ddd6a72f265431104b08a5aa13`；Codex 和 pi 使用 GitHub 获取的
`8c0f7011674145da7e10627769fb221c74fbf7a0`、
`cdaf6c227d4718a72b59ccd02152c9614df44cfc`。以下结论仅覆盖这些版本和所列模块。

## 结论

Cairn 适合保留独立 memory 包，通过 Harness 人工审核写入，按当前 Workspace
检验适用性，再以可选上下文进入请求。三个参考项目分别提供了选择性读取、读写分离、
持久化与模型上下文分离的经验；它们的会话摘要或自动生成内容不能直接充当
Issue #23 要求的“已批准、具备明确证据范围、当前 checkout 可用”的工程知识。

| 项目 | 实际机制 | Cairn 借鉴 | V1 不照搬 |
| --- | --- | --- | --- |
| learn-cc | Markdown 记录与索引；模型选取、提取、合并 | 先筛选再加载；允许零结果；限制条目数量 | 自动提取、自动改写、工作区内可写 memory 文件 |
| Codex | rollout 提取与全局 consolidation 两阶段；数据库协调；受限读取上下文 | 读写职责分开；有界工作；来源信息；写入协调 | 后台提取与 consolidation agent、全局个人偏好 |
| pi | 会话树、compaction、扩展持久数据与上下文消息分别建模 | 保存的数据不等于自动注入的数据；构建请求时显式投影 | 把会话摘要当项目事实库、通用扩展框架 |

## learn-cc：不仅有 compaction，也有独立 memory 教学实现

[s09_memory/code.py](https://github.com/shareAI-lab/learn-claude-code/blob/0dcafa2ae053a1ddd6a72f265431104b08a5aa13/s09_memory/code.py)
将记录写入 `.memory/*.md`，重建 `MEMORY.md` 索引。`select_relevant_memories`
先让模型选最多五条，失败时回退关键词匹配；`load_memories` 再读取正文并限制总长度。
`build_system` 告知模型这些内容是背景知识，当前请求优先。

`extract_memories` 在回合结束后提取信息，经 `should_store_memory` 检查持久范围、
临时指令和重复记录后写入。`consolidate_memories` 达到数量阈值后让模型整理记录，
替换失败时恢复文件快照。这些检查并不等同于人工证据绑定或数据库并发事务。

[s06_context_compact.py](https://github.com/shareAI-lab/learn-claude-code/blob/0dcafa2ae053a1ddd6a72f265431104b08a5aa13/agents/s06_context_compact.py)
则处理另一件事：压缩旧工具结果，保存完整 transcript，并用模型摘要替换消息。
Cairn 的 Project Memory 不采用这种消息替换，也不保存完整 transcript。

建议借鉴 s09 的“先筛选、后加载、空结果正常”，把选取改成确定性规则，把写入改成
人工 approve + typed evidence + SQLite transaction。不要把模型生成的 persistent
标签视为批准，也不要用自然语言的背景提示代替真正的工具权限控制。

## Codex：复杂度主要来自自动写入管线与协调

[phase1.rs](https://github.com/openai/codex/blob/8c0f7011674145da7e10627769fb221c74fbf7a0/codex-rs/memories/write/src/phase1.rs)
从状态数据库认领 rollout 工作，限制并发，调用提取模型并处理敏感内容。
[phase2.rs](https://github.com/openai/codex/blob/8c0f7011674145da7e10627769fb221c74fbf7a0/codex-rs/memories/write/src/phase2.rs)
通过全局锁协调共享 memory workspace，准备增量材料并启动 consolidation agent。
[管线说明](https://github.com/openai/codex/blob/8c0f7011674145da7e10627769fb221c74fbf7a0/codex-rs/memories/README.md)
解释了 lease、重试、阶段输出、使用频率选择和文件产物；其中部分目录描述与当前文件
布局已有差异，应以同一 SHA 下实际代码为准。

[context/memory.rs](https://github.com/openai/codex/blob/8c0f7011674145da7e10627769fb221c74fbf7a0/codex-rs/core/src/context/memory.rs)
单独建模 memory context：读取说明与提取证据具有不同 role/content kind，并限制正文大小。
这支持一个重要设计判断：memory 自己拥有数据结构和边界，Agent 不必拥有整个记忆生成系统。

Cairn V1 没有后台工作队列，SQLite transaction + expected-version CAS 已足够处理人工
写入并发。应借鉴职责和资源边界，而不是复制两阶段 agent 工作流。本次调查的这些模块
没有为 Cairn 所需的“完整声明依赖 + 当前 checkout freshness”提供可直接复用的契约，
因此需要自己实现 Git tree、依赖指纹及人工绑定。

## pi：区分持久会话数据与真正进入模型的消息

[session-manager.ts](https://github.com/badlogic/pi-mono/blob/cdaf6c227d4718a72b59ccd02152c9614df44cfc/packages/coding-agent/src/core/session-manager.ts)
明确区分 `CustomEntry` 与 `CustomMessageEntry`：前者供扩展恢复状态，不进入 LLM
context；后者才投影成用户消息。会话通过父节点关系保留分支，compaction 记录保存摘要
和 `firstKeptEntryId`，构建当前上下文时选择相应历史。

[compaction 文档](https://github.com/badlogic/pi-mono/blob/cdaf6c227d4718a72b59ccd02152c9614df44cfc/packages/coding-agent/docs/compaction.md)
及 [custom-compaction 扩展示例](https://github.com/badlogic/pi-mono/blob/cdaf6c227d4718a72b59ccd02152c9614df44cfc/packages/coding-agent/examples/extensions/custom-compaction.ts)
展示了会话压缩和替换摘要策略。它们解决长会话与分支延续；仅凭这些模块不能断言 pi
生态完全没有其他 memory 扩展，也不能把示例当作具备审批与 freshness 的项目记忆库。

Cairn 可借鉴“持久数据与请求投影分开”，但当前只需要一个可选 `MemoryContext` 参数，
无需引入 session tree 或通用插件协议。

## 本项目落地与后续顺序

23A 使用独立模型和项目级 SQLite store，人工批准 API 留在 Harness；Git common
目录划分项目，使 Worktree 共享、不同 clone 隔离。23B 将 source HEAD、verified tree
和批准时 checkout HEAD 分开保存，通过最小证据 capsule 保留原始来源，并逐项检查
内容、类型、mode、存在性和声明的目录成员。23C 只注入 accepted 且 fresh 的相关条目，
先构造 baseline request，再利用剩余预算，fallback 逐模型重算。

当前最值得接着做的是独立代码审查和真实人工审批 UI：展示声明、完整依赖范围、证据
绑定和替换对象，让用户理解自己批准了什么。之后再以真实任务衡量检索命中、零结果、
过期排除和 token 开销；这些指标只记录 ID/计数。没有这些反馈前，不建议引入自动提取、
embeddings、consolidation 或扩大为跨项目个人记忆。

操作说明、资源限制与安全假设见 [project-memory.md](project-memory.md)。
