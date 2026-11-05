# LangGraph 最小执行实验

结论：**暂缓迁移**。LangGraph 已能编排模型与工具循环，并复用 Cairn 的权限边界；当前没有证据证明它会减少代码、token 或运行时间。值得继续一个兼容性验证切片，暂不接管生产。

任务：[Issue #96](https://github.com/CaiusLuo/cairn/issues/96)。基线：`f4b784269b7451b28d616c1604a4dc34b2613148`。分支：`feat/langgraph-spike`。

## 实际流程

```text
START -> 模型节点 -> 无工具 -> END
              |
              +-> 工具节点 -> 未达到模型步数上限 -> 模型节点
                         +-> 已达到上限 -> END -> RunBudgetExceeded
```

工具节点原样调用 `execute_tool_call`：存在性检查 → 参数校验 → 权限策略/审批 → 带授权上下文执行 → 工具消息。没有使用绕过 Cairn 权限的预制 ToolNode。

每次实验新建一个单次使用的 Agent。对话使用 Cairn 的 Message 和 AgentState，图状态仅保存步数和完成标记。后续模型失败或取消时，已执行工具的事实仍按现有规则保留。

模型节点复用 ModelExecutor，可注入已经配置好的 LLMClient、ContextBuilder 和 ModelExecutor；没有新增 Provider 解析、凭据读取、客户端或模型路由。演示只有模拟模型和内存只读工具。

## 离线运行与依赖

```bash
uv sync --locked --all-groups
uv run --group langgraph-spike python -m evaluation.langgraph_spike
uv run --group langgraph-spike pytest -q tests/evaluation/test_langgraph_spike.py
```

独立 `langgraph-spike` dependency group 固定 `langgraph==1.2.20`，传递版本和下载哈希锁在 `uv.lock`。普通生产安装不增加该组，没有生产模块导入实验。实验测试需要安装该组；现有 CI 使用 `--all-groups`。

新增 16 个包。保持原有 certifi 版本后，解析器选择兼容的 langchain-core；原有包版本保持不变。锁中包含 checkpoint/prebuilt/SDK 传递依赖，但实验没有启用其功能。图运行期间显式关闭 LangSmith export，不修改宿主环境。

同一输入、模拟模型和工具，两组各自创建独立实例：

| 观察 | Cairn Loop | LangGraph |
| --- | --- | --- |
| 答案 | `Answer: offline fixture` | `Answer: offline fixture` |
| 模型请求 | 2 | 2 |
| 工具执行 | 1 | 1 |
| 图节点 | 不适用 | model → tools → model |

第二次模型调用实际读取工具返回的 JSON。本轮未减少请求次数，未测量真实 token、费用或吞吐量。

## 哪些能替代，哪些保留

| 部分 | 本轮与后续处理 |
| --- | --- |
| 模型/工具分支及循环 | StateGraph 可承担这些编排语句；生产代码尚未删除 |
| 模型、fallback、上下文准入及流式组装 | 复用 ModelExecutor 和 ContextBuilder，保留 |
| Message、AgentState、ToolRegistry | 复用，避免平行消息体系 |
| 参数校验、权限和执行上下文 | 原样复用 execute_tool_call，必须保留 |
| 步数预算 | 复用 RunBudget/RunBudgetExceeded；模型调用计一步，最后一批工具结算后停止 |
| 取消和工具事实收尾 | 复用 settle_owned 与原 Loop 结算 helper，必须保留 |
| 遥测 | 复用 TurnTrace；usage、脱敏和最终结算仍需原有机制 |
| Session、DurableSession、TaskLedger、SQLite | 未接入；准入、事务、CAS、恢复和副作用证明必须保留 |
| Sandbox、凭据保护及发布检查 | 未修改，不能由图边或模型参数替代 |
| 压缩、记忆、技能、steering | 未接入最小图，保留原有实现，后续验证调用时机 |

## 风险与验证

自动化覆盖聊天、工具循环、无审批器及明确拒绝、硬策略拒绝、参数/存在性先于审批、工具异常及非零退出、步数停止、模型错误前后事实保留，以及模型/工具/审批三个阶段的重复取消。测试禁止 socket 网络连接。取消等待清理完成，在两秒测试期限内结束。

取消保证针对可协作取消的实现，不承诺对任意阻塞代码硬超时。图任务由 shield 和 settle_owned 收尾，不自动重试工具。

实验复用了私有 `_settle_aborted_turn`，它不是稳定公共接口。没有 checkpoint、retry、cache、resume；实例不能再次运行。闭包内的 Agent 不属于可序列化图状态，因此本轮不能当作 durable recovery 实现。完整事件兼容性、压缩、Session 准入及 trace 最终结算失败语义仍未验证。

节点重执行/恢复不能自动证明副作用未发生。未来仍需 fence、幂等及人工恢复边界。参考官方 [Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api) 和 [持久化说明](https://docs.langchain.com/oss/python/langgraph/persistence)。

边界自查：7/7（10/10），无失败项；框架只在外层实验，依赖指向现有契约和能力。该评分仅描述隔离，不代表生产迁移通过。

## 下一阶段最小切片

### 本轮验收记录（2026-11-05）

- 实验测试 17 项通过；连同 Loop、模型执行、权限、上下文、工具注册、架构及文档回归，共 469 项通过。
- `uv run --group langgraph-spike ruff check .` 通过。
- `uv run --group langgraph-spike ruff format --check --exclude .investigation .`：566 个文件通过。未排除时仅原有未跟踪 `.investigation/` 中两份 Markdown 报告有格式问题，未修改它们。
- `uv run --group langgraph-spike mypy src tests evaluation/langgraph_spike.py`：504 个源码文件通过。
- `git diff --check` 通过。模拟比较的独立命令见上方；不消耗付费 API。
- 未运行全部离线测试、真实 Provider 或生产验收；只报告本次相关回归结果。

相关回归的完整命令：

```bash
uv run --group langgraph-spike pytest -q tests/evaluation \
  tests/runtime/test_loop.py tests/runtime/test_loop_recovery.py \
  tests/runtime/test_loop_context.py tests/runtime/test_loop_streaming.py \
  tests/runtime/test_loop_tracing.py tests/runtime/test_loop_compaction.py \
  tests/runtime/test_model_executor.py tests/runtime/test_model_executor_streaming.py \
  tests/runtime/test_network_permissions.py tests/runtime/test_filesystem_permissions.py \
  tests/core/test_permissions.py tests/core/test_filesystem_permissions.py \
  tests/core/test_harness_permissions.py tests/core/test_context.py \
  tests/core/test_architecture.py tests/infra/tools/test_registry.py tests/docs
```

### 后续切片

只验证 stateless turn：形成可复用回合结算接口，将图放在同一回合所有者之内，比较流式事件、fallback、上下文压缩、usage 和异常结算。相同离线任务记录净代码量及宿主开销，证明收益后再讨论生产入口。

单一线性循环、严格副作用恢复或要求立即减少 token 的场景，目前不建议迁移。实际需要复杂分支、人工中断和多阶段流程时，再评估维护收益。

本轮不修改生产入口、SQLite、沙箱、微信或 UI，不合并 main。完成后停在待 Review 状态。
