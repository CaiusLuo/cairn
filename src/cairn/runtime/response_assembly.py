"""Bounded, per-attempt assembly. Only finish() exposes an LLMResponse."""

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from cairn.core.llm import LLMStreamChunk, StreamProtocolError, ToolCallDelta
from cairn.core.models import LLMResponse, LLMUsage, ToolCall


@dataclass(frozen=True, slots=True)
class StreamLimits:
    text_bytes: int = 1_048_576
    argument_bytes: int = 1_048_576
    tool_calls: int = 32
    chunks: int = 16_384

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value < 1
            for value in (
                self.text_bytes,
                self.argument_bytes,
                self.tool_calls,
                self.chunks,
            )
        ):
            raise ValueError("Stream limits must be positive.")


@dataclass(slots=True)
class _Call:
    id: str = ""
    name: str = ""
    arguments: list[str] = field(default_factory=list)
    identity_sealed: bool = False


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StreamProtocolError("Duplicate JSON argument key.")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise StreamProtocolError("Non-finite JSON argument value.")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise StreamProtocolError("Non-finite JSON argument value.")
    return result


class ResponseAssembler:
    """Append-only identity fragments must precede nonempty argument fragments.

    New call indices start at zero and are contiguous; subsequent fragments may
    interleave. Finish is unique, followed only by usage. A parseable call or a
    finish marker alone does not expose a response: the consumer must also reach
    clean exhaustion and close its stream before using finish().
    """

    def __init__(self, limits: StreamLimits | None = None) -> None:
        self.limits = limits or StreamLimits()
        self._text: list[str] = []
        self._calls: list[_Call] = []
        self._text_bytes = 0
        self._argument_bytes = 0
        self._chunks = 0
        self._finish: str | None = None
        self._usage: LLMUsage | None = None
        self._discarded = False

    def add(self, chunk: LLMStreamChunk) -> None:
        if self._discarded:
            raise StreamProtocolError("Stream assembly was discarded.")
        self._chunks += 1
        if self._chunks > self.limits.chunks:
            raise StreamProtocolError("Stream chunk limit exceeded.")
        if not isinstance(chunk, LLMStreamChunk):
            raise StreamProtocolError("Invalid normalized stream chunk.")
        if self._finish is not None and (
            chunk.text is not None
            or chunk.tool_calls
            or chunk.finish_reason is not None
        ):
            raise StreamProtocolError("Response data after finish.")
        if chunk.text is not None:
            self._text_bytes += self._bytes(chunk.text)
            if self._text_bytes > self.limits.text_bytes:
                raise StreamProtocolError("Stream text limit exceeded.")
            self._text.append(chunk.text)
        if not isinstance(chunk.tool_calls, tuple):
            raise StreamProtocolError("Invalid tool fragment group.")
        indices: set[int] = set()
        for delta in chunk.tool_calls:
            if (
                not isinstance(delta, ToolCallDelta)
                or type(delta.index) is not int
                or delta.index in indices
            ):
                raise StreamProtocolError("Invalid or repeated tool fragment index.")
            indices.add(delta.index)
            self._add_call(delta)
        if chunk.finish_reason is not None:
            if chunk.finish_reason not in ("stop", "tool_calls"):
                raise StreamProtocolError("Unsuccessful or unsupported stream finish.")
            self._finish = chunk.finish_reason
        if chunk.usage is not None:
            self._add_usage(chunk.usage)

    @staticmethod
    def _bytes(fragment: str) -> int:
        if not isinstance(fragment, str):
            raise StreamProtocolError("Stream fragment must be text.")
        try:
            return len(fragment.encode("utf-8"))
        except UnicodeEncodeError:
            raise StreamProtocolError("Invalid Unicode stream fragment.") from None

    def _add_call(self, delta: ToolCallDelta) -> None:
        if type(delta.index) is not int or not 0 <= delta.index <= len(self._calls):
            raise StreamProtocolError("Invalid tool call index order.")
        if delta.index == len(self._calls):
            if len(self._calls) >= self.limits.tool_calls:
                raise StreamProtocolError("Stream tool call limit exceeded.")
            self._calls.append(_Call())
        call = self._calls[delta.index]
        for attribute, fragment, limit in (
            ("id", delta.id, 256),
            ("name", delta.name, 128),
        ):
            if fragment is None:
                continue
            self._bytes(fragment)
            if fragment and call.identity_sealed:
                raise StreamProtocolError(
                    "Tool identity changed after arguments began."
                )
            combined = getattr(call, attribute) + fragment
            if len(combined) > limit:
                raise StreamProtocolError("Stream tool identity limit exceeded.")
            setattr(call, attribute, combined)
        if delta.arguments is not None:
            self._argument_bytes += self._bytes(delta.arguments)
            if self._argument_bytes > self.limits.argument_bytes:
                raise StreamProtocolError("Stream argument limit exceeded.")
            if delta.arguments:
                call.identity_sealed = True
            call.arguments.append(delta.arguments)

    def _add_usage(self, usage: LLMUsage) -> None:
        if not isinstance(usage, LLMUsage):
            raise StreamProtocolError("Invalid stream usage.")
        # Usage chunks are cumulative snapshots, not additive deltas. Keep exact
        # reported fields, including partial usage, without estimating others.
        values = (usage.input_tokens, usage.output_tokens)
        if any(
            value is not None and (type(value) is not int or value < 0)
            for value in values
        ):
            raise StreamProtocolError("Invalid stream usage counters.")
        if self._usage is None:
            self._usage = usage.model_copy(deep=True)
            return
        for name in ("input_tokens", "output_tokens"):
            value = getattr(usage, name)
            previous = getattr(self._usage, name)
            if value is not None:
                if previous is not None and value < previous:
                    raise StreamProtocolError("Stream usage counters decreased.")
                setattr(self._usage, name, value)

    def finish(self) -> LLMResponse:
        if self._discarded:
            raise StreamProtocolError("Stream assembly was discarded.")
        if self._finish is None:
            raise StreamProtocolError("Stream exhausted without provider completion.")
        if bool(self._calls) != (self._finish == "tool_calls"):
            raise StreamProtocolError("Stream finish does not match tool call group.")
        calls: list[ToolCall] = []
        ids: set[str] = set()
        for call in self._calls:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", call.id):
                raise StreamProtocolError("Invalid or missing tool call identity.")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]{0,127}", call.name):
                raise StreamProtocolError("Invalid or missing tool function name.")
            if call.id in ids:
                raise StreamProtocolError("Duplicate completed tool call identity.")
            ids.add(call.id)
            try:
                arguments = json.loads(
                    "".join(call.arguments),
                    object_pairs_hook=_object,
                    parse_constant=_reject_constant,
                    parse_float=_finite_float,
                )
            except (ValueError, RecursionError):
                raise StreamProtocolError(
                    "Invalid tool argument JSON object."
                ) from None
            if not isinstance(arguments, dict):
                raise StreamProtocolError("Tool arguments must be a JSON object.")
            calls.append(ToolCall(id=call.id, name=call.name, arguments=arguments))
        return LLMResponse(
            content="".join(self._text) if self._text else None,
            tool_calls=calls,
            usage=self._usage.model_copy(deep=True)
            if self._usage is not None
            else None,
        )

    def discard(self) -> None:
        """Release provisional buffers; this attempt cannot be used again."""
        self._text.clear()
        for call in self._calls:
            call.arguments.clear()
            call.id = call.name = ""
        self._calls.clear()
        self._usage = None
        self._finish = None
        self._discarded = True
