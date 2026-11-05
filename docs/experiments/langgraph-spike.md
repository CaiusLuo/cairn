# LangGraph execution on the experiment branch

[Issue #97](https://github.com/CaiusLuo/cairn/issues/97) promotes the minimal
[Issue #96](https://github.com/CaiusLuo/cairn/issues/96) graph into the existing
runtime.loop.run_turn. The production API and sole lifecycle owner remain the
same; model/tool nodes and conditional edges replace the manual step loop.
The separate experimental execution/rollback implementation has been removed.
The evaluation facade delegates to run_turn and is only a compatibility probe.

The shared [Runtime service](../guides/runtime-service.md) is the functional
CLI/Web/WeChat entry. Its tasks use real WorktreeProvider, CodingTaskRunner,
DurableSession and file tools; offline acceptance replaces only the LLM.

ModelExecutor, context admission, streaming assembly, compaction, permission
preflight and capability-derived authority are reused. Graph node execution is
owned and settled before rollback/trace/Session completion, including repeated
cancellation. Graph checkpoint, retry and automatic replay remain disabled.
LangSmith export is disabled for the graph without modifying host configuration.

LangGraph 1.2.20 is a locked runtime dependency. Both compatibility probe entry
forms now call the same turn owner; they are not a comparison with the retired
manual loop. The earlier 2-model-request/1-tool observation did not establish a
token saving. Actual token, time and cost limits belong to the shared task budget.

```sh
uv run python -m evaluation.langgraph_spike
uv run pytest -q tests/evaluation/test_langgraph_spike.py
```

This remains on feat/langgraph-spike pending Review; no main merge or online
service deployment is implied.
