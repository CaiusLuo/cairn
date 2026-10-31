"""Agent-facing schedule tools.

The model may propose a schedule, but a job is persisted only after the user
accepts the brief. The host supplies workspace/owner/account; model arguments
never choose another owner.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import uuid4

from cairn.core.models import ToolResult
from cairn.core.schedule import ScheduleJob, ScheduleKind, initial_next_run
from cairn.infra.schedule.store import ScheduleStore
from cairn.infra.tools.base import InvalidArguments, ToolExecutionContext
from cairn.runtime.scheduling.runner import current_time


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidArguments(f"{name} must be a non-empty string")
    return value.strip()


class _ScheduleTool:
    def __init__(
        self,
        *,
        store: ScheduleStore,
        workspace: str,
        recipient: Callable[[], str | None],
        account_id: Callable[[], str | None],
        clock: Callable[[], datetime] = current_time,
        redact: Callable[[str], str] | None = None,
    ) -> None:
        self.store = store
        self.workspace = workspace
        self.recipient = recipient
        self.account_id = account_id
        self.clock = clock
        self.redact = redact or (lambda text: text)

    def _owner(self) -> tuple[str, str]:
        owner = self.recipient()
        account = self.account_id()
        if not owner or not account:
            raise InvalidArguments(
                "WeChat owner is not available; finish /wx login first"
            )
        return owner, account

    def _owned(self, job: ScheduleJob) -> bool:
        owner, account = self._owner()
        return (
            job.workspace == self.workspace
            and job.recipient == owner
            and job.account_id == account
        )

    @staticmethod
    def _result(text: str, *, ok: bool = True) -> ToolResult:
        return ToolResult(
            stdout=text, stderr="" if ok else text, exit_code=0 if ok else 1
        )


class ScheduleCreateTool(_ScheduleTool):
    name = "schedule_create"
    description = (
        "Create a future or recurring task after the user has accepted a brief. "
        "Call once with confirmed=false to obtain the brief, show it to the user, "
        "and only call with confirmed=true after an explicit confirmation."
    )

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string"},
                        "kind": {
                            "type": "string",
                            "enum": ["once", "daily", "interval"],
                        },
                        "when": {
                            "type": "string",
                            "description": "ISO datetime for once, HH:MM for daily, or seconds for interval",
                        },
                        "timezone": {"type": "string", "default": "Asia/Shanghai"},
                        "confirmed": {"type": "boolean", "default": False},
                    },
                    "required": ["prompt", "kind", "when"],
                    "additionalProperties": False,
                },
            },
        }

    def validate(self, arguments: dict[str, Any]) -> None:
        if (
            not isinstance(arguments.get("prompt"), str)
            or not arguments["prompt"].strip()
        ):
            raise InvalidArguments("prompt must be a non-empty string")
        if arguments.get("kind") not in {"once", "daily", "interval"}:
            raise InvalidArguments("kind must be once, daily or interval")
        if not isinstance(arguments.get("when"), str) or not arguments["when"].strip():
            raise InvalidArguments("when must be a non-empty string")
        if not isinstance(arguments.get("timezone", "Asia/Shanghai"), str):
            raise InvalidArguments("timezone must be a string")
        if not isinstance(arguments.get("confirmed", False), bool):
            raise InvalidArguments("confirmed must be a boolean")

    def _shape(
        self, arguments: dict[str, Any]
    ) -> tuple[ScheduleKind, datetime | None, str | None, int | None, datetime]:
        kind = ScheduleKind(arguments["kind"])
        when = arguments["when"].strip()
        at = daily_time = interval_seconds = None
        if kind is ScheduleKind.ONCE:
            at = datetime.fromisoformat(when)
        elif kind is ScheduleKind.DAILY:
            daily_time = when
        else:
            interval_seconds = int(when)
        next_run = initial_next_run(
            kind,
            now=self.clock(),
            at=at,
            daily_time=daily_time,
            interval_seconds=interval_seconds,
            timezone=arguments.get("timezone", "Asia/Shanghai"),
        )
        return kind, at, daily_time, interval_seconds, next_run

    async def execute(
        self, arguments: dict[str, Any], *, context: ToolExecutionContext | None = None
    ) -> ToolResult:
        self.validate(arguments)
        owner, account = self._owner()
        try:
            kind, at, daily_time, interval_seconds, next_run = self._shape(arguments)
        except (ValueError, TypeError) as exc:
            return self._result(f"invalid schedule: {exc}", ok=False)
        prompt = self.redact(arguments["prompt"].strip())[:8000]
        brief = (
            f"Schedule brief:\n"
            f"- task: {prompt}\n"
            f"- kind: {kind.value}\n"
            f"- timezone: {arguments.get('timezone', 'Asia/Shanghai')}\n"
            f"- next run: {next_run.isoformat()}\n"
            f"- delivery: WeChat owner {owner}\n"
            f"- note: recurring jobs may spend model requests and send messages.\n"
            "Ask the user to confirm or edit this brief before creating it."
        )
        if not arguments.get("confirmed", False):
            return self._result(brief)
        job = ScheduleJob(
            job_id="job-" + uuid4().hex[:12],
            name=prompt[:60],
            prompt=prompt,
            workspace=self.workspace,
            recipient=owner,
            account_id=account,
            kind=kind,
            at=at,
            daily_time=daily_time,
            interval_seconds=interval_seconds,
            timezone=arguments.get("timezone", "Asia/Shanghai"),
            created_at=self.clock(),
            next_run_at=next_run,
        )
        try:
            self.store.add_job(job)
        except (OSError, ValueError) as exc:
            return self._result(f"could not save schedule: {exc}", ok=False)
        return self._result(f"Created {job.job_id}; next run {next_run.isoformat()}.")


class ScheduleListTool(_ScheduleTool):
    name = "schedule_list"
    description = "List scheduled tasks owned by the current user."

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        }

    def validate(self, arguments: dict[str, Any]) -> None:
        if arguments:
            raise InvalidArguments("schedule_list takes no arguments")

    async def execute(
        self, arguments: dict[str, Any], *, context: ToolExecutionContext | None = None
    ) -> ToolResult:
        self.validate(arguments)
        jobs = [job for job in self.store.load_jobs() if self._owned(job)]
        if not jobs:
            return self._result("No scheduled tasks for this owner.")
        lines = []
        for job in jobs:
            state = "enabled" if job.enabled else "paused"
            lines.append(
                f"{job.job_id} [{state}] kind={job.kind.value} "
                f"next={job.next_run_at.isoformat() if job.next_run_at else '-'} "
                f"last={job.last_status or 'never'} task={job.prompt[:80]}"
            )
        return self._result("\n".join(lines))


class ScheduleManageTool(_ScheduleTool):
    name = "schedule_manage"
    description = "Pause, resume, or remove one of the current user's scheduled tasks."

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string"},
                        "action": {
                            "type": "string",
                            "enum": ["pause", "resume", "remove"],
                        },
                    },
                    "required": ["job_id", "action"],
                    "additionalProperties": False,
                },
            },
        }

    def validate(self, arguments: dict[str, Any]) -> None:
        if (
            not isinstance(arguments.get("job_id"), str)
            or not arguments["job_id"].strip()
        ):
            raise InvalidArguments("job_id must be a non-empty string")
        if arguments.get("action") not in {"pause", "resume", "remove"}:
            raise InvalidArguments("action must be pause, resume or remove")

    async def execute(
        self, arguments: dict[str, Any], *, context: ToolExecutionContext | None = None
    ) -> ToolResult:
        self.validate(arguments)
        job_id = arguments["job_id"].strip()
        jobs = [
            job
            for job in self.store.load_jobs()
            if job.job_id == job_id and self._owned(job)
        ]
        if not jobs:
            return self._result("No such owned scheduled task.", ok=False)
        action = arguments["action"]
        if action == "remove":
            changed = self.store.remove_job(job_id)
        else:
            changed = self.store.set_enabled(job_id, action == "resume")
        return self._result(
            f"{action}: {job_id}" if changed else f"No change: {job_id}", ok=changed
        )
