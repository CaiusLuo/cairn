import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from cairn.core.models import ToolResult


class BashTool:
    name = "bash"
    description = "Execute a shell command in the current workspace."

    def __init__(self, cwd: Path, timeout: float = 30.0) -> None:
        self.cwd = cwd
        self.timeout = timeout

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

    async def execute(
        self,
        arguments: dict[str, Any],
    ) -> ToolResult:
        command = arguments["command"]
        cwd = self.cwd.resolve()
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
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout
            )
        except TimeoutError:
            process.kill()
            await process.communicate()

            return ToolResult(
                stderr=f"Command '{command}' time out after {self.timeout}s",
                exit_code=-1,
            )

        return ToolResult(
            stdout=stdout.decode(),
            stderr=stderr.decode(),
            exit_code=await process.wait(),
        )
