# M1-A model streaming

Cairn displays model text incrementally under the `Cairn>` header. The final
answer is printed once. Tool and permission output follows response assembly;
incomplete tool arguments are never previewed. Streaming text is plain text:
Rich markup/emoji syntax is literal and C0/C1 terminal controls are filtered
(newline/tab remain). Generate-only clients keep the existing Markdown rendering.

OpenAI-compatible `delta.reasoning_content` strings are normalized separately as
`LLMStreamChunk.reasoning_text` and shown under a `thinking…` label, so a reader
can tell reasoning from the answer while it streams.
Thinking and answer fragments may occur in the same chunk. Thinking is transient:
it is never merged into the answer, retained in `LLMResponse` or conversation,
replayed to a provider, or stored in trace/checkpoint data. No provider-specific
thinking option is forced; this accepts the field when the selected provider
returns it. Generate-only clients retain their existing response contract.

This implements M1-A. [M1-B](session-lifecycle.md) supplies opt-in Session/Turn
identity, ordered delivery and admission; [M1-C.1](session-store.md),
[M1-C.2](durable-session.md) and [M1-C.3](durable-cli.md) supply the durable
store, admission and CLI. A text delta,
completed response, `agent_finish` or `trace_finish` is not durable acknowledgement.

## Ownership and acceptance

`LLMClient.generate()` is unchanged. The optional `StreamingLLMClient` capability
in [core/llm.py](../../src/cairn/core/llm.py) yields typed `LLMStreamChunk` values
and returns a closable async generator. Core exposes no SDK types.
Before requesting, ModelExecutor also respects an optional per-client
`supports_streaming` flag. Clients without that flag retain the existing optional
capability behavior; generate-only clients remain unchanged.

`LiteLLMClient → ModelExecutor / ResponseAssembler → run_turn → tool_execution`
is the response path. ModelExecutor selects and admits candidates as before; one owned
consumption task and one assembler belong to each attempt. It returns the same
`(ContextRequest, LLMResponse)` only after explicit successful finish, clean
exhaustion, full validation and cleanup. `run_turn` alone adds assistant messages,
tracks pending calls, executes tools and applies the existing rollback policy.
Trace ownership and exact/unknown usage aggregation remain with TurnTrace.

The [assembler](../../src/cairn/runtime/response_assembly.py) enforces:

| Contract | Rule |
| --- | --- |
| Text | Ordered UTF-8 fragments; at most 1 MiB cumulatively |
| Thinking | Valid UTF-8 fragments; independent cumulative 1 MiB limit per attempt; counted without retaining a reasoning buffer |
| Tool indices | New indices contiguous from zero; fragments may interleave |
| Identity | Append ID/name fragments before nonempty arguments; no identity changes after arguments begin; final IDs unique in the group |
| ID/name shape | IDs: ASCII letters/digits/underscore/hyphen, 1–256 characters; names: letter/underscore followed by letters/digits/underscore/dot/colon/hyphen, 1–128 characters |
| Arguments | At most 1 MiB across all calls; complete JSON objects; reject duplicate keys, non-finite numbers and invalid/incomplete JSON |
| Group/stream | At most 32 calls and 16,384 chunks, including empty metadata; no repeated index within one chunk |
| Finish | One `stop` for no tools or one `tool_calls` for a nonempty complete group; reject length/filter/legacy/unknown finishes and missing finish |
| After finish | Only usage/empty metadata; response data or another finish is invalid; wait for iterator exhaustion |
| Usage | Cumulative provider snapshots, never summed as deltas; preserve reported fields, reject decreasing counts, leave absent fields unknown |

A parseable argument object, a complete individual call, or even a finish marker
before exhaustion cannot trigger a tool. The entire group must pass validation.
Every attempt releases its provisional buffers on exit; completed responses hold
detached content, calls and usage. Existing preflight precedes permission checks,
and execution authority still comes exclusively from granted capabilities.

## SDK boundary and failures

The locked LiteLLM 1.101.0 chat API uses `acompletion(stream=True,
stream_options={"include_usage": True}, num_retries=0, max_retries=0)`. Cairn drives
the SDK wrapper but observes its upstream OpenAI-compatible chunks before SDK
normalization: that release synthesizes an EOF finish and may estimate usage.
Neither synthetic value is a provider completion fact.
Upstream read failures remain failures even after finish: that SDK release may
otherwise swallow them as EOF. Cairn requires observed clean upstream exhaustion.
Offline tests use actual OpenAI chunk objects and the installed CustomStreamWrapper
to pin this behavior.
The integration intentionally depends on its `completion_stream`/`aclose` seam;
SDK upgrades must rerun those tests.

LiteLLMClient defaults to streaming for explicit `openai/<chat-model>` and
`azure/<chat-deployment>` routes, including custom OpenAI-compatible endpoints.
Selection uses only the configured model ID, loaded SDK mode metadata and local
routing flags; it makes no provider request or credential/provider lookup. Cached
non-chat modes, `responses/` subroutes, legacy completion fine-tunes and enabled
SDK proxy/Responses rerouting decline streaming. Unknown IDs on the two explicit
chat routes remain eligible for custom models/deployments; chunks still require
full validation.

Native provider prefixes (including `deepseek`, `anthropic`, `gemini`, `azure_ai`),
implicit model names such as `gpt-4o`, and all other routes select the unchanged
`generate()` path before any request. An OpenAI-compatible endpoint alone does
not enable streaming. The decision is independent for every fallback candidate;
it does not change admission, request options or fallback eligibility.

The stream normalizer supports asynchronous, single-choice OpenAI-compatible chat
chunks and the LiteLLM `openai`/`azure` wrappers for that shape. An unexpected native
iterator, synchronous stream, multiple choices, legacy function calls, audio,
refusal or structured `reasoning` delta fails closed after a selected stream
request. The `reasoning_content` extension accepts only string/null values, not
objects, lists or scalar numbers. Repeated
full identity headers after arguments begin are unsupported. Explicitly requesting
a stream for a declined configuration fails before requesting the provider.
There is no hidden retry through `generate()` when streaming fails.

| Condition | Result / recovery |
| --- | --- |
| Eligible vendor error after fragments | Close stream, discard buffers, label visible output `abandoned`, then admit the next candidate with fresh client/counter/request/assembly |
| Invalid shape/finish/JSON/limits | Close/discard, label `incomplete`, propagate; no fallback or partial assistant/tool messages |
| Admission or display callback failure | No next candidate; callbacks cannot acquire provider fallback eligibility through a wrapped quota error |
| Cancellation before completion/exhaustion | Cancel and settle the owned attempt; finish provider cleanup despite repeated caller cancellation; label `incomplete`; preserve the primary cancellation |
| Provider error plus failed failure notification | Preserve provider error with a static note and stop fallback |
| Cleanup error after an otherwise complete stream | Reject response; static diagnostic, no tools; SDK-swallowed close failures are also detected |
| Failure/cancellation after an earlier accepted tool step | Preserve that step's settled facts; discard only the new incomplete response and retain existing pending-call abort handling |

Infrastructure owns request acquisition and stream/resource closure. Runtime
uses `settle_owned` for abandoning the consuming task and `await_owned` for its
generator close. Cleanup is cooperative, with no new timeout policy: a provider
that never settles cancellation/close may delay cancellation indefinitely.

Transient events `model_stream_start`, `model_text_delta`, `model_reasoning_delta`
and `model_stream_end` are attributed by step and candidate attempt (model ID
nullable). Reasoning events use the same `text` payload and cleanup/fallback
boundary as answer events; neither delta enters conversation or trace attributes.
`completed` means one assembled response, not a completed Turn. No events contain
tool fragments or credential options, and no raw provisional chunks enter trace
spans/reports. Credential-bearing errors are omitted; errors after provider chunks
use static diagnostics while retaining typed fallback/quota semantics.
No eval/report schema, frozen dataset, permissions or persistence contract changes.

## Offline verification

Event-barrier tests cover visibility before completion, interleaving, limits,
invalid groups, fresh fallback admission, callback failures, cancellation at each
boundary, repeated cancellation, cleanup failure and prior-tool fact retention.
Selection tests cover native/implicit routes, local capability decisions, exactly
one non-streaming request, both mixed fallback directions and unchanged tool grants.
CLI tests check plain text, answer deduplication, abandonment, trace ordering and
legacy Markdown. Provider calls are mocked; SDK-estimated usage is deliberately
different from reported usage.

```bash
uv run pytest tests/runtime/test_response_assembly.py tests/runtime/test_loop_streaming.py tests/runtime/test_model_executor_streaming.py tests/infra/llm/test_litellm_stream.py tests/terminal/test_streaming.py
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
```

Select related fallback, recovery, trace, terminal, eval, architecture and
documentation tests according to the changed contract; use the verification
scope rules in [AGENTS.md](../../AGENTS.md).
