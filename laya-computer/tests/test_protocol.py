"""Real stdio protocol, no native UI or model. Unit fixtures live separately."""

import asyncio
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def test_stdio_tool_discovery_and_validation():
    async def run():
        parameters = StdioServerParameters(command=sys.executable, args=["-m", "laya_computer.server"])
        async with stdio_client(parameters) as streams:
            async with ClientSession(*streams) as session:
                initialized = await asyncio.wait_for(session.initialize(), 10)
                assert initialized.server_info.name == "laya-computer"
                tools = await session.list_tools()
                assert {t.name for t in tools.tools} == {
                    "inspect", "run_plan", "get_run", "resume_plan", "stop_run"
                }
                result = await session.call_tool("run_plan", {"plan": {"goal": "invalid"}})
                assert result.is_error
    asyncio.run(run())
