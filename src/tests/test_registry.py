import asyncio
from pathlib import Path

from cairn.tools.registry import ToolRegistry
from cairn.tools.bash import BashTool


def test_duplicate_tool_raises():
    registry = ToolRegistry()
    bash_tool = BashTool(cwd=Path.cwd())

    registry.register_tool(bash_tool)

    try:
        registry.register_tool(bash_tool)
    except ValueError as e:
        assert "bash" in str(e)
        return

    raise AssertionError("Expected ValueError was not raised")


async def main():
    registry = ToolRegistry()

    registry.register_tool(
        BashTool(
            cwd=Path.cwd()
        )
    )

    print(registry.schemas())

    result = await registry.execute(
        "bash",
        {"command": "pwd"}
    )

    print(result)


if __name__ == "__main__":
    asyncio.run(main())
