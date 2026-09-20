import asyncio
from pathlib import Path

from cairn.core.models import ToolResult

class BashTool:
    name = "bash"
    description = "Execute a shell command in the current workspace."

    def __init__(
            self, 
            cwd: Path,
            timeout: float = 30.0
        ):
        self.cwd = cwd
        self.timeout = timeout

    def schema(self) -> dict:
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
                            "description": "The shell command to execute."
                        }
                    },
                    "required": ["command"],
                    "additionalProperties": False
                }
            },
        }

    async def execute(
        self,
        arguments: dict
    ) -> ToolResult:
        command = arguments["command"]

        process = await asyncio.create_subprocess_shell(
            command,
            cwd=self.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self.timeout
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()

            return ToolResult(
                stderr=f"Command '{command}' time out after {self.timeout}s", 
                exit_code=-1
            )

        return ToolResult(
            stdout=stdout.decode(),
            stderr=stderr.decode(),
            exit_code=process.returncode
        )


