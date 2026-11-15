#!/usr/bin/env python3
"""Synthetic offline MCP stdio server for the `agentwrench scan` demo.

Usage: demo_mcp.py <catalogue>   (serves ./<catalogue>.json next to this file)

It answers initialize, ping and paginated tools/list, and nothing else: no
network, no file access beyond its own catalogue, and tools/call is refused.
All tool definitions are fictional examples shaped like popular MCP servers.
"""
import json
import os
import signal
import sys

signal.signal(signal.SIGPIPE, signal.SIG_DFL)
signal.signal(signal.SIGINT, signal.SIG_DFL)
HERE = os.path.dirname(os.path.abspath(__file__))
NAME = sys.argv[1] if len(sys.argv) > 1 else "github"
with open(os.path.join(HERE, os.path.basename(NAME) + ".json"), encoding="utf-8") as handle:
    TOOLS = json.load(handle)["tools"]
VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
PAGE = 25


def emit(message):
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    if not line.strip():
        continue
    message = json.loads(line)
    if "id" not in message:
        continue  # notifications
    method, params = message.get("method"), message.get("params") or {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        emit({"jsonrpc": "2.0", "id": message["id"], "result": {
            "protocolVersion": asked if asked in VERSIONS else VERSIONS[-1],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": f"demo-{NAME}", "version": "0.0.1"}}})
    elif method == "ping":
        emit({"jsonrpc": "2.0", "id": message["id"], "result": {}})
    elif method == "tools/list":
        start = int(params.get("cursor") or 0)
        result = {"tools": TOOLS[start:start + PAGE]}
        if start + PAGE < len(TOOLS):
            result["nextCursor"] = str(start + PAGE)
        emit({"jsonrpc": "2.0", "id": message["id"], "result": result})
    else:
        emit({"jsonrpc": "2.0", "id": message["id"],
              "error": {"code": -32601, "message": "demo server: method not available"}})
