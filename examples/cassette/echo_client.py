"""Official SDK demo: record or replay the repository's synthetic echo session."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run(mode, cassette):
    argv = [sys.executable, "-m", "agentwrench", "cassette", mode, "--cassette", cassette]
    if mode == "record":
        server = Path(__file__).resolve().parent / "echo_server.py"
        argv += ["--", sys.executable, str(server)]
    else:
        argv.append("--strict")
    async with stdio_client(StdioServerParameters(command=argv[0], args=argv[1:])) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            tools = await client.list_tools()
            result = await client.call_tool("echo", {"text": "你好, offline MCP!"})
            await client.send_ping()
            print(json.dumps({"tools": [tool.name for tool in tools.tools],
                              "content": result.model_dump(mode="json")["content"]}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["record", "replay"])
    parser.add_argument("cassette")
    args = parser.parse_args()
    asyncio.run(asyncio.wait_for(run(args.mode, args.cassette), timeout=15))
