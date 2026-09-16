import asyncio
from pathlib import Path

from cairn.tools.registry import ToolRegistry
from cairn.tools.bash import BashTool
 
async def main():
    registry = ToolRegistry()

    registry.register_tool(
        BashTool(
            cwd=Path.cwd()
        )
    )

    print(registry.schema())

    result = await registry.execute(
        "bash",
        {"command": "pwd" }   
    )

    print(result)

if __name__ == "__main__":
    asyncio.run(main())