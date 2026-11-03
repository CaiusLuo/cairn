import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

from cairn.infra.wechat.state import WechatAccount, WechatState
from cairn.terminal.wechat.session import WechatSessionController
from tests.support.fake_ilink import FakeILink

CHILD = r"""
import asyncio, os
from pathlib import Path
from cairn.infra.wechat.state import WechatAccount, WechatState
from cairn.terminal.headless import HeadlessResult, HeadlessStatus
from cairn.terminal.wechat.serve import ConversationDecision, ServeConfig, serve_wechat

async def classifier(text):
    return ConversationDecision.CHAT if "你好" in text else ConversationDecision.TASK
async def chat(question, sender, snapshot):
    return "chat:" + question
async def execute(prompt, cancel, task_id):
    return HeadlessResult(task_id=task_id, status=HeadlessStatus.COMPLETED, workspace=os.environ["WS"], final_response="done")
async def main():
    ws=Path(os.environ["WS"]); state=Path(os.environ["STATE"])
    account=WechatAccount(bot_token="t", ilink_bot_id="b", base_url=os.environ["URL"], ilink_user_id="u1")
    WechatState(state).save_account(account)
    cfg=ServeConfig(workspace=ws, state_dir=state, account=account, approval=None, allowed_senders=frozenset({"u1"}), max_polls=200, poll_interval=0.02)
    await serve_wechat(config=cfg, task_executor=execute, conversation_executor=chat, conversation_classifier=classifier, allow_insecure_local_http=True)
asyncio.run(main())
"""


def test_background_child_serves_and_detaches(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    script = tmp_path / "child.py"
    script.write_text(CHILD)
    with FakeILink() as fake:
        env = {
            **os.environ,
            "PYTHONPATH": str(Path.cwd() / "src"),
            "WS": str(workspace),
            "STATE": str(state_dir),
            "URL": fake.url,
        }
        proc = subprocess.Popen([sys.executable, str(script)], env=env)
        try:
            for _ in range(100):
                if (state_dir / "control.sock").exists():
                    break
                if proc.poll() is not None:
                    raise AssertionError("background child exited before readiness")
                time.sleep(0.05)
            assert (state_dir / "control.sock").exists()

            async def launcher(*args: object) -> tuple[int, str]:
                (state_dir / "control.sock").touch()
                return proc.pid, "child-session"

            controller = WechatSessionController(
                workspace=workspace,
                state_dir=state_dir,
                task_executor=lambda *args: None,  # type: ignore[arg-type]
                background_launcher=launcher,
                emit=lambda text: None,
            )
            account = WechatAccount(
                bot_token="t",
                ilink_bot_id="b",
                base_url=fake.url,
                ilink_user_id="u1",
            )
            action = asyncio.run(controller._start(account))
            assert "后台服务已启动" in action.message
            fake.updates.append(
                {
                    "ret": 0,
                    "msgs": [
                        {
                            "message_type": 1,
                            "from_user_id": "u1",
                            "context_token": "ctx-1",
                            "message_id": 1,
                            "item_list": [{"type": 1, "text_item": {"text": "你好"}}],
                        }
                    ],
                    "get_updates_buf": "c1",
                }
            )
            for _ in range(100):
                record = WechatState(state_dir).get_record("msg:1")
                if record is not None and record.get("state") == "chat_replied":
                    break
                time.sleep(0.05)
            record = WechatState(state_dir).get_record("msg:1")
            assert record is not None and record["state"] == "chat_replied"

            asyncio.run(controller.close())
            assert proc.poll() is None
            asyncio.run(controller._stop_background())
            proc.wait(timeout=5)
            assert proc.poll() is not None
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
