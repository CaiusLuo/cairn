"""Server composition of existing WeChat, memory and scheduler controllers.

Clients invoke these controls; neither a browser nor terminal owns an Agent,
receiver or scheduler. TaskService remains the sole execution owner.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections import deque
from pathlib import Path
from typing import Any, Literal

import httpx
import qrcode  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field

from cairn.infra.atomic_json import atomic_write
from cairn.infra.config.server import DEFAULT_SERVICES, Services
from cairn.infra.redaction import redact_value
from cairn.infra.runtime_catalog import RuntimeTask
from cairn.infra.runtime_client import RuntimeClient
from cairn.infra.schedule.store import ScheduleStore
from cairn.infra.skills import layered_skills
from cairn.infra.tools.skill import ReadSkillResourceTool, ReadSkillTool
from cairn.memory.commands import MemoryController
from cairn.memory.service import PersonalMemoryService
from cairn.memory.store import PersonalMemoryStore
from cairn.memory.tools import (
    MemoryCorrectTool,
    MemoryForgetTool,
    MemoryRememberTool,
    MemorySearchTool,
)
from cairn.runtime.agent import Agent
from cairn.runtime.lifecycle import await_owned, settle_owned
from cairn.runtime.scheduling.tools import (
    ScheduleCreateTool,
    ScheduleListTool,
    ScheduleManageTool,
)
from cairn.runtime.service_execution import ServiceExecution
from cairn.runtime.task_board.service import TaskBoardService
from cairn.runtime.task_service import TaskService
from cairn.terminal.headless import HeadlessResult, HeadlessStatus
from cairn.terminal.schedule.controller import ScheduleController
from cairn.terminal.task_board.controller import TaskBoardController
from cairn.terminal.wechat.session import WechatSessionController


class ControlState(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, frozen=True)
    version: Literal[1] = 1
    selected_model: str | None = None
    wechat_enabled: bool = False
    trusted_senders: tuple[str, ...] = Field(default=(), max_length=100)
    receipts: dict[str, dict[str, Any]] = Field(default_factory=dict, max_length=256)
    deliveries: dict[str, str] = Field(default_factory=dict, max_length=512)


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class ServerControls:
    def __init__(
        self,
        *,
        service: TaskService,
        execution: ServiceExecution,
        workspace: Path,
        state: Path,
        token: str,
        web: Any,
        port: int,
        wechat_state: Path,
        agent_home: Path,
        schedule_state: Path,
        services: Services = DEFAULT_SERVICES,
    ) -> None:
        self.service, self.execution = service, execution
        self.services = services
        self.workspace = workspace.resolve()
        for private in (wechat_state, agent_home, schedule_state):
            if private.is_symlink() or any(
                private.resolve() == root.resolve()
                or private.resolve().is_relative_to(root.resolve())
                or root.resolve().is_relative_to(private.resolve())
                for root in (workspace, execution.worktrees)
            ):
                raise ValueError(
                    "Management state must be private and outside writable roots"
                )
        self.path = state / "controls.json"
        if self.path.is_symlink():
            raise ValueError("Management state cannot be a symlink")
        self.state = (
            ControlState.model_validate_json(self.path.read_bytes())
            if self.path.exists()
            else ControlState()
        )
        self.memory = PersonalMemoryService(
            PersonalMemoryStore(agent_home),
            secret_values=(*execution.sensitive_values, token),
            enabled=services.memory,
        )
        self.memory_controller = MemoryController(self.memory)
        self.execution.protected_paths = (
            wechat_state.resolve(),
            agent_home.resolve(),
            schedule_state.resolve(),
        )
        self.service.default_model_id = self.state.selected_model
        self.notices: deque[str] = deque(maxlen=20)
        self.qr_image: str | None = None
        self.qr_content: str | None = None
        self.lock = asyncio.Lock()
        self.operations: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self.memory_queue: asyncio.Queue[RuntimeTask] = asyncio.Queue(maxsize=64)
        self.memory_worker: asyncio.Task[None] | None = None
        self.delivery_tasks: set[asyncio.Task[None]] = set()
        self.delivery: Any | None = None
        self.closed = False
        api = RuntimeClient(
            f"http://127.0.0.1:{port}",
            token,
            transport=httpx.ASGITransport(app=web),
            workspace=workspace,
        )

        async def reject_local(*args: Any, **kwargs: Any) -> HeadlessResult:
            raise RuntimeError("Management cannot start a local Agent")

        self.wechat = WechatSessionController(
            workspace=workspace,
            state_dir=wechat_state,
            task_executor=reject_local,
            runtime_client=api,
            discover_background=False,
            qr_handler=self._qr,
            emit=self._notice,
            redact_text=self.memory.redact,
            on_account_changed=self._account_changed,
        )
        self.wechat.restore_senders(self.state.trusted_senders)
        self.schedules = ScheduleController(
            workspace=workspace,
            store=ScheduleStore(schedule_state),
            executor=self._scheduled,
            deliver=self.wechat.deliver,
            deliver_for_account=self.wechat.deliver_for_account,
            recipient=self.wechat.recipient_id,
            account_id=self.wechat.account_id,
            emit=self._notice,
            result_redactor=self.memory.redact,
        )
        self.execution.configure_agent = self.configure_agent
        self.execution.validate_task = self.validate_task
        self.service.validate_submission = self.validate_task
        self.service.on_settled = self.observe_settled

    def _notice(self, text: str) -> None:
        self.notices.append(self.memory.redact(text))

    def _refresh_secrets(self) -> None:
        secrets = self.wechat.secret_values()
        self.memory.add_secret_values(secrets)
        self.execution.protect_secrets(secrets)
        self.service.catalogue.sensitive_values = self.execution.sensitive_values

    def _account_changed(self) -> None:
        self._refresh_secrets()
        self.qr_image = None
        self.qr_content = None
        if self.delivery is not None:
            self.delivery.rescan()

    def _qr(self, qr: Any) -> None:
        if qr is None:
            self.qr_image = None
            self.qr_content = None
            return
        code = qrcode.QRCode(border=4)
        self.qr_content = qr.image_content or qr.qrcode
        code.add_data(self.qr_content)
        code.make(fit=True)
        matrix = code.get_matrix()
        size = len(matrix)
        cells = " ".join(
            f"M{x},{y}h1v1h-1z"
            for y, row in enumerate(matrix)
            for x, filled in enumerate(row)
            if filled
        )
        svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}"><path fill="white" d="M0 0h{size}v{size}H0z"/><path fill="black" d="{cells}"/></svg>'
        self.qr_image = (
            "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()
        )

    def _save(self, **updates: Any) -> None:
        self.state = ControlState.model_validate({**self.state.model_dump(), **updates})
        atomic_write(self.path, self.state.model_dump(mode="json"))

    async def start(self) -> None:
        if self.services.memory:
            self.memory_worker = asyncio.create_task(self._journal())
        if self.services.schedule:
            await asyncio.to_thread(self.schedules.store.load_jobs)
            await self.schedules.start()
        self._refresh_secrets()
        if self.services.wechat and self.state.wechat_enabled:
            if not self.wechat.account_id():
                raise RuntimeError(
                    "WeChat is enabled but account credentials are missing"
                )
            action = await self.wechat.command("")
            if action.tone != "info":
                raise RuntimeError("WeChat startup refused: " + action.message)
            self._notice(action.message)

    async def close(self) -> None:
        self.closed = True
        if self.operations:
            pending = asyncio.create_task(self._finish_operations())
            await await_owned(pending)
        await self.schedules.close()
        await self.wechat.close()
        if self.memory_worker is not None:
            await self.memory_queue.join()
            await settle_owned(self.memory_worker)
        if self.delivery_tasks:
            await settle_owned(*tuple(self.delivery_tasks))

    async def _finish_operations(self) -> None:
        await asyncio.gather(*tuple(self.operations.values()), return_exceptions=True)

    def commands(self) -> list[dict[str, Any]]:
        return [
            {"name": "/new", "description": "新对话", "children": []},
            {"name": "/develop", "description": "发起开发任务", "children": []},
            {"name": "/use", "description": "继续已有任务", "children": []},
            {"name": "/status", "description": "当前任务状态", "children": []},
            {"name": "/history", "description": "对话历史", "children": []},
            {"name": "/continue", "description": "安全继续任务", "children": []},
            {"name": "/cancel", "description": "取消任务", "children": []},
            {
                "name": "/wx",
                "description": "微信登录与接收",
                "children": [
                    "status",
                    "login",
                    "verify",
                    "allow",
                    "stop",
                    "disconnect",
                    "help",
                ],
            },
            {
                "name": "/schedule",
                "description": "Server 定时任务",
                "children": [
                    "list",
                    "history",
                    "add",
                    "remind",
                    "pause",
                    "resume",
                    "remove",
                    "help",
                ],
            },
            {
                "name": "/memory",
                "description": "共享记忆与规则审批",
                "children": [
                    "show",
                    "agent",
                    "note",
                    "distill",
                    "pending",
                    "propose",
                    "propose-auto",
                    "approve",
                    "reject",
                    "rollback",
                    "files",
                ],
            },
            {
                "name": "/model",
                "description": "Server 批准的模型",
                "children": ["list", "use"],
            },
            {
                "name": "/provider",
                "description": "Server Provider 配置 (切换需宿主批准)",
                "children": ["list"],
            },
            {
                "name": "/task",
                "description": "共享任务",
                "children": [
                    "list",
                    "show",
                    "add",
                    "use",
                    "continue",
                    "cancel",
                    "todo",
                    "done",
                    "undo",
                    "status",
                    "progress",
                ],
            },
            {"name": "/skill", "description": "项目技能", "children": ["list"]},
            {"name": "/mcp", "description": "MCP 授权状态", "children": ["list"]},
            {
                "name": "/compact",
                "description": "自动压缩说明 (手动命令尚未迁移)",
                "children": [],
            },
            {"name": "/help", "description": "命令说明", "children": []},
            {"name": "/exit", "description": "关闭客户端", "children": []},
        ]

    async def snapshot(self, section: str = "summary") -> dict[str, Any]:
        if section not in {"summary", "wechat", "schedule", "memory"}:
            raise ValueError("Unknown management section")
        result = await asyncio.to_thread(self._snapshot, section)
        # Async receiver/login state is observed on its owning event loop.
        if section in {"summary", "wechat"}:
            result["wechat"] = {
                **self.wechat.management_status(),
                "enabled": self.state.wechat_enabled,
                "qr_image": self.qr_image,
                "qr_content": self.qr_content,
                "trusted_senders": list(self.state.trusted_senders),
            }
        return result

    def _snapshot(self, section: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "workspace": str(self.workspace),
            "notices": list(self.notices),
            "pending_operations": [
                {"id": key, "command": row["command"]}
                for key, row in self.state.receipts.items()
                if row["state"] == "pending"
            ],
        }
        if section == "memory":
            data = redact_value(self.memory.status(), self.execution.sensitive_values)
            result["memory"] = {
                **data,
                "enabled": self.memory.enabled,
                "proposal_digest": digest(data["pending"]) if data["pending"] else None,
            }
        if section == "schedule":
            owner, account = self.wechat.recipient_id(), self.wechat.account_id()
            jobs = [
                job.model_dump(mode="json")
                for job in self.schedules.store.load_jobs()
                if job.workspace == str(self.workspace)
                and job.recipient == owner
                and job.account_id == account
            ]
            runs = [
                row
                for row in self.schedules.store.load_runs(limit=50)
                if row.get("workspace") == str(self.workspace)
                and row.get("recipient") == owner
                and row.get("account_id") == account
            ]
            result["schedule"] = {
                "jobs": jobs,
                "runs": runs,
                "owner_available": bool(owner and account),
            }
        return result

    async def command(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.closed:
            raise RuntimeError("Server is stopping")
        is_wechat_task = (
            request.get("source") == "wechat" and request.get("task_id") is not None
        )
        if is_wechat_task:
            # Authorization is checked before a receipt can be used.  A sender
            # whose access was revoked cannot replay an old successful request.
            try:
                self._require_wechat_task(
                    str(request["task_id"]),
                    request.get("sender"),
                    request.get("account"),
                )
            except KeyError:
                return {"ok": False, "message": "未找到对应的任务。"}
        name, args = request["command"], request.get("args", "")
        key = request["request_id"]
        fingerprint = digest({k: v for k, v in request.items() if k != "request_id"})
        if key in self.operations:
            receipt = self.state.receipts.get(key)
            if receipt and receipt["digest"] != fingerprint:
                raise ValueError("Request ID reused for different input")
            return await asyncio.shield(self.operations[key])
        receipt = self.state.receipts.get(key)
        if receipt:
            if receipt["digest"] != fingerprint:
                raise ValueError("Request ID reused for different input")
            if receipt["state"] == "pending":
                raise RuntimeError(
                    "上次管理操作结果不明; 先检查微信/计划/记忆状态, 不会自动重放。"
                )
            return dict(receipt["result"])
        readonly = (
            name in {"/help", "/provider", "/skill", "/mcp", "/trace", "/compact"}
            or (name == "/model" and (not args or args == "list"))
            or (name == "/wx" and args in {"status", "help"})
            or (
                name == "/schedule"
                and (not args or args.split()[0] in {"list", "history", "help"})
            )
            or (
                name == "/memory"
                and (
                    not args
                    or args.split()[0]
                    in {"show", "status", "agent", "pending", "files"}
                )
            )
            or (name == "/task" and args.split()[:1] in [[], ["list"], ["show"]])
            or (
                is_wechat_task and name in {"/status", "/task", "/history", "/recovery"}
            )
        )
        if readonly:
            return await self._execute(request)
        operation = asyncio.create_task(self._mutate(request, fingerprint))
        self.operations[key] = operation
        operation.add_done_callback(lambda task: self._finished_operation(key, task))
        return await asyncio.shield(operation)

    def _finished_operation(self, key: str, task: asyncio.Task[dict[str, Any]]) -> None:
        self.operations.pop(key, None)
        if not task.cancelled():
            task.exception()

    async def _mutate(
        self, request: dict[str, Any], fingerprint: str
    ) -> dict[str, Any]:
        async with self.lock:
            key = request["request_id"]
            receipts = dict(self.state.receipts)
            while len(receipts) >= 256:
                oldest = next(
                    (k for k, row in receipts.items() if row["state"] == "completed"),
                    None,
                )
                if oldest is None:
                    raise RuntimeError("管理操作记录需要先人工检查")
                del receipts[oldest]
            receipts[key] = {
                "command": request["command"],
                "digest": fingerprint,
                "state": "pending",
            }
            self._save(receipts=receipts)
            result = await self._execute(request)
            receipts = {
                **self.state.receipts,
                key: {**receipts[key], "state": "completed", "result": result},
            }
            self._save(receipts=receipts)
            return result

    async def _execute(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("source") == "wechat" and request.get("task_id") is not None:
            return await self._wechat_task_command(request)
        name, args = request["command"], request.get("args", "")
        source = request.get("source", "web")
        service = {"/wx": "wechat", "/schedule": "schedule", "/memory": "memory"}.get(
            name
        )
        if service and not getattr(self.services, service):
            return {
                "ok": False,
                "message": service + " is disabled in Server configuration",
            }
        if source == "wechat":
            if (
                request.get("sender") != self.wechat.recipient_id()
                or request.get("account") != self.wechat.account_id()
            ):
                return {
                    "ok": False,
                    "message": "只有当前微信账号绑定的用户可管理自己的计划和记忆。",
                }
            if name not in {"/schedule", "/memory", "/help"} or (
                name == "/memory" and args.split()[:1] in [["approve"], ["rollback"]]
            ):
                return {
                    "ok": False,
                    "message": "规则审批与微信连接管理请在 CLI 或 Web 操作。",
                }
        if name == "/wx":
            action = await self.wechat.command(args)
            if action.tone == "info":
                verb = args.split()[:1]
                if verb in [[], ["login"]]:
                    self._save(wechat_enabled=True)
                elif verb in [["stop"], ["disconnect"], ["logout"]]:
                    self._save(wechat_enabled=False)
                if verb in [["disconnect"], ["logout"], ["reset"]]:
                    self._save(trusted_senders=())
                elif verb == ["allow"] and len(args.split()) == 2:
                    self._save(
                        trusted_senders=tuple(
                            sorted(set(self.state.trusted_senders) | {args.split()[1]})
                        )
                    )
        elif name == "/memory":
            if args.split()[:1] in [["pending"], ["review"]]:
                pending = await asyncio.to_thread(self.memory.pending_proposal)
                return {
                    "ok": True,
                    "message": self.memory.redact(
                        MemoryController.format_pending(pending).message
                    ),
                    "proposal_digest": digest(pending) if pending else None,
                }
            pending = None
            if args.split()[:1] == ["approve"]:
                pending = self.memory.pending_proposal()
                if (
                    not request.get("confirmed")
                    or not pending
                    or request.get("expected_proposal") != digest(pending)
                ):
                    return {
                        "ok": False,
                        "message": "先查看当前候选规则, 再明确确认这份候选; 候选变化后需要重新查看。",
                    }
            if pending is not None:
                work = asyncio.create_task(
                    asyncio.to_thread(
                        self.memory_controller.execute, args, expected_proposal=pending
                    )
                )
            else:
                work = asyncio.create_task(
                    asyncio.to_thread(self.memory_controller.execute, args)
                )
            late = await await_owned(work)
            action = work.result()
            if late:
                raise late
        elif name == "/schedule":
            work = asyncio.create_task(asyncio.to_thread(self.schedules.execute, args))
            late = await await_owned(work)
            action = work.result()
            if late:
                raise late
        elif name == "/help":
            return {
                "ok": True,
                "message": "\n".join(
                    f"{row['name']}  {row['description']}" for row in self.commands()
                ),
            }
        elif name == "/management":
            parts = args.split()
            if (
                len(parts) != 2
                or parts[0] != "acknowledge"
                or not request.get("confirmed")
            ):
                return {
                    "ok": False,
                    "message": "检查操作实际结果后, 使用 /management acknowledge REQUEST_ID 确认不再重放。",
                }
            receipt = self.state.receipts.get(parts[1])
            if (
                not receipt
                or receipt["state"] != "pending"
                or parts[1] in self.operations
            ):
                raise ValueError("No interrupted operation to acknowledge")
            result = {"ok": True, "message": "已记录人工核对, 没有重放原操作。"}
            self._save(
                receipts={
                    **self.state.receipts,
                    parts[1]: {**receipt, "state": "completed", "result": result},
                }
            )
            return result
        elif name in {"/model", "/provider"}:
            profile = self.execution.provider_runtime
            if profile is None:
                return {"ok": True, "message": "Server 当前使用离线验收模型。"}
            current = profile.model_manager.current_model()
            parts = args.split()
            if name == "/model" and parts[:1] == ["use"] and len(parts) == 2:
                ident = parts[1]
                if ident == current.name:
                    ident = current.model_ids[0]
                if ident not in current.model_ids:
                    return {
                        "ok": False,
                        "message": "模型不在宿主已批准的组内。批准列表: "
                        + ", ".join(current.model_ids),
                    }
                task_id = request.get("task_id")
                if task_id:
                    task = self.service.inspect(task_id)
                    if task.status not in {"completed", "paused"}:
                        raise ValueError("Only an idle clean task can change model")
                    self.execution.profile_for(
                        task.model_copy(update={"model_id": ident})
                    )
                    self.service.record(
                        task.model_copy(update={"model_id": ident}), "model_selected"
                    )
                self._save(selected_model=ident)
                self.service.default_model_id = ident
                return {
                    "ok": True,
                    "message": "后续请求使用 " + ident,
                    "task_id": task_id,
                }
            if args and args != "list":
                return {
                    "ok": False,
                    "message": "此操作未执行。Provider 和模型配置增删须由宿主修改 models.toml 并重新批准、重启 Server; /model use 可切换已批准组内的模型。",
                }
            return {
                "ok": True,
                "message": "Provider: "
                + profile.provider.name
                + "\n已批准模型: "
                + ", ".join(current.model_ids)
                + "\n默认: "
                + str(self.service.default_model_id or current.model_ids[0]),
            }
        elif name == "/task":
            parts = args.split()
            if self.service.board is not None and (
                not parts
                or parts[0]
                in {"list", "add", "todo", "done", "undo", "status", "progress", "help"}
                or (
                    parts[0] in {"show", "use"}
                    and len(parts) == 2
                    and parts[1] not in self.service.catalogue.data.tasks
                )
            ):
                board = TaskBoardService(
                    self.service.board.store, project=str(self.workspace)
                )
                action = await TaskBoardController(board).command(args)
                return {"ok": action.tone == "info", "message": action.message}

            if not parts or parts[0] == "list":
                return {
                    "ok": True,
                    "message": "\n".join(
                        f"{t.task_id} {t.status} {t.prompt[:60]}"
                        for t in self.service.list_tasks()[-30:]
                    )
                    or "暂无任务。",
                }
            ident = parts[1] if len(parts) > 1 else request.get("task_id")
            task = self.service.inspect(ident or "")
            if parts[0] in {"show", "use"}:
                return {
                    "ok": True,
                    "message": f"{task.task_id} · {task.status}\n{task.result or task.diagnostic or '任务处理中'}",
                    "task_id": task.task_id,
                }
            if parts[0] == "continue":
                task = self.service.continue_task(task.task_id)
            elif parts[0] == "cancel":
                task = await self.service.cancel(task.task_id)
            else:
                return {
                    "ok": False,
                    "message": "/task list|show|use|continue|cancel TASK_ID; 开发任务用 /develop GOAL。",
                }
            return {
                "ok": True,
                "message": f"{task.task_id} · {task.status}",
                "task_id": task.task_id,
            }
        elif name == "/skill":
            skills = layered_skills(self.workspace)
            if not args or args == "list":
                return {
                    "ok": True,
                    "message": "\n".join(
                        f"{s.name}: {s.description}" for s in skills.summaries()
                    )
                    or "没有项目技能。",
                }
            skill = skills.read(args)
            return {
                "ok": bool(skill),
                "message": skill.body if skill else "找不到该技能。",
            }
        elif name == "/mcp":
            from cairn.infra.mcp.config import load_mcp_servers

            servers = load_mcp_servers(self.workspace)
            return {
                "ok": True,
                "message": "\n".join(server.name for server in servers)
                + "\nMCP 执行须由 Server 宿主授权, 客户端不能开启额外进程或网络权限。",
            }
        elif name == "/compact":
            return {
                "ok": True,
                "message": "共享模式由 Server 按预算自动压缩。手动 /compact 尚未迁移, 本次没有执行压缩。",
            }
        elif name == "/trace":
            task = self.service.inspect(request.get("task_id") or "")
            return {
                "ok": True,
                "message": f"状态: {task.status}\n"
                + json.dumps(task.metrics, ensure_ascii=False),
            }
        else:
            return {
                "ok": False,
                "message": "未知命令; 使用 /help 查看当前 Server 实际支持的命令。",
            }
        self._refresh_secrets()
        return {
            "ok": action.tone == "info",
            "message": self.memory.redact(action.message),
        }

    def _wechat_sender_allowed(self, sender: str | None, account: str | None) -> bool:
        return bool(
            sender
            and account
            and account == self.wechat.account_id()
            and (
                sender == self.wechat.recipient_id()
                or sender in self.state.trusted_senders
            )
        )

    def _require_wechat_task(
        self, task_id: str, sender: str | None, account: str | None
    ) -> RuntimeTask:
        if not self.services.wechat or not self._wechat_sender_allowed(sender, account):
            raise KeyError(task_id)
        return self.service.inspect_wechat_task(task_id, sender=sender, account=account)

    async def _wechat_task_command(self, request: dict[str, Any]) -> dict[str, Any]:
        task_id = request["task_id"]
        sender, account = request.get("sender"), request.get("account")
        command = request["command"]
        try:
            task = self._require_wechat_task(task_id, sender, account)
            if command == "/cancel":
                await self.service.cancel(task_id)
                task = self._require_wechat_task(task_id, sender, account)
        except KeyError:
            return {"ok": False, "message": "未找到对应的任务。"}

        if command == "/history":
            reader = self.service.history_reader
            return {"ok": True, "history": reader(task) if reader else []}
        if command == "/recovery":
            return {"ok": True, "recovery": self.service.recovery(task_id)}
        if command == "/reply":
            prompt = request.get("args", "").strip()
            if not prompt:
                raise ValueError("A task reply requires a prompt")
            task = self.service.message(
                task_id,
                prompt,
                "wechat",
                request["request_id"],
                task.sequence,
                delivery_recipient=sender,
                delivery_account=account,
            )
        elif command == "/continue":
            task = self.service.continue_task(task_id)
        elif command not in {"/status", "/task", "/cancel"}:
            raise ValueError("Unknown WeChat task command")
        return {"ok": True, "task": task.model_dump(mode="json")}

    def validate_task(self, task: RuntimeTask) -> None:
        if (task.delivery_recipient is None) != (task.delivery_account is None):
            raise ValueError("Task delivery requires both recipient and account")
        if task.delivery_recipient is not None and not self._wechat_sender_allowed(
            task.delivery_recipient, task.delivery_account
        ):
            raise ValueError("Task delivery identity is not authorized")

    def configure_agent(self, agent: Agent, task: RuntimeTask) -> None:
        # An extra trusted project sender is not the personal-memory owner.
        personal = (
            task.delivery_recipient is None
            or task.delivery_recipient == self.wechat.recipient_id()
        )
        if personal and self.services.memory:
            agent.personal_context = "\n\n".join(
                filter(None, (agent.personal_context, self.memory.render_context()))
            )
            for tool in (
                MemorySearchTool(self.memory),
                MemoryRememberTool(self.memory),
                MemoryForgetTool(self.memory),
                MemoryCorrectTool(self.memory),
            ):
                agent.tools.register_tool(tool)
        if personal and self.services.schedule:
            for kind in (ScheduleCreateTool, ScheduleListTool, ScheduleManageTool):
                agent.tools.register_tool(
                    kind(
                        store=self.schedules.store,
                        workspace=str(self.workspace),
                        recipient=self.wechat.recipient_id,
                        account_id=self.wechat.account_id,
                        redact=self.memory.redact,
                        require_trusted_message=True,
                    )
                )
        library = layered_skills(
            Path(task.worktree) if task.worktree else self.workspace
        )
        agent.skill_library = library
        agent.tools.register_tool(ReadSkillTool(library))
        agent.tools.register_tool(ReadSkillResourceTool(library))

    async def _scheduled(
        self, prompt: str, cancel: asyncio.Event, ident: str, *, run_key: str = ""
    ) -> HeadlessResult:
        task = self.service.submit(
            prompt, "schedule", request_id="schedule:" + run_key, mode="conversation"
        )
        while task.status in {"queued", "running"}:
            await self.service.follow(task.task_id, task.sequence)
            task = self.service.inspect(task.task_id)
        return self._outcome(task)

    def _outcome(self, task: RuntimeTask) -> HeadlessResult:
        status = {
            "completed": HeadlessStatus.COMPLETED,
            "paused": HeadlessStatus.BUDGET_EXHAUSTED,
            "cancelled": HeadlessStatus.CANCELLED,
        }.get(task.status, HeadlessStatus.RUNTIME_ERROR)
        return HeadlessResult(
            task_id=task.task_id,
            status=status,
            workspace=task.worktree or str(self.workspace),
            final_response=task.result,
            error_category=task.diagnostic,
        )

    def observe_settled(self, task: RuntimeTask) -> None:
        if self.closed:
            return
        if self.services.wechat and task.delivery_recipient:
            if len(self.delivery_tasks) >= 32:
                self._notice("微信推送繁忙, 结果已保留在任务历史, 可用 /status 查询。")
            else:
                delivery = asyncio.create_task(self._deliver(task))
                self.delivery_tasks.add(delivery)
                delivery.add_done_callback(self._delivery_finished)
        if not self.services.memory:
            return
        try:
            self.memory_queue.put_nowait(task)
        except asyncio.QueueFull:
            self._notice("记忆日志队列已满, 本次结果仍保留在任务历史中。")

    def _delivery_finished(self, task: asyncio.Task[None]) -> None:
        self.delivery_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self._notice("微信推送未确认: " + type(task.exception()).__name__)

    async def _journal(self) -> None:
        while True:
            task = await self.memory_queue.get()
            try:
                if (
                    task.delivery_recipient is None
                    or task.delivery_recipient == self.wechat.recipient_id()
                ):
                    await asyncio.to_thread(
                        self.memory.record_task,
                        prompt=task.input_prompt or task.prompt,
                        result=self._outcome(task),
                        task_id=task.task_id,
                    )
            except Exception as error:
                self._notice("记忆日志未保存: " + type(error).__name__)
            finally:
                self.memory_queue.task_done()

    async def _deliver(self, task: RuntimeTask) -> None:
        key = task.task_id + ":" + str(task.attempts)
        if key in self.state.deliveries:
            return
        rows = dict(self.state.deliveries)
        while len(rows) >= 512:
            del rows[next(iter(rows))]
        rows[key] = "started"
        self._save(deliveries=rows)
        try:
            self.validate_task(task)
            if task.mode == "conversation":
                body = task.result or task.diagnostic or "本轮没有生成可显示回复。"
            elif task.status == "completed":
                body = "开发任务已完成\n" + (task.result or "")
            elif task.status in {"paused", "recovery_required", "failed"}:
                body = f"开发任务状态: {task.status}\n{task.result or task.diagnostic or ''}"
            else:
                body = f"开发任务状态: {task.status}"
            sent = await self.wechat.deliver_for_account(
                task.delivery_recipient or "",
                body,
                task.delivery_account or "",
            )
            self._save(
                deliveries={
                    **self.state.deliveries,
                    key: "sent" if sent else "unconfirmed",
                }
            )
        except (Exception, asyncio.CancelledError):
            self._save(deliveries={**self.state.deliveries, key: "unconfirmed"})
