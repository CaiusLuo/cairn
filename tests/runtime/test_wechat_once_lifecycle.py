"""One-shot acceptance at WeChat ingress, recovery and shutdown boundaries."""

from __future__ import annotations

import asyncio
import gc
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from cairn.integrations.wechat.serve import (
    MAX_INTENT_TASKS,
    ConversationDecision,
    ServeConfig,
    ServeReport,
    _Inbound,
    _Service,
)
from cairn.integrations.wechat.state import WechatAccount, WechatState
from cairn.runtime.lifecycle import settle_owned
from cairn.scheduling.models import ScheduleJob
from cairn.scheduling.store import ScheduleStore
from cairn.terminal.headless import HeadlessResult
from tests.support.wechat_service import _msg


class SimulatedCrash(BaseException):
    """Stop at a persisted boundary without claiming an ordinary user error."""


@dataclass
class Client:
    messages: list[dict[str, object]] = field(default_factory=list)
    sent: list[str] = field(default_factory=list)
    crash_send: bool = False
    polled: bool = False

    async def notify_start(self) -> None:
        return

    async def notify_stop(self) -> None:
        return

    async def get_updates(self, cursor: str, **kwargs: object) -> dict[str, object]:
        if not self.polled:
            self.polled = True
            return {"ret": 0, "msgs": self.messages, "get_updates_buf": "one"}
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send_text(self, **kwargs: object) -> dict[str, object]:
        self.sent.append(str(kwargs.get("text", "")))
        if self.crash_send:
            raise SimulatedCrash("process stopped during receipt delivery")
        return {"ret": 0}


@dataclass
class Harness:
    config: ServeConfig
    state: WechatState
    store: ScheduleStore
    model_inputs: list[str] = field(default_factory=list)

    def inbound(
        self,
        text: str = "十分钟后提醒我喝水",
        *,
        key: str = "msg:1",
        received_at: float | None = None,
    ) -> _Inbound:
        assert self.state.record_received(
            key, sender="owner", context_token="ctx", text=text
        )
        if received_at is not None:
            self.state.mark(key, "received", received_at=received_at)
        return _Inbound(key=key, sender="owner", context_token="ctx", text=text)

    def service(
        self,
        *,
        store: ScheduleStore | None = None,
        client: Client | None = None,
        stop: asyncio.Event | None = None,
    ) -> _Service:
        async def execute(
            prompt: str, cancel: asyncio.Event, task_id: str
        ) -> HeadlessResult:
            self.model_inputs.append(prompt)
            raise AssertionError("scheduling must not execute a model task")

        async def classify(text: str) -> ConversationDecision:
            self.model_inputs.append(text)
            return ConversationDecision.UNKNOWN

        return _Service(
            config=self.config,
            state=self.state,
            client=client or Client(),  # type: ignore[arg-type]
            executor=execute,
            report=ServeReport(),
            stop_event=stop,
            conversation_classifier=classify,
            schedule_store=store or self.store,
        )


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = ServeConfig(
        workspace=workspace,
        state_dir=tmp_path / "state",
        account=WechatAccount(
            bot_token="fake-once-token",
            ilink_bot_id="bot",
            ilink_user_id="owner",
            base_url="https://example.test",
        ),
        approval=None,
        allowed_senders=frozenset({"owner"}),
        schedule_store=tmp_path / "schedule",
        poll_interval=0.0,
    )
    return Harness(
        config, WechatState(config.state_dir), ScheduleStore(tmp_path / "schedule")
    )


async def drain(service: _Service) -> list[BaseException]:
    """Observe all ingress-owned work; new descendants must finish as well."""
    errors: list[BaseException] = []

    async def complete() -> None:
        while service._intent_tasks:
            outcomes = await asyncio.gather(
                *tuple(service._intent_tasks), return_exceptions=True
            )
            errors.extend(
                outcome for outcome in outcomes if isinstance(outcome, BaseException)
            )

    await asyncio.wait_for(complete(), timeout=2)
    return errors


@pytest.mark.parametrize("boundary", ["before_save", "after_save", "during_send"])
def test_recovery_settles_once_creation_and_distinguishes_unknown_send(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    """Crash facts originate at _handle, then a new host runs _recover."""
    original_add = harness.store.add_or_get_job

    def crash_save(job: ScheduleJob) -> tuple[ScheduleJob, bool]:
        if boundary == "after_save":
            original_add(job)
        raise SimulatedCrash("process stopped at schedule save boundary")

    async def scenario() -> None:
        inbound = harness.inbound()
        first_received = harness.state.get_record("msg:1")["received_at"]  # type: ignore[index]
        first = harness.service(client=Client(crash_send=boundary == "during_send"))
        with monkeypatch.context() as injection:
            if boundary in {"before_save", "after_save"}:
                injection.setattr(harness.store, "add_or_get_job", crash_save)
            await first._handle(inbound)
            failures = await drain(first)
        assert any(isinstance(error, SimulatedCrash) for error in failures)
        before = harness.store.load_jobs()
        assert len(before) == (0 if boundary == "before_save" else 1)
        second_client = Client()
        second = harness.service(client=second_client)
        await second._recover()
        assert await drain(second) == []
        jobs = harness.store.load_jobs()
        assert len(jobs) == 1, "a pre-save crash must not lose the accepted request"
        assert jobs[0].at is not None
        assert jobs[0].at.timestamp() == pytest.approx(
            first_received + 600, abs=0.00001
        )
        if before:
            assert jobs == before, "recovery must reuse the saved exact job"
        record = harness.state.get_record("msg:1") or {}
        if boundary == "during_send":
            assert record.get("schedule_receipt_state") == "unknown"
            assert second_client.sent == [], "unknown receipt sends must not replay"
        else:
            assert record.get("schedule_receipt_state") == "sent"
            assert second_client.sent, "an unattempted receipt must survive restart"
        assert harness.model_inputs == []

    asyncio.run(scenario())


def test_service_shutdown_waits_for_slow_schedule_store_without_blocking_status(
    harness: Harness,
) -> None:
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    class BlockingStore(ScheduleStore):
        def add_or_get_job(self, job: Any) -> Any:
            entered.set()
            try:
                if not release.wait(timeout=3):
                    raise TimeoutError("test did not release schedule storage")
                return super().add_or_get_job(job)
            finally:
                finished.set()

    async def scenario() -> None:
        stop = asyncio.Event()
        client = Client(messages=[_msg("十分钟后提醒我喝水", sender="owner")])
        service = harness.service(
            store=BlockingStore(harness.store.root), client=client, stop=stop
        )
        running = asyncio.create_task(service.run())
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            status = harness.inbound("/status", key="msg:status")
            await asyncio.wait_for(service._handle(status), timeout=0.5)
            assert client.sent and not finished.is_set()
            stop.set()
            done, _ = await asyncio.wait({running}, timeout=0.05)
            assert running not in done, (
                "host returned while its write thread was still active"
            )
            assert not finished.is_set()
        finally:
            release.set()
            stop.set()
            await asyncio.wait_for(running, timeout=2)
            assert await asyncio.to_thread(finished.wait, 1)
        assert len(harness.store.load_jobs()) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("text", "age"),
    [
        ("明天25点提醒我喝水", 0),
        ("明天一二十点提醒我喝水", 0),
        ("十分钟后提醒我喝水", 3600),
    ],
)
def test_invalid_or_expired_time_is_a_per_message_clarification(
    harness: Harness, text: str, age: int
) -> None:
    async def scenario() -> None:
        client = Client()
        service = harness.service(client=client)
        inbound = harness.inbound(
            text,
            received_at=time.time() - age,
        )
        await service._handle(inbound)
        assert await drain(service) == []
        assert harness.store.load_jobs() == ()
        assert harness.model_inputs == []
        assert client.sent, "invalid time needs a clarification, not a stopped service"

    asyncio.run(scenario())


def test_storage_failure_is_observed_and_reported_per_message(harness: Harness) -> None:
    class FailedStore(ScheduleStore):
        def add_or_get_job(self, job: Any) -> Any:
            raise OSError("fake-private-storage-error")

    async def scenario() -> None:
        client = Client()
        service = harness.service(store=FailedStore(harness.store.root), client=client)
        unhandled: list[dict[str, Any]] = []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        try:
            await service._handle(harness.inbound())

            async def idle() -> None:
                while service._intent_tasks:
                    await asyncio.sleep(0)

            await asyncio.wait_for(idle(), timeout=2)
            gc.collect()
            await asyncio.sleep(0)
            assert unhandled == [], "the host must observe its background failure"
            record = harness.state.get_record("msg:1") or {}
            assert record.get("state") not in {"received", "schedule_pending"}
            assert client.sent, "the sender needs the creation failure diagnosis"
            assert harness.store.load_jobs() == ()
            assert harness.model_inputs == []
        finally:
            loop.set_exception_handler(previous_handler)
            await settle_owned(*tuple(service._intent_tasks))

    asyncio.run(scenario())


def test_full_schedule_backlog_does_not_leak_a_coroutine_or_call_model(
    harness: Harness, recwarn: pytest.WarningsRecorder
) -> None:
    async def scenario() -> None:
        service = harness.service()

        async def hold_slot() -> None:
            await asyncio.Event().wait()

        blockers = [asyncio.create_task(hold_slot()) for _ in range(MAX_INTENT_TASKS)]
        service._intent_tasks.update(blockers)
        try:
            await service._handle(harness.inbound())
            gc.collect()
            await asyncio.sleep(0)
            assert harness.store.load_jobs() == ()
            assert harness.model_inputs == []
            assert len(service._intent_tasks) == MAX_INTENT_TASKS
            assert (harness.state.get_record("msg:1") or {}).get("state") == "rejected"
            assert not [
                item for item in recwarn if issubclass(item.category, RuntimeWarning)
            ]
        finally:
            await settle_owned(*blockers)
            service._intent_tasks.clear()

    asyncio.run(scenario())


def test_reminder_payload_can_contain_a_negative_instruction(harness: Harness) -> None:
    async def scenario() -> None:
        service = harness.service()
        await service._handle(harness.inbound("十分钟后提醒我不要迟到"))
        assert await drain(service) == []
        assert harness.model_inputs == [], "payload negation is not a negated schedule"
        jobs = harness.store.load_jobs()
        assert len(jobs) == 1 and jobs[0].reminder
        assert jobs[0].prompt == "不要迟到"

    asyncio.run(scenario())


def test_cancelled_creation_preserves_primary_when_storage_also_fails(
    harness: Harness,
) -> None:
    entered, release = threading.Event(), threading.Event()

    class FailedAfterStop(ScheduleStore):
        def add_or_get_job(self, job: Any) -> Any:
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release storage")
            raise OSError("private-storage-detail")

    async def scenario() -> None:
        service = harness.service(store=FailedAfterStop(harness.store.root))
        await service._handle(harness.inbound())
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            creating = next(iter(service._intent_tasks))
            creating.cancel("original stop")
            await asyncio.sleep(0)
            creating.cancel("repeated stop")
            await asyncio.sleep(0)
            assert not creating.done()
            release.set()
            with pytest.raises(asyncio.CancelledError, match="original stop") as error:
                await creating
            assert error.value.args == ("original stop",)
            assert "private-storage-detail" not in str(
                getattr(error.value, "__notes__", ())
            )
        finally:
            release.set()
            await settle_owned(*tuple(service._intent_tasks))

    asyncio.run(scenario())


def test_unconfirmed_creation_receipt_is_not_retried(harness: Harness) -> None:
    from cairn.integrations.wechat.ilink import ILinkError

    class UnconfirmedClient(Client):
        async def send_text(self, **kwargs: object) -> dict[str, object]:
            self.sent.append(str(kwargs.get("text", "")))
            raise ILinkError("receipt response unavailable")

    async def scenario() -> None:
        client = UnconfirmedClient()
        service = harness.service(client=client)
        await service._handle(harness.inbound())
        assert await drain(service) == []
        assert len(client.sent) == 1
        assert len(harness.store.load_jobs()) == 1
        assert (harness.state.get_record("msg:1") or {}).get(
            "schedule_receipt_state"
        ) == "unknown"
        restarted_client = Client()
        restarted = harness.service(client=restarted_client)
        await restarted._recover()
        assert await drain(restarted) == []
        assert restarted_client.sent == []

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["revoked", "unstable", "non_text", "owner_changed"])
def test_schedule_recovery_rechecks_current_authorization(
    harness: Harness, change: str
) -> None:
    from dataclasses import replace

    harness.inbound()
    harness.state.mark("msg:1", "schedule_pending")
    if change == "revoked":
        harness.config = replace(harness.config, allowed_senders=frozenset({"other"}))
    elif change == "unstable":
        harness.state.mark("msg:1", "schedule_pending", stable=False)
    elif change == "non_text":
        harness.state.mark("msg:1", "schedule_pending", is_text=False)
    else:
        harness.config = replace(
            harness.config,
            account=replace(harness.config.account, ilink_user_id="other"),
        )

    async def scenario() -> None:
        client = Client()
        service = harness.service(client=client)
        await service._recover()
        assert await drain(service) == []
        assert harness.store.load_jobs() == ()
        assert client.sent == []
        assert harness.model_inputs == []
        assert (harness.state.get_record("msg:1") or {}).get("state") == "rejected"

    asyncio.run(scenario())
