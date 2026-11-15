"""Synthetic MCP 2025-11-25 wire fixture, stdlib only; no network or secrets."""
import json
import sys


def send(message):
    print(json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False), flush=True)


for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    method = msg["method"]
    if method == "initialize":
        result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "synthetic-echo", "version": "1.0"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "description": "Echo synthetic text",
                             "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                                             "required": ["text"], "additionalProperties": False}}]}
    elif method == "tools/call" and msg.get("params", {}).get("name") == "echo":
        text = msg["params"]["arguments"]["text"]
        send({"method": "notifications/message", "params": {"level": "info", "data": "synthetic call"}})
        result = {"content": [{"type": "text", "text": text}], "isError": False}
    elif method == "ping":
        result = {}
    else:
        send({"id": msg["id"], "error": {"code": -32601, "message": "Unknown method"}})
        continue
    send({"id": msg["id"], "result": result})
