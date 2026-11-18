"""Files attached to one conversation turn reach only that Run's model."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from cairn.agent.budget import RunBudget
from cairn.agent.models.messages import LLMResponse, Message, ToolCall
from cairn.agent.models.offline import OfflineDevelopmentModel
from cairn.server.bootstrap import runtime_app
from cairn.server.client import RuntimeClient
from tests.support.git import initialize_repository

WAIT_SECONDS = 5.0


class AttachmentModel(OfflineDevelopmentModel):
    """Call read_attachment once, then answer with what the tool returned."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0
        self.seen_tools: list[list[str]] = []

    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        self.seen_tools.append(
            [item["function"]["name"] for item in tools or [] if "function" in item]
        )
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="read_attachment",
                        arguments={"name": self.name},
                    )
                ],
            )
        result = next(
            message for message in reversed(messages) if message.role == "tool"
        )
        payload = json.loads(result.content or "{}")
        return LLMResponse(content="ATTACHMENT:" + str(payload.get("stdout", "")))


class PlainModel(OfflineDevelopmentModel):
    def __init__(self) -> None:
        self.seen_tools: list[list[str]] = []

    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        del messages
        self.seen_tools.append(
            [item["function"]["name"] for item in tools or [] if "function" in item]
        )
        return LLMResponse(content="plain answer")


def _app(tmp_path: Path) -> Any:
    source = tmp_path / "source"
    source.mkdir()
    initialize_repository(source)
    return runtime_app(
        source,
        tmp_path / "state",
        tmp_path / "worktrees",
        budget=RunBudget(5),
        token="zzzz",
    )


def _client(app: Any) -> RuntimeClient:
    return RuntimeClient(
        "http://127.0.0.1:8777",
        "zzzz",
        transport=httpx.ASGITransport(app=app),
    )


async def _wait_terminal(api: RuntimeClient, run_id: str) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + WAIT_SECONDS
    while True:
        run = await api.get_run(run_id)
        if run["status"] not in {"queued", "running"}:
            return run
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"Run {run_id} did not settle: {run['status']}")
        await asyncio.sleep(0.01)


def _pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, 1):
        offsets.append(len(output))
        output += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(output)
    output += f"xref\n0 {len(objects) + 1}\n".encode()
    output += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        output += f"{offset:010d} 00000 n \n".encode()
    output += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(output)


@pytest.mark.parametrize(
    ("name", "media_type", "payload", "expected"),
    [
        (
            "notes.txt",
            "text/plain",
            b"the launch code is 4321\n",
            "launch code is 4321",
        ),
        (
            "report.pdf",
            "application/pdf",
            _pdf("attachment says hello"),
            "attachment says hello",
        ),
    ],
)
def test_attachments_are_read_by_a_run_scoped_tool(
    tmp_path: Path, name: str, media_type: str, payload: bytes, expected: str
) -> None:
    app = _app(tmp_path)
    model = AttachmentModel(name)
    app.state.execution.llm_factory = lambda: model
    api = _client(app)

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            conversation = await api.create_conversation("hello", source="cli")
            upload = await api.upload_attachment(name, payload, media_type=media_type)
            assert upload["name"] == name
            run = await api.send_conversation(
                conversation["conversation_id"],
                "read the attachment",
                source="cli",
                request_id="attachment-run-1",
                attachment_ids=(upload["attachment_id"],),
            )
            settled = await _wait_terminal(api, run["run_id"])
            assert settled["status"] == "completed", settled
            assert expected in settled["result"]
            assert [item["name"] for item in settled["attachments"]] == [name]
            assert "read_attachment" in model.seen_tools[0]
            events = (await api.run_events(run["run_id"]))["events"]
            call = next(event for event in events if event["kind"] == "tool_call")
            assert call["data"]["tool"] == "read_attachment"
            stored = tmp_path / "state" / "attachments" / "runs" / run["run_id"] / name
            assert stored.read_bytes() == payload

            # A later Run has no attachment and must not inherit the tool.
            plain = PlainModel()
            app.state.execution.llm_factory = lambda: plain
            follow = await api.send_conversation(
                conversation["conversation_id"],
                "no attachment this time",
                source="cli",
                request_id="attachment-run-2",
            )
            assert (await _wait_terminal(api, follow["run_id"]))[
                "status"
            ] == "completed"
            assert "read_attachment" not in plain.seen_tools[0]

    asyncio.run(scenario())


def test_a_claimed_attachment_cannot_be_reused(tmp_path: Path) -> None:
    app = _app(tmp_path)
    model = AttachmentModel("notes.txt")
    app.state.execution.llm_factory = lambda: model
    api = _client(app)

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            conversation = await api.create_conversation("hello", source="cli")
            upload = await api.upload_attachment(
                "notes.txt", b"one use only", media_type="text/plain"
            )
            await api.send_conversation(
                conversation["conversation_id"],
                "first",
                source="cli",
                request_id="attachment-once-1",
                attachment_ids=(upload["attachment_id"],),
            )
            with pytest.raises(httpx.HTTPStatusError) as rejected:
                await api.send_conversation(
                    conversation["conversation_id"],
                    "second",
                    source="cli",
                    request_id="attachment-once-2",
                    attachment_ids=(upload["attachment_id"],),
                )
            assert rejected.value.response.status_code == 409

    asyncio.run(scenario())


def test_attachment_names_cannot_escape_the_run_directory(tmp_path: Path) -> None:
    app = _app(tmp_path)
    api = _client(app)

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            upload = await api.upload_attachment(
                "../../escape.txt", b"safe", media_type="text/plain"
            )
            assert upload["name"] == "escape.txt"
            assert not (tmp_path / "escape.txt").exists()

    asyncio.run(scenario())


def test_oversized_attachment_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cairn.interfaces.web.conversations as routes

    monkeypatch.setattr(routes, "MAX_ATTACHMENT_BYTES", 8)
    app = _app(tmp_path)
    api = _client(app)

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            with pytest.raises(httpx.HTTPStatusError) as rejected:
                await api.upload_attachment(
                    "big.txt", b"nine byte", media_type="text/plain"
                )
            assert rejected.value.response.status_code == 413

    asyncio.run(scenario())
