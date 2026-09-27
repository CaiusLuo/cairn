import asyncio
import json
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

from cairn.core.models import ToolResult
from cairn.workspace.workspace import Workspace


class BashTool:
    name = "bash"
    description = "Execute a shell command in the current workspace."

    def __init__(
        self, workspace: Workspace, timeout: float = 30.0, cleanup_timeout: float = 2.0
    ) -> None:
        self.workspace = workspace
        self.timeout = timeout
        self.cleanup_timeout = cleanup_timeout

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The shell command to execute.",
                        }
                    },
                    "required": ["command"],
                    "additionalProperties": False,
                },
            },
        }

    async def _cleanup_process(self, process: asyncio.subprocess.Process) -> None:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)

        with suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=self.cleanup_timeout)

    async def execute(
        self,
        arguments: dict[str, Any],
    ) -> ToolResult:
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty string")
        if arguments.keys() - {"command"}:
            raise ValueError("bash only accepts the 'command' argument")

        cwd = self.workspace.root
        env = {
            key: value
            for key in ("PATH", "LANG", "LC_ALL", "TERM", "VIRTUAL_ENV")
            if (value := os.environ.get(key)) is not None
        }
        env.update(HOME=str(cwd), TMPDIR=str(cwd))
        command_argv = (
            [f"/bin/{command.strip()}"]
            if command.strip() in {"pwd", "ls"}
            else ["/bin/sh", "-c", command]
        )

        if sys.platform == "darwin":
            root = json.dumps(str(cwd))
            hidden = " ".join(
                f"(subpath {json.dumps(str(path))})"
                for path in {Path.home().resolve(), cwd.parent}
                if path != Path("/")
            )
            profile = (
                "(version 1) (allow default) "
                f"(deny file-read* {hidden}) (allow file-read* (subpath {root})) "
                f'(deny file-write*) (allow file-write* (literal "/dev/null") (subpath {root})) '
                "(deny network*)"
            )
            argv = ["/usr/bin/sandbox-exec", "-p", profile, *command_argv]
        elif sys.platform == "linux":
            bwrap = Path("/usr/bin/bwrap")
            if not bwrap.is_file():
                raise RuntimeError("BashTool requires bubblewrap on Linux")
            argv = [
                str(bwrap),
                "--die-with-parent",
                "--unshare-net",
                "--unshare-pid",
                "--dev",
                "/dev",
                "--proc",
                "/proc",
                "--tmpfs",
                "/tmp",
            ]
            for path in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc"):
                if Path(path).exists():
                    argv.extend(("--ro-bind", path, path))
            for parent in reversed(cwd.parents):
                if parent != Path("/"):
                    argv.extend(("--dir", str(parent)))
            argv.extend(("--bind", str(cwd), str(cwd), "--chdir", str(cwd)))
            argv.extend(command_argv)
        else:
            raise RuntimeError(f"BashTool has no sandbox for {sys.platform}")

        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

        # Keep draining the pipes while terminating the group: otherwise a full
        # pipe can prevent asyncio's process waiter from completing after exit.
        communication = asyncio.create_task(process.communicate())
        try:
            stdout, stderr = await asyncio.wait_for(
                asyncio.shield(communication), timeout=self.timeout
            )
        except TimeoutError:
            return ToolResult(
                stderr=f"Command '{command}' time out after {self.timeout}s",
                exit_code=-1,
            )
        else:
            return ToolResult(
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                exit_code=await process.wait(),
            )
        finally:
            # Also stop background children whose redirected output allowed
            # communicate() to finish normally. Cancellation propagates only
            # after this same bounded cleanup path.
            try:
                await self._cleanup_process(process)
            finally:
                communication.cancel()
                with suppress(asyncio.CancelledError):
                    await communication
