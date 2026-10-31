"""Terminal commands for personal memory and AGENT.md evolution."""

from __future__ import annotations

from collections.abc import Callable

from cairn.core.agent_memory import MemoryKind
from cairn.runtime.agent_memory.service import PersonalMemoryService
from cairn.terminal.action import Action, correction, note


class MemoryController:
    def __init__(
        self,
        service: PersonalMemoryService,
        *,
        emit: object | None = None,
        on_agent_changed: Callable[[], None] | None = None,
    ) -> None:
        self.service = service
        self.emit = emit
        self.on_agent_changed = on_agent_changed

    async def command(self, args: str) -> Action:
        parts = args.split()
        verb = parts[0].lower() if parts else "show"
        if verb in {"show", "status"}:
            return self._show()
        if verb in {"pending", "review"}:
            return self._pending()
        if verb == "agent":
            return note(
                self.service.store.read_agent_md().strip() or "(AGENT.md is empty)"
            )
        if verb == "note":
            text = " ".join(parts[1:]).strip()
            if not text:
                return correction("用法: /memory note <要记住的内容>")
            try:
                self.service.record_note(text, kind=MemoryKind.PREFERENCE)
            except (OSError, ValueError) as exc:
                return correction(f"写入记忆失败: {exc}")
            if self.on_agent_changed is not None:
                self.on_agent_changed()
            return note("已记录到今天的每日记忆. ")
        if verb == "distill":
            days = 7
            if len(parts) > 1:
                try:
                    days = int(parts[1])
                except ValueError:
                    return correction("用法: /memory distill [天数]")
            try:
                count = self.service.distill(days=days)
            except (OSError, ValueError) as exc:
                return correction(f"提炼失败: {exc}")
            return note(
                f"已提炼 {count} 条到 MEMORY.md. " if count else "没有可提炼的新内容. "
            )
        if verb in {"propose-auto", "propose_auto"}:
            days = 7
            if len(parts) > 1:
                try:
                    days = int(parts[1])
                except ValueError:
                    return correction("用法: /memory propose-auto [天数]")
            try:
                self.service.propose_from_memory(days=days)
            except (OSError, ValueError) as exc:
                return correction(f"生成 AGENT.md 候选失败: {exc}")
            return note(
                "已根据每日记忆生成 AGENT.md 候选, 来源见提案; "
                "使用 /memory approve 应用或 /memory reject 放弃。"
            )
        if verb == "propose":
            text = " ".join(parts[1:]).strip()
            if not text:
                return correction("用法: /memory propose <希望加入 AGENT.md 的内容>")
            try:
                self.service.propose_agent_update(text, "user-requested")
            except (OSError, ValueError) as exc:
                return correction(f"创建 AGENT.md 提案失败: {exc}")
            return note(
                "已创建 AGENT.md 提案; 使用 /memory approve 应用, /memory reject 放弃. "
            )
        if verb == "approve":
            try:
                self.service.approve_agent_update()
            except (OSError, ValueError) as exc:
                return correction(f"应用提案失败: {exc}")
            if self.on_agent_changed is not None:
                self.on_agent_changed()
            return note("AGENT.md 提案已应用, 并写入审计记录. ")
        if verb == "reject":
            if self.service.pending_proposal() is None:
                return note("当前没有待处理的 AGENT.md 提案. ")
            self.service.store.clear_proposal()
            return note("已放弃待处理的 AGENT.md 提案. ")
        if verb == "rollback":
            try:
                changed = self.service.rollback_agent_update()
            except (OSError, ValueError) as exc:
                return correction(f"回滚失败: {exc}")
            if changed and self.on_agent_changed is not None:
                self.on_agent_changed()
            return note(
                "已回滚最近一次 AGENT.md 修改. " if changed else "没有可回滚的历史. "
            )
        if verb == "files":
            root = self.service.store.root
            return note(f"AGENT.md: {root / 'AGENT.md'}\nmemory: {root / 'memory'}")
        return correction(
            "未知 /memory 子命令; 使用 /memory show|agent|note|distill|propose|approve|reject|rollback|files. "
        )

    def _pending(self) -> Action:
        pending = self.service.pending_proposal()
        if not isinstance(pending, dict):
            return note("当前没有待处理的 AGENT.md 提案。")
        return note(
            "待审批 AGENT.md 候选\n"
            f"reason: {pending.get('reason', '(none)')}\n"
            f"created: {pending.get('created_at', '(unknown)')}\n"
            "---\n"
            f"{pending.get('text', '')!s}"
        )

    def _show(self) -> Action:
        try:
            status = self.service.status()
        except (OSError, ValueError) as exc:
            return correction(f"读取个人记忆失败: {exc}")
        pending = status["pending"]
        lines = [
            f"个人记忆目录: {self.service.store.root}",
            f"AGENT.md: {len(str(status['agent_md']))} chars",
            f"MEMORY.md: {len(str(status['memory_md']))} chars",
            f"每日记忆文件: {status['daily_days']} 天",
        ]
        if isinstance(pending, dict):
            lines.append(f"待审批 AGENT.md 提案: {pending.get('text', '')!s}")
        return note("\n".join(lines))
