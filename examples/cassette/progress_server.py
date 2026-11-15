"""Synthetic ordinary tools/call progress server; stdlib only, no external I/O."""
import json
import sys

for line in sys.stdin:
    msg = json.loads(line)
    token = msg["params"]["_meta"]["progressToken"]
    for value, message in [(0.5, "starting"), (2.5, "done")]:
        print(json.dumps({"jsonrpc": "2.0", "method": "notifications/progress",
                          "params": {"progressToken": token, "progress": value,
                                     "total": 2.5, "message": message}}), flush=True)
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
        "content": [{"type": "text", "text": msg["params"]["arguments"]["text"]}], "isError": False}}), flush=True)
