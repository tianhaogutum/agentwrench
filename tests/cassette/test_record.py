import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys

import pytest

from agentwrench.cassette.format import load_cassette
from agentwrench.cassette.replay import Replayer
from .conftest import ROOT, request, response

SERVER = ROOT / "tests" / "cassette" / "fixtures" / "echo_server.py"


def test_record_proxy_redaction_and_inspect(cli, tmp_path):
    path = tmp_path / "capture.json"
    msg = request(params={"name": "echo", "arguments": {"text": "credential-秘密"}})
    env = {**os.environ, "SYNTHETIC_SECRET": "never-store-environment"}
    result = cli("record", "--cassette", path, "--redact-path", "/params/arguments/text",
                 "--redact-path", "/result/content/0/text", "--", sys.executable, SERVER, messages=[msg], env=env)
    assert result.returncode == 0, result.stderr
    messages = [json.loads(line) for line in result.stdout.splitlines()]
    assert messages[-1]["result"]["content"][0]["text"] == "credential-秘密"  # wire unchanged
    data = load_cassette(path)
    assert not data["incomplete"]
    assert "credential-秘密" not in path.read_text()
    assert "never-store-environment" not in path.read_text()
    assert str(SERVER) not in path.read_text()  # argv is deliberately not saved
    assert data["events"][0]["message"]["params"]["arguments"]["text"] == "<REDACTED>"
    assert all(e["ms"] >= 0 for e in data["events"])
    inspection = cli("inspect", path)
    assert inspection.returncode == 0
    assert json.loads(inspection.stdout)["tools"] == {"echo": 1}
    replay = cli("replay", "--cassette", path, "--strict", messages=[{**msg, "id": "changed"}])
    assert replay.returncode == 0, replay.stderr
    assert json.loads(replay.stdout.splitlines()[-1])["id"] == "changed"


def test_record_crash_incomplete_and_replay_refusal(cli, tmp_path):
    path = tmp_path / "crash.json"
    result = cli("record", "--cassette", path, "--", sys.executable, "-c", "import sys; sys.exit(7)", messages=[request()])
    assert result.returncode == 1
    data = load_cassette(path)
    assert data["incomplete"] and data["metadata"]["server_exit_code"] == 7
    assert cli("replay", "--cassette", path).returncode == 2


def test_missing_executable(cli, tmp_path):
    path = tmp_path / "missing.json"
    result = cli("record", "--cassette", path, "--", str(tmp_path / "nonexistent"))
    assert result.returncode == 1
    assert load_cassette(path)["incomplete"]


def test_concurrent_requests_and_reversed_responses(cli, tmp_path):
    path = tmp_path / "concurrent.json"
    code = '''import json, sys
messages = [json.loads(sys.stdin.readline()), json.loads(sys.stdin.readline())]
for msg in reversed(messages):
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"method": msg["method"]}}), flush=True)
sys.stdin.read()
'''
    result = cli("record", "--cassette", path, "--", sys.executable, "-c", code,
                 messages=[request("first", id=1), request("second", id="1")])
    assert result.returncode == 0, result.stderr
    data = load_cassette(path)
    assert [e["message"]["id"] for e in data["events"]] == [1, "1", "1", 1]
    player = Replayer(data, strict=True)
    assert player.handle(request("first", id=11)) == [response(11, {"method": "first"})]
    assert player.handle(request("second", id=12)) == [response(12, {"method": "second"})]
    assert player.finish() == 0


def test_records_server_initiated_round_trip(cli, tmp_path):
    path = tmp_path / "roots.json"
    code = '''import json, sys
print(json.dumps({"jsonrpc": "2.0", "id": "server", "method": "roots/list"}), flush=True)
sys.stdin.read()
'''
    with subprocess.Popen([sys.executable, "-m", "agentwrench", "cassette", "record", "--cassette", str(path), "--",
                           sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE) as proc:
        assert json.loads(_readline(proc.stdout))["method"] == "roots/list"
        proc.stdin.write((json.dumps(response("server", {"roots": []})) + "\n").encode())
        proc.stdin.close()
        assert proc.wait(timeout=10) == 0, proc.stderr.read()
    assert not load_cassette(path)["incomplete"]
    replay = cli("replay", "--cassette", path)
    assert replay.returncode == 2 and b"server-initiated" in replay.stderr


def _readline(stream):
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        assert selector.select(timeout=8), "subprocess did not produce a message"
    return stream.readline()


def test_signal_finalizes_record_and_strict_replay(cli, tmp_path):
    path = tmp_path / "signal.json"
    argv = [sys.executable, "-m", "agentwrench", "cassette", "record", "--cassette", str(path), "--", sys.executable, str(SERVER)]
    with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
        proc.stdin.write((json.dumps(request("ping")) + "\n").encode())
        proc.stdin.flush()
        assert json.loads(_readline(proc.stdout))["result"] == {}
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0, proc.stderr.read()
    assert not load_cassette(path)["incomplete"]
    report = tmp_path / "result.json"
    with subprocess.Popen([sys.executable, "-m", "agentwrench", "cassette", "replay", "--cassette", str(path),
                           "--strict", "--report", str(report)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE) as proc:
        proc.stdin.write((json.dumps(request("wrong")) + "\n").encode())
        proc.stdin.flush()
        assert "error" in json.loads(_readline(proc.stdout))
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 1
    assert json.loads(report.read_text())["unused"] == 1


@pytest.mark.parametrize("skip, expected", [(False, 1), (True, 0)])
def test_non_json_stdout_policy(cli, tmp_path, skip, expected):
    path = tmp_path / "noise.json"
    code = 'import sys; print("synthetic log", flush=True); sys.stdin.read()'
    options = ["--skip-non-json"] if skip else []
    result = cli("record", "--cassette", path, *options, "--", sys.executable, "-c", code)
    assert result.returncode == expected
    assert result.stdout == b""
    assert load_cassette(path)["incomplete"] is (not skip)
    if skip:
        assert b"skipped non-JSON" in result.stderr


def test_skip_flag_does_not_hide_invalid_rpc(cli, tmp_path):
    path = tmp_path / "invalid.json"
    result = cli("record", "--cassette", path, "--skip-non-json", "--", sys.executable, "-c", 'print("{}")')
    assert result.returncode == 1
    assert load_cassette(path)["incomplete"]


def test_hung_server_timeout(cli, tmp_path):
    path = tmp_path / "hung.json"
    result = cli("record", "--cassette", path, "--shutdown-timeout", "0.1", "--", sys.executable,
                 "-c", "import time; time.sleep(60)")
    assert result.returncode == 1
    assert "server did not exit" in " ".join(load_cassette(path)["metadata"]["reasons"])


def test_malformed_client_and_unanswered(cli, tmp_path):
    path = tmp_path / "invalid-input.json"
    result = cli("record", "--cassette", path, "--", sys.executable, SERVER, raw=b"[]\n")
    assert result.returncode == 1
    assert load_cassette(path)["incomplete"]
    result = cli("record", "--cassette", path, "--", sys.executable, "-c", "import sys; sys.stdin.read()",
                 messages=[request()])
    assert result.returncode == 1
    assert "unanswered requests remain" in load_cassette(path)["metadata"]["reasons"]


def test_unmatched_response_and_duplicate_id_are_incomplete(cli, tmp_path):
    path = tmp_path / "pairing.json"
    server = "import sys; sys.stdin.read()"
    for messages, diagnostic in [([response(404)], "no matching"), ([request(), request()], "duplicate")]:
        result = cli("record", "--cassette", path, "--", sys.executable, "-c", server, messages=messages)
        assert result.returncode == 1
        assert load_cassette(path)["incomplete"]
        assert diagnostic in " ".join(load_cassette(path)["metadata"]["reasons"])


def test_oversized_line_does_not_grow_recording(cli, tmp_path):
    path = tmp_path / "huge.json"
    result = cli("record", "--cassette", path, "--", sys.executable, SERVER, raw=b"x" * (1024 * 1024 + 2) + b"\n")
    assert result.returncode == 1
    data = load_cassette(path)
    assert data["incomplete"] and data["events"] == []


def test_event_budget_is_enforced(monkeypatch):
    import agentwrench.cassette.record as module
    from agentwrench.cassette.format import CassetteError
    monkeypatch.setattr(module, "MAX_EVENTS", 1)
    recorder = module.Recorder([])
    recorder.append("client", request())
    with pytest.raises(CassetteError, match="recording limit"):
        recorder.append("server", response())


def test_eof_without_newline_is_rejected(cli, tmp_path):
    path = tmp_path / "truncated.json"
    result = cli("record", "--cassette", path, "--", sys.executable, SERVER, raw=json.dumps(request()).encode())
    assert result.returncode == 1
    assert load_cassette(path)["events"] == []
    assert b"newline" in result.stderr


def test_invalid_redaction_does_not_start_command(cli, tmp_path):
    path = tmp_path / "c.json"
    sentinel = tmp_path / "executed"
    command = [sys.executable, "-c", f"from pathlib import Path; Path({str(sentinel)!r}).touch()"]
    result = cli("record", "--cassette", path, "--redact-path", "/id", "--", *command)
    assert result.returncode == 2
    assert not sentinel.exists()
    assert not path.exists()


def test_skip_does_not_hide_duplicate_keys_or_nonfinite_values(cli, tmp_path):
    path = tmp_path / "strict-json.json"
    for bad_json in ['{"jsonrpc":"2.0","method":"ping","id":1,"id":2}', '{"jsonrpc":"2.0","id":1,"result":NaN}']:
        result = cli("record", "--cassette", path, "--skip-non-json", "--", sys.executable, "-c", f'print({bad_json!r}, flush=True)')
        assert result.returncode == 1
        assert load_cassette(path)["incomplete"]
