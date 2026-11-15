"""Real official SDK clients, talking to proxy/replay subprocesses over stdio."""
import asyncio
from datetime import timedelta
import json
from pathlib import Path
import shutil
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError

from agentwrench.cassette.format import load_cassette
from .conftest import ROOT


async def sdk_session(argv):
    params = StdioServerParameters(command=argv[0], args=argv[1:])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            initialized = await client.initialize()
            tools = await client.list_tools()
            result = await client.call_tool("echo", {"text": "你好, offline MCP!"})
            await client.send_ping()
            return (initialized.model_dump(mode="json"), tools.model_dump(mode="json"), result.model_dump(mode="json"))


def test_official_sdk_record_then_replay_without_server(tmp_path):
    # Copy then DELETE the only server path used by record. A replay that
    # mistakenly tries to execute its upstream will fail this test.
    server = tmp_path / "real_server.py"
    shutil.copyfile(ROOT / "tests/cassette/fixtures/echo_server.py", server)
    path = tmp_path / "echo.json"
    report = tmp_path / "replay-report.json"
    recorded = asyncio.run(asyncio.wait_for(sdk_session([
        sys.executable, "-m", "agentwrench", "cassette", "record", "--cassette", str(path), "--", sys.executable, str(server)]), timeout=15))
    assert load_cassette(path)["incomplete"] is False
    server.unlink()
    replayed = asyncio.run(asyncio.wait_for(sdk_session([
        sys.executable, "-m", "agentwrench", "cassette", "replay", "--cassette", str(path), "--strict", "--report", str(report)]), timeout=15))
    assert replayed == recorded
    assert json.loads(report.read_text()) == {"exit_code": 0, "unmatched": 0, "unused": 0}
    assert recorded[2]["content"][0]["text"] == "你好, offline MCP!"


@pytest.mark.mcp_cassette("echo")
def test_official_sdk_plugin_checked_in_cassette(mcp_cassette):
    assert not mcp_cassette.recording
    result = asyncio.run(asyncio.wait_for(sdk_session(mcp_cassette.argv), timeout=15))
    assert result[2]["content"][0]["text"] == "你好, offline MCP!"


@pytest.mark.parametrize("kind,error_code,message", [
    ("eof", -32000, "Connection closed"),
    ("timeout", 408, "Timed out while waiting for response"),
])
def test_official_sdk_observes_transport_fault(tmp_path, kind, error_code, message):
    data = load_cassette(ROOT / "tests/cassette/cassettes/echo.json")
    # End at the tool response so strict replay has no intentional unused suffix.
    data["events"] = data["events"][:8]
    path = tmp_path / "session.json"
    path.write_text(json.dumps(data))
    scenario = {"version": 1, "request_seq": 5, "kind": kind}
    if kind == "timeout":
        scenario["duration_ms"] = 1000
    fault = tmp_path / "fault.json"
    fault.write_text(json.dumps(scenario))
    report = tmp_path / "exit.json"

    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=[
            "-m", "agentwrench", "cassette", "replay", "--cassette", str(path), "--strict",
            "--fault-scenario", str(fault), "--report", str(report)])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                assert (await client.list_tools()).tools[0].name == "echo"
                with pytest.raises(McpError) as exc:
                    await client.call_tool("echo", {"text": "你好, offline MCP!"},
                                           read_timeout_seconds=timedelta(milliseconds=100))
                assert exc.value.error.code == error_code
                assert message in exc.value.error.message

    asyncio.run(asyncio.wait_for(exercise(), timeout=8))
    assert json.loads(report.read_text()) == {"exit_code": 0, "unmatched": 0, "unused": 0,
                                             "fault": {**scenario, "triggered": True}}
