from pathlib import Path
import json
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]  # repository root


@pytest.fixture
def cli():
    def run(*args, messages=(), raw=None, env=None):
        data = raw if raw is not None else "".join(json.dumps(m) + "\n" for m in messages).encode()
        return subprocess.run([sys.executable, "-m", "agentwrench", "cassette", *map(str, args)],
                              input=data, capture_output=True, timeout=15, env=env)
    return run


def request(method="tools/call", params=None, id=1):
    msg = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def response(id=1, result=None):
    return {"jsonrpc": "2.0", "id": id, "result": result if result is not None else {}}


def cassette(*messages, paths=(), incomplete=False):
    return {"version": 1, "incomplete": incomplete, "redact_paths": list(paths), "events": [
        {"seq": i, "ms": i, "direction": direction, "message": msg}
        for i, (direction, msg) in enumerate(messages)]}
