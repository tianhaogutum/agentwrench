"""MCP stdio server that negotiates an older protocol and writes to stderr (test fixture)."""
import json
import sys

sys.stderr.write("old-protocol-server: STDERR-NOISE-MARKER\n")
sys.stderr.flush()
TOOLS = [{"name": "ping_db", "description": "Check the database connection.",
          "inputSchema": {"type": "object", "properties": {}}}]
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    if message.get("method") == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "old", "version": "1.0.0"}}
    elif message.get("method") == "tools/list":
        result = {"tools": TOOLS}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\n")
    sys.stdout.flush()
