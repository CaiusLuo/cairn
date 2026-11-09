import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from cairn.development.execution.headless import HeadlessResult
from cairn.integrations.wechat.serve import (
    ConversationDecision,
    ServeConfig,
    _Inbound,
    _Service,
)
from cairn.integrations.wechat.state import WechatAccount, WechatState
from cairn.scheduling.store import ScheduleStore

NOW = datetime.now(ZoneInfo("Asia/Shanghai"))


class Client:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, **kwargs: object) -> dict[str, object]:
        self.sent.append(str(kwargs.get("text", "")))
        return {"ret": 0}


async def _scenario(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state = WechatState(tmp_path / "state")
    account = WechatAccount(
        bot_token="t",
        ilink_bot_id="bot",
        base_url="http://127.0.0.1",
        ilink_user_id="owner",
    )
    state.save_account(account)
    state.record_received("msg:1", sender="owner", context_token="ctx", text="hello")
    state.mark("msg:1", "received", received_at=NOW.timestamp())
    store = ScheduleStore(tmp_path / "schedule")
    called: list[str] = []

    async def execute(
        prompt: str, cancel: asyncio.Event, task_id: str
    ) -> HeadlessResult:
        called.append("executor")
        raise AssertionError("executor must not run")

    async def classify(text: str) -> ConversationDecision:
        called.append("classifier")
        raise AssertionError("classifier must not run")

    client = Client()
    service = _Service(
        config=ServeConfig(
            workspace=workspace,
            state_dir=tmp_path / "state",
            account=account,
            approval=None,
            allowed_senders=frozenset({"owner"}),
            schedule_store=tmp_path / "schedule",
        ),
        state=state,
        client=client,  # type: ignore[arg-type]
        executor=execute,
        report=__import__(
            "cairn.integrations.wechat.serve", fromlist=["ServeReport"]
        ).ServeReport(),
        stop_event=None,
        conversation_classifier=classify,
        schedule_store=store,
    )

    async def send(key: str, text: str) -> bool:
        state.mark(
            key,
            "received",
            received_at=NOW.timestamp(),
            sender="owner",
            context_token="ctx",
            text=text,
        )
        handled = service._try_host_once_schedule(
            _Inbound(key=key, sender="owner", context_token="ctx", text=text)
        )
        if service._intent_tasks:
            await asyncio.gather(*tuple(service._intent_tasks))
        return handled

    assert await send("msg:1", "十分钟后提醒我检查部署结果")
    assert await send("msg:2", "明天上午九点检查这个项目的CI\uff0c把结果发给我")
    assert await send("msg:2", "明天上午九点检查这个项目的CI\uff0c把结果发给我")
    jobs = store.load_jobs()
    assert len(jobs) == 2
    assert jobs[0].reminder is True and jobs[1].reminder is False
    assert called == []
    assert await send("msg:3", "不要创建任何提醒") is False
    assert await send("msg:4", "晚点提醒我") is True
    assert len(store.load_jobs()) == 2
    assert any("已创建定时任务" in item for item in client.sent)


def test_host_once_schedule_path(tmp_path: Path) -> None:
    asyncio.run(_scenario(tmp_path))
