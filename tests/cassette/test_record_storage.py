"""Stored-frame limits apply after redaction and compact UTF-8 serialization."""
import copy
import json
import re
import subprocess
import sys

import pytest

from agentwrench.cassette.format import (CassetteError, MAX_LINE, canonical, load_cassette,
                                 parse_line, redact, validate_cassette)
from agentwrench.cassette.record import Recorder
from .conftest import request, response

DIAGNOSTIC = "recorded message exceeds 1 MiB after redaction (including newline)"


def frame(msg):
    return (canonical(msg) + "\n").encode("utf-8")


def near_limit(kind, value, over):
    payload = {"redact": value, "pad": "秘密\n\"\\"}
    if kind == "response":
        msg, pointer = response(7, payload), "/result/redact"
    else:
        msg, pointer = request("work", payload, id=7), "/params/redact"
        if kind == "notification":
            del msg["id"]
    payload["pad"] += "x" * (MAX_LINE + over - len(frame(redact(msg, [pointer]))))
    assert len(frame(msg)) <= MAX_LINE
    assert parse_line(frame(msg)) == msg
    assert len(frame(redact(msg, [pointer]))) == MAX_LINE + over
    return msg, pointer


@pytest.mark.parametrize("direction", ["client", "server"])
@pytest.mark.parametrize("kind", ["request", "response", "notification"])
@pytest.mark.parametrize("value", ["", 0, {}, [], "秘", "\n\"\\"])
@pytest.mark.parametrize("over", [0, 1], ids=["exact", "one-over"])
def test_transformed_storage_bound_and_pairing(direction, kind, value, over):
    msg, pointer = near_limit(kind, value, over)
    original = copy.deepcopy(msg)
    recorder = Recorder([pointer])
    # Same value, different ID types, in both directions; neither may be lost.
    for owner in ("client", "server"):
        recorder.append(owner, request("pending", id=7))
        recorder.append(owner, request("pending", id="7"))
    if kind == "request":
        msg["id"] = 8  # same serialized width
    before = copy.deepcopy((recorder.events, recorder.pending, recorder.size))
    if over:
        with pytest.raises(CassetteError, match="^" + re.escape(DIAGNOSTIC) + "$"):
            recorder.append(direction, msg)
        assert (recorder.events, recorder.pending, recorder.size) == before
        validate_cassette(recorder.document(0, [DIAGNOSTIC]))
    else:
        recorder.append(direction, msg)
        stored = recorder.events[-1]["message"]
        assert len(frame(stored)) == MAX_LINE
        assert stored == redact(msg, [pointer])
        expected = copy.deepcopy(before[1])
        if kind == "request":
            expected[direction].add(("int", 8))
        elif kind == "response":
            expected["server" if direction == "client" else "client"].remove(("int", 7))
        assert recorder.pending == expected
    if kind == "request":
        original["id"] = 8
    assert msg == original
    # Close every accepted lifetime and prove the retained events are consistent.
    for owner in ("client", "server"):
        for _, value in list(recorder.pending[owner]):
            recorder.append("server" if owner == "client" else "client", response(value))
    validate_cassette(recorder.document(0, []))


@pytest.mark.parametrize("direction", ["client", "server"])
@pytest.mark.parametrize("over", [0, 1], ids=["exact", "one-over"])
def test_canonical_number_expansion_without_redaction(direction, over):
    # Valid JSON 1e-7 gains a byte when compact serialization writes 1e-07.
    raw = b'{"jsonrpc":"2.0","method":"note","params":{"n":1e-7,"pad":""}}\n'
    raw = raw.replace(b'"pad":""', b'"pad":"' + b'x' * (MAX_LINE + over - len(raw) - 1) + b'"')
    msg = parse_line(raw)
    assert len(raw) <= MAX_LINE
    assert len(frame(msg)) == MAX_LINE + over
    recorder = Recorder([])
    if over:
        with pytest.raises(CassetteError, match="recorded message exceeds"):
            recorder.append(direction, msg)
        assert recorder.events == [] and not any(recorder.pending.values())
    else:
        recorder.append(direction, msg)
        validate_cassette(recorder.document(0, []))


@pytest.mark.parametrize("kind", ["request", "response"])
@pytest.mark.parametrize("budget", ["MAX_EVENTS", "MAX_CASSETTE"])
def test_budget_rejection_preserves_pairing(monkeypatch, kind, budget):
    import agentwrench.cassette.record as module
    recorder = Recorder([])
    recorder.append("client", request(id=7))
    recorder.append("client", request(id="7"))
    before = copy.deepcopy((recorder.events, recorder.pending, recorder.size))
    monkeypatch.setattr(module, budget, 0)
    with pytest.raises(CassetteError, match="recording limit"):
        recorder.append("server", request(id=7) if kind == "request" else response(id=7))
    assert (recorder.events, recorder.pending, recorder.size) == before


SERVER = '''import json, sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text())
sys.stdout.buffer.write(Path(plan['startup']).read_bytes())
sys.stdout.buffer.flush()
if plan['direction'] == 'client':
    raw = sys.stdin.buffer.read()
    Path(plan['received']).write_bytes(raw)
    if raw and plan['kind'] == 'request':
        sys.stdout.buffer.write(b'{"jsonrpc":"2.0","id":7,"result":{}}\\n')
        sys.stdout.buffer.flush()
else:
    Path(plan['received']).write_bytes(sys.stdin.buffer.readline())
    sys.stdout.buffer.write(Path(plan['target']).read_bytes())
    sys.stdout.buffer.flush()
    sys.stdin.buffer.read()
'''


@pytest.mark.parametrize("direction", ["client", "server"])
@pytest.mark.parametrize("kind", ["request", "response", "notification"])
@pytest.mark.parametrize("over", [0, 1], ids=["exact", "one-over"])
def test_record_storage_cli(cli, tmp_path, direction, kind, over):
    msg, pointer = near_limit(kind, "", over)
    # Noncanonical spacing/order, Unicode and CRLF must survive live forwarding.
    wire = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode() + b'\r\n'
    assert len(wire) <= MAX_LINE
    prefix = b'{ "method": "note", "jsonrpc": "2.0" }\r\n'
    startup = prefix if direction == "client" else b''
    if kind == "response":
        if direction == "client":
            startup += frame(request("work", id=7))
    received = tmp_path / 'received.jsonl'
    (tmp_path / 'startup').write_bytes(startup)
    (tmp_path / 'target').write_bytes(wire)
    server = tmp_path / 'server.py'
    server.write_text(SERVER)
    plan = tmp_path / 'plan.json'
    plan.write_text(json.dumps({"direction": direction, "kind": kind,
        "startup": str(tmp_path / 'startup'), "target": str(tmp_path / 'target'),
        "received": str(received)}))
    path, report = tmp_path / 'capture.json', tmp_path / 'report.json'
    path.write_text('previous destination must be atomically replaced')
    argv = [sys.executable, '-m', 'agentwrench', 'cassette', 'record', '--cassette', str(path),
            '--report', str(report), '--redact-path', pointer, '--', sys.executable, str(server), str(plan)]
    with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
        # Reading startup first ensures any server request is pending before its
        # client response arrives. Read target concurrently with the server write.
        for line in startup.splitlines(keepends=True):
            assert proc.stdout.readline() == line
        client_prefix = frame(request("work", id=7)) if kind == 'response' else prefix
        if direction == 'server':
            proc.stdin.write(client_prefix)
            proc.stdin.flush()
            if not over:
                assert proc.stdout.readline() == wire
        stdin = wire if direction == 'client' else (frame(response(7)) if kind == 'request' and not over else b'')
        stdout, stderr = proc.communicate(stdin, timeout=15)
    assert proc.returncode == (1 if over else 0), stderr
    assert json.loads(report.read_bytes()) == {"exit_code": int(bool(over)), "unmatched": 0, "unused": 0}
    data = load_cassette(path)
    assert data['incomplete'] is bool(over)
    assert data['redact_paths'] == [pointer]
    assert all(len(frame(e['message'])) <= MAX_LINE for e in data['events'])
    expected_pending = kind == 'response' and bool(over)
    assert ('unanswered requests remain' in data['metadata']['reasons']) is expected_pending
    if over:
        assert DIAGNOSTIC.encode() in stderr and len(stderr) < 300
        assert stdout == b''
        assert len(data['events']) == (2 if direction == 'client' and kind == 'response' else 1)
        assert all(e['message'].get('params', {}).get('redact') != '<REDACTED>' for e in data['events'])
    else:
        stored = next(e['message'] for e in data['events'] if len(frame(e['message'])) == MAX_LINE)
        assert stored == redact(msg, [pointer])
        assert stdout == (b'{"jsonrpc":"2.0","id":7,"result":{}}\n' if direction == 'client' and kind == 'request' else b'')
    if direction == 'client' and not over:
        assert received.read_bytes() == wire
    if direction == 'server':
        assert received.read_bytes() == client_prefix
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob('.mcp-cassette-*'))
    validation = cli('validate', path)
    assert validation.returncode == int(bool(over))
    diagnostics = json.loads(validation.stdout)['diagnostics']
    assert {d['code'] for d in diagnostics} == ({'incomplete_recording', 'unanswered_request'} if expected_pending else {'incomplete_recording'} if over else set())
    if over or (direction, kind) not in {('server', 'request'), ('client', 'response')}:
        replay = cli('replay', '--cassette', path, '--strict', raw=(wire if direction == 'client' else client_prefix))
        assert replay.returncode == (2 if over else 0), replay.stderr


@pytest.mark.parametrize("over", [0, 1], ids=["exact", "one-over"])
def test_record_storage_pytest_teardown(pytester, over):
    msg, pointer = near_limit("response", "", over)
    target = pytester.path / "server.jsonl"
    target.write_bytes(frame(msg))
    server = pytester.path / "server.py"
    server.write_text("import sys\nfrom pathlib import Path\nsys.stdin.buffer.readline()\nsys.stdout.buffer.write(Path(sys.argv[1]).read_bytes())\nsys.stdout.buffer.flush()\nsys.stdin.buffer.read()\n")
    pytester.makepyfile(f'''import subprocess, sys
import pytest
@pytest.mark.mcp_cassette("storage", command=[sys.executable, {str(server)!r}, {str(target)!r}], redact_paths=[{pointer!r}])
def test_client(mcp_cassette):
    # Deliberately ignore status: the fixture must still reject incomplete record.
    subprocess.run(mcp_cassette.argv, input=b'{{"jsonrpc":"2.0","id":7,"method":"work"}}\\n', capture_output=True, timeout=15)
''')
    result = pytester.runpytest_subprocess("--mcp-record=all", "-q", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, errors=int(bool(over)))
    if over:
        result.stdout.fnmatch_lines(["*MCP cassette process failed*'exit_code': 1*"])
    data = load_cassette(pytester.path / "cassettes/storage.json")
    assert data["incomplete"] is bool(over)
    assert all(len(frame(e["message"])) <= MAX_LINE for e in data["events"])
