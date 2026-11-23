"""Files attached to one conversation turn reach only that Run's model."""

from __future__ import annotations

import asyncio
import json
import os
from io import BytesIO
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from docx import Document
from openpyxl import Workbook
from pptx import Presentation

from cairn.agent.budget import RunBudget
from cairn.agent.models.messages import LLMResponse, Message, ToolCall
from cairn.agent.models.offline import OfflineDevelopmentModel
from cairn.conversation.attachments import AttachmentStore
from cairn.conversation.models import ConversationSource, RunStatus
from cairn.conversation.service import ConversationService
from cairn.conversation.store import DomainStore
from cairn.development.catalogue import ChannelPrincipal
from cairn.server.bootstrap import runtime_app
from cairn.server.client import RuntimeClient
from tests.support.git import initialize_repository
from tests.support.pg import database_for

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
        self.seen_messages: list[list[Message]] = []

    async def generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        self.seen_messages.append(list(messages))
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
        database=database_for(tmp_path),
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


def _docx(text: str) -> bytes:
    document = Document()
    document.add_paragraph(text)
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = text
    table.cell(0, 1).text = "table cell"
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _xlsx(text: str) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Data"
    sheet.append(["launch code", text])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _pptx(text: str) -> bytes:
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    slide.shapes.add_textbox(0, 0, 300, 80).text_frame.text = text
    buffer = BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def _html(text: str) -> bytes:
    return (
        f"<html><head><title>Report</title></head><body><p>{text}</p>"
        "<script>bad()</script></body></html>"
    ).encode()


@pytest.mark.parametrize(
    ("name", "media_type", "payload", "expected", "forbidden"),
    [
        (
            "notes.txt",
            "text/plain",
            b"the launch code is 4321\n",
            "launch code is 4321",
            "",
        ),
        (
            "report.pdf",
            "application/pdf",
            _pdf("attachment says hello"),
            "attachment says hello",
            "",
        ),
        (
            "report.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            _docx("attachment says hello"),
            "attachment says hello",
            "",
        ),
        (
            "sheet.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            _xlsx("attachment says hello"),
            "attachment says hello",
            "",
        ),
        (
            "slides.pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            _pptx("attachment says hello"),
            "attachment says hello",
            "",
        ),
        (
            "page.html",
            "text/html",
            _html("attachment says hello"),
            "attachment says hello",
            "bad()",
        ),
    ],
    ids=["text", "pdf", "docx", "xlsx", "pptx", "html"],
)
def test_attachments_are_read_by_a_run_scoped_tool(
    tmp_path: Path,
    name: str,
    media_type: str,
    payload: bytes,
    expected: str,
    forbidden: str,
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
            assert not forbidden or forbidden not in settled["result"]
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


def test_lost_response_retry_returns_the_same_attachment_run(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    model = AttachmentModel("notes.txt")
    app.state.execution.llm_factory = lambda: model
    api = _client(app)

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            conversation = await api.create_conversation("hello", source="cli")
            upload = await api.upload_attachment(
                "notes.txt", b"retry me", media_type="text/plain"
            )
            first = await api.send_conversation(
                conversation["conversation_id"],
                "read the attachment",
                source="cli",
                request_id="attachment-retry-1",
                attachment_ids=(upload["attachment_id"],),
            )
            await _wait_terminal(api, first["run_id"])
            # The client never saw the response and retries with the same ID.
            # Admission already claimed the file, so a re-claim would 409.
            retry = await api.send_conversation(
                conversation["conversation_id"],
                "read the attachment",
                source="cli",
                request_id="attachment-retry-1",
                attachment_ids=(upload["attachment_id"],),
            )
            assert retry["run_id"] == first["run_id"]
            assert [item["name"] for item in retry["attachments"]] == ["notes.txt"]

    asyncio.run(scenario())


def test_same_request_id_with_a_different_attachment_set_conflicts(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    model = AttachmentModel("notes.txt")
    app.state.execution.llm_factory = lambda: model
    api = _client(app)

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            conversation = await api.create_conversation("hello", source="cli")
            first = await api.upload_attachment(
                "notes.txt", b"first", media_type="text/plain"
            )
            second = await api.upload_attachment(
                "other.txt", b"second", media_type="text/plain"
            )
            await api.send_conversation(
                conversation["conversation_id"],
                "read the attachment",
                source="cli",
                request_id="attachment-conflict-1",
                attachment_ids=(first["attachment_id"],),
            )
            with pytest.raises(httpx.HTTPStatusError) as rejected:
                await api.send_conversation(
                    conversation["conversation_id"],
                    "read the attachment",
                    source="cli",
                    request_id="attachment-conflict-1",
                    attachment_ids=(second["attachment_id"],),
                )
            assert rejected.value.response.status_code == 409

    asyncio.run(scenario())


def test_deleting_a_conversation_removes_its_attachment_files(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    model = AttachmentModel("notes.txt")
    app.state.execution.llm_factory = lambda: model
    api = _client(app)

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            conversation = await api.create_conversation("hello", source="cli")
            upload = await api.upload_attachment(
                "notes.txt", b"delete me", media_type="text/plain"
            )
            run = await api.send_conversation(
                conversation["conversation_id"],
                "read the attachment",
                source="cli",
                request_id="attachment-delete-1",
                attachment_ids=(upload["attachment_id"],),
            )
            await _wait_terminal(api, run["run_id"])
            stored = tmp_path / "state" / "attachments" / "runs" / run["run_id"]
            assert (stored / "notes.txt").is_file()
            app.state.conversations.delete_conversation(conversation["conversation_id"])
            assert not stored.exists()

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


def test_attachment_access_is_owner_only(tmp_path: Path) -> None:
    app = _app(tmp_path)
    api = _client(app)

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            guest_upload = await api.upload_attachment(
                "guest-notes.txt", b"guest secret", media_type="text/plain"
            )
            guest = ChannelPrincipal(
                connection_id="ch-1",
                platform="telegram",
                sender_id="guest-9",
                role="allowed",
                conversation_binding="guest-9",
            )
            guest_conversation = app.state.conversations.ensure_channel_conversation(
                "telegram", "ch-1", "guest-9", "guest-9", "hello"
            )
            plain = PlainModel()
            app.state.execution.llm_factory = lambda: plain
            guest_run = app.state.conversations.submit(
                guest_conversation.conversation_id,
                "read the attachment",
                ConversationSource.CHANNEL,
                "attachment-guest-1",
                attachment_ids=(guest_upload["attachment_id"],),
                metadata={"channel_principal": guest.model_dump(mode="json")},
            )
            await asyncio.wait_for(app.state.conversations.queue.join(), WAIT_SECONDS)
            settled = app.state.conversations.get_run(guest_run.run_id)
            assert settled is not None and settled.status is RunStatus.COMPLETED
            assert "read_attachment" not in plain.seen_tools[0]
            system = next(
                message
                for message in plain.seen_messages[0]
                if message.role == "system"
            )
            assert "guest-notes.txt" not in (system.content or "")

            owner_upload = await api.upload_attachment(
                "owner-notes.txt", b"owner secret", media_type="text/plain"
            )
            owner = ChannelPrincipal(
                connection_id="ch-1",
                platform="telegram",
                sender_id="owner-1",
                role="owner",
                conversation_binding="owner-1",
            )
            owner_conversation = app.state.conversations.ensure_channel_conversation(
                "telegram", "ch-1", "owner-1", "owner-1", "hello"
            )
            owner_model = AttachmentModel("owner-notes.txt")
            app.state.execution.llm_factory = lambda: owner_model
            owner_run = app.state.conversations.submit(
                owner_conversation.conversation_id,
                "read the attachment",
                ConversationSource.CHANNEL,
                "attachment-owner-1",
                attachment_ids=(owner_upload["attachment_id"],),
                metadata={"channel_principal": owner.model_dump(mode="json")},
            )
            await asyncio.wait_for(app.state.conversations.queue.join(), WAIT_SECONDS)
            settled = app.state.conversations.get_run(owner_run.run_id)
            assert settled is not None and settled.status is RunStatus.COMPLETED
            assert "read_attachment" in owner_model.seen_tools[0]
            assert "owner secret" in (settled.result or "")

    asyncio.run(scenario())


class RecordingExecution:
    calls = 0

    async def __call__(
        self,
        task: Any,
        emit: Any,
        *,
        service: Any | None = None,
        steering: Any | None = None,
    ) -> str:
        del task, emit, service, steering
        self.calls += 1
        return "ok"


def _queued_run_with_attachment(
    tmp_path: Path, *attachment_ids: str
) -> tuple[DomainStore, Any]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = DomainStore(database_for(tmp_path))
    conversation = store.create_conversation(
        workspace=workspace,
        source=ConversationSource.CLI,
        initial_prompt="resume claim",
    )
    run = store.accept_run(
        conversation_id=conversation.conversation_id,
        prompt="read the attachment",
        source=ConversationSource.CLI,
        request_id="resume-claim-1",
        metadata={"attachment_ids": list(attachment_ids)},
    )
    return store, run


def test_restart_completes_an_interrupted_attachment_claim(tmp_path: Path) -> None:
    attachment_store = AttachmentStore(tmp_path / "attachments")
    upload = attachment_store.stage("notes.txt", "text/plain", b"recovered")
    store, run = _queued_run_with_attachment(tmp_path, upload.attachment_id)
    execution = RecordingExecution()

    async def scenario() -> None:
        service = ConversationService(
            store,
            cast(Any, execution),
            workspace=tmp_path / "workspace",
            attachments_root=tmp_path / "attachments",
        )
        await service.start()
        try:
            await asyncio.wait_for(service.queue.join(), WAIT_SECONDS)
        finally:
            await service.close()

    asyncio.run(scenario())
    settled = store.get_run(run.run_id)
    assert settled is not None and settled.status is RunStatus.COMPLETED
    assert [item["name"] for item in settled.metadata["attachments"]] == ["notes.txt"]
    assert execution.calls == 1
    assert (
        tmp_path / "attachments" / "runs" / run.run_id / "notes.txt"
    ).read_bytes() == b"recovered"


def test_restart_uses_the_manifest_after_a_crash_mid_claim(
    tmp_path: Path,
) -> None:
    attachment_store = AttachmentStore(tmp_path / "attachments")
    upload = attachment_store.stage("notes.txt", "text/plain", b"bound before crash")
    store, run = _queued_run_with_attachment(tmp_path, upload.attachment_id)
    # Simulation: bind() finished, but the process died before the Run
    # metadata recorded the claim.
    attachment_store.bind(run.run_id, attachment_store.plan((upload.attachment_id,)))
    execution = RecordingExecution()

    async def scenario() -> None:
        service = ConversationService(
            store,
            cast(Any, execution),
            workspace=tmp_path / "workspace",
            attachments_root=tmp_path / "attachments",
        )
        await service.start()
        try:
            await asyncio.wait_for(service.queue.join(), WAIT_SECONDS)
        finally:
            await service.close()

    asyncio.run(scenario())
    settled = store.get_run(run.run_id)
    assert settled is not None and settled.status is RunStatus.COMPLETED
    assert [item["name"] for item in settled.metadata["attachments"]] == ["notes.txt"]
    assert execution.calls == 1


def test_restart_finishes_claim_after_only_one_file_moved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachments = AttachmentStore(tmp_path / "attachments")
    files = (
        attachments.stage("one.txt", "text/plain", b"one"),
        attachments.stage("two.txt", "text/plain", b"two"),
    )
    ids = tuple(item.attachment_id for item in files)
    store, run = _queued_run_with_attachment(tmp_path, *ids)
    plans = attachments.plan(ids)
    move = os.replace

    def interrupt_second_file(source: Path, target: Path) -> None:
        if source.name == ids[1]:
            raise OSError("interrupted before second move")
        move(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", interrupt_second_file)
        with pytest.raises(OSError, match="second move"):
            attachments.bind(run.run_id, plans)

    async def scenario() -> None:
        execution = RecordingExecution()
        service = ConversationService(
            store,
            cast(Any, execution),
            workspace=tmp_path / "workspace",
            attachments_root=attachments.root,
        )
        orphan = attachments.runs / ("run-" + "a" * 32)
        orphan.mkdir()
        await service.start()
        try:
            await asyncio.wait_for(service.queue.join(), WAIT_SECONDS)
        finally:
            await service.close()
        assert execution.calls == 1
        assert not orphan.exists()

    asyncio.run(scenario())
    settled = store.get_run(run.run_id)
    assert settled is not None and settled.status is RunStatus.COMPLETED
    assert [item["name"] for item in settled.metadata["attachments"]] == [
        "one.txt",
        "two.txt",
    ]
    assert (attachments.runs / run.run_id / "two.txt").read_bytes() == b"two"


def test_restart_fails_a_run_whose_uploads_are_gone(tmp_path: Path) -> None:
    store, run = _queued_run_with_attachment(tmp_path, "att-" + "1" * 32)
    execution = RecordingExecution()

    async def scenario() -> None:
        service = ConversationService(
            store,
            cast(Any, execution),
            workspace=tmp_path / "workspace",
            attachments_root=tmp_path / "attachments",
        )
        await service.start()
        try:
            await asyncio.wait_for(service.queue.join(), WAIT_SECONDS)
        finally:
            await service.close()

    asyncio.run(scenario())
    settled = store.get_run(run.run_id)
    assert settled is not None and settled.status is RunStatus.FAILED
    assert settled.error == "attachment_binding_failed"
    assert execution.calls == 0
