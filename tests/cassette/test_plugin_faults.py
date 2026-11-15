"""Fault-marker contracts through the auto-loaded plugin in isolated pytest runs."""
import json

import pytest

from .conftest import cassette, request, response


EOF = {"version": 1, "request_seq": 2, "kind": "eof"}
TIMEOUT = {**EOF, "kind": "timeout", "duration_ms": 300}


def session(pytester, data=None):
    directory = pytester.path / "cassettes"
    directory.mkdir()
    target = directory / "fault.json"
    target.write_text(json.dumps(data if data is not None else cassette(
        ("client", request("first")), ("server", response(result={"ok": True})),
        ("client", request("ping")), ("server", response()))))
    return target


def run(pytester, mode="none"):
    return pytester.runpytest_subprocess(f"--mcp-record={mode}", "-q", "-p", "no:cacheprovider")


@pytest.mark.parametrize("mode,match", [("none", "params"), ("once", "sequence")])
@pytest.mark.parametrize("scenario", [EOF, TIMEOUT], ids=["eof", "timeout"])
def test_marker_stages_snapshot_and_triggers(pytester, mode, match, scenario):
    target = session(pytester)
    original = target.read_bytes()
    pytester.makepyfile('''
import json, selectors, subprocess, sys, time
from pathlib import Path
import pytest
SCENARIO = %r
@pytest.mark.mcp_cassette("fault", fault_scenario=SCENARIO, match=%r,
    command=[sys.executable, "-c", 'from pathlib import Path; Path("server-started").touch()'])
def test_client(mcp_cassette, tmp_path):
    assert not mcp_cassette.recording
    armed = dict(SCENARIO)
    staged = Path(mcp_cassette.argv[mcp_cassette.argv.index("--fault-scenario") + 1])
    assert staged.parent == tmp_path
    assert staged.stat().st_mode & 0o777 == 0o600
    assert json.loads(staged.read_text()) == armed == SCENARIO
    assert staged.stat().st_size <= 4096
    # The fixture must retain the validated snapshot, including at teardown.
    SCENARIO["request_seq"] = 0
    assert json.loads(staged.read_text()) == armed
    with subprocess.Popen(mcp_cassette.argv, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
        try:
            proc.stdin.write(b'{"jsonrpc":"2.0","id":"live","method":"first"}\\n')
            proc.stdin.flush()
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                assert selector.select(3), "no prefix response"
                assert json.loads(proc.stdout.readline()) == {
                    "jsonrpc":"2.0", "id":"live", "result":{"ok":True}}
                started = time.monotonic()
                proc.stdin.write(b'{"jsonrpc":"2.0","id":999,"method":"ping"}\\n')
                proc.stdin.flush()
                if armed["kind"] == "timeout":
                    assert not selector.select(0.05), "target output or early EOF"
            assert proc.wait(timeout=5) == 0, proc.stderr.read()
            if armed["kind"] == "timeout":
                assert time.monotonic() - started >= 0.25
            assert proc.stdout.read() == b""
            assert not proc.stdin.closed
        finally:
            if proc.poll() is None:
                proc.kill()
    report = Path(mcp_cassette.argv[mcp_cassette.argv.index("--report") + 1])
    assert json.loads(report.read_text()) == {
        "exit_code":0, "unmatched":0, "unused":0, "fault":{**armed, "triggered":True}}
''' % (scenario, match))
    result = run(pytester, mode)
    result.assert_outcomes(passed=1)
    assert target.read_bytes() == original
    assert not (pytester.path / "server-started").exists()


@pytest.mark.parametrize("mode,present", [("all", True), ("all", False), ("once", False)])
def test_fault_recording_rejected_before_test_or_server(pytester, mode, present):
    target = session(pytester)
    original = target.read_bytes()
    if not present:
        target.unlink()
    pytester.makepyfile('''
import subprocess, sys
from pathlib import Path
import pytest
@pytest.mark.mcp_cassette("fault", fault_scenario=%r,
    command=[sys.executable, "-c", 'from pathlib import Path; Path("server-started").touch()'])
def test_client(mcp_cassette):
    Path("body-started").touch()
    subprocess.run(mcp_cassette.argv, input=b"", capture_output=True, timeout=5)
''' % EOF)
    result = run(pytester, mode)
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*fault_scenario is replay-only*recording*"])
    assert not (pytester.path / "body-started").exists()
    assert not (pytester.path / "server-started").exists()
    if present:
        assert target.read_bytes() == original
    else:
        assert not target.exists()


@pytest.mark.parametrize("scenario", [
    None, [], "fault.json", {},
    {**EOF, "version": True}, {**EOF, "version": 2},
    {**EOF, "request_seq": True}, {**EOF, "request_seq": 2.0},
    {**EOF, "request_seq": -1}, {**EOF, "request_seq": 1}, {**EOF, "request_seq": 4},
    {**EOF, "kind": "delay"}, {**EOF, "duration_ms": 1}, {**EOF, "extra": "x" * 4097},
    {**TIMEOUT, "duration_ms": 0}, {**TIMEOUT, "duration_ms": 60001},
    {**TIMEOUT, "duration_ms": True}, {**TIMEOUT, "duration_ms": 1.5},
], ids=["null", "array", "path-string", "empty", "bool-version", "future-version",
        "bool-seq", "float-seq", "negative-seq", "response-target", "absent-target",
        "kind", "eof-duration", "oversized-unknown", "zero-duration", "long-duration",
        "bool-duration", "float-duration"])
def test_invalid_marker_rejected_at_setup(pytester, scenario):
    target = session(pytester)
    original = target.read_bytes()
    pytester.makepyfile('''
from pathlib import Path
import pytest
@pytest.mark.mcp_cassette("fault", fault_scenario=%r)
def test_client(mcp_cassette):
    Path("body-started").touch()
''' % scenario)
    result = run(pytester)
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*invalid fault_scenario:*"])
    assert not (pytester.path / "body-started").exists()
    assert target.read_bytes() == original


@pytest.mark.parametrize("case", ["missing", "incomplete", "notification", "server-request"])
def test_fault_requires_complete_cassette_and_client_request(pytester, case):
    data = cassette(("client", request("ping")), ("server", response()))
    if case == "incomplete":
        data["incomplete"] = True
    elif case == "notification":
        data = cassette(("client", {"jsonrpc": "2.0", "method": "notifications/initialized"}))
    elif case == "server-request":
        data = cassette(("server", request("sampling/createMessage")), ("client", response()))
    target = session(pytester, data)
    if case == "missing":
        target.unlink()
    scenario = {**EOF, "request_seq": 0}
    pytester.makepyfile('''
from pathlib import Path
import pytest
@pytest.mark.mcp_cassette("fault", fault_scenario=%r)
def test_client(mcp_cassette):
    Path("body-started").touch()
''' % scenario)
    result = run(pytester)
    result.assert_outcomes(errors=1)
    message = {"missing": "*missing cassette*", "incomplete": "*incomplete cassette*"}.get(
        case, "*request_seq must identify a recorded client request*")
    result.stdout.fnmatch_lines([message])
    assert not (pytester.path / "body-started").exists()


@pytest.mark.parametrize("fault", [
    None, [], {},
    EOF, {**EOF, "triggered": False}, {**EOF, "triggered": 1}, {**EOF, "triggered": "true"},
    {**EOF, "triggered": True, "request_seq": 0},
    {**EOF, "triggered": True, "request_seq": 2.0},
    {**EOF, "triggered": True, "version": True},
    {**EOF, "triggered": True, "version": 1.0},
    {**EOF, "triggered": True, "extra": 1},
    {**TIMEOUT, "triggered": True},
], ids=["null", "array", "empty", "no-trigger", "untriggered", "int-trigger", "str-trigger",
        "other-target", "float-target", "bool-version", "float-version", "extra-field", "other-kind"])
def test_bad_fault_report_fails_even_with_exit_zero(pytester, fault):
    session(pytester)
    # Simulate a client ignoring process errors or using a different argv. A
    # success exit code alone must not count as exercising the intended fault.
    pytester.makepyfile('''
import json
from pathlib import Path
import pytest
@pytest.mark.mcp_cassette("fault", fault_scenario=%r)
def test_client(mcp_cassette):
    report = Path(mcp_cassette.argv[mcp_cassette.argv.index("--report") + 1])
    report.write_text(json.dumps({"exit_code":0, "unmatched":0, "unused":0, "fault":%r}))
''' % (EOF, fault))
    result = run(pytester)
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*invalid MCP subprocess result*fault*"])


def test_normal_replay_report_cannot_satisfy_armed_fault(pytester):
    session(pytester)
    pytester.makepyfile('''
import subprocess
import pytest
@pytest.mark.mcp_cassette("fault", fault_scenario=%r)
def test_client(mcp_cassette):
    argv = list(mcp_cassette.argv)
    index = argv.index("--fault-scenario")
    del argv[index:index+2]
    result = subprocess.run(argv, input=(
        b'{"jsonrpc":"2.0","id":1,"method":"first"}\\n'
        b'{"jsonrpc":"2.0","id":2,"method":"ping"}\\n'), capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
''' % EOF)
    result = run(pytester)
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*fault report must contain triggered=true*"])


@pytest.mark.parametrize("raw", [None, "{", "null"], ids=["missing", "malformed", "null"])
def test_armed_fault_requires_finalized_report(pytester, raw):
    session(pytester)
    pytester.makepyfile('''
from pathlib import Path
import pytest
@pytest.mark.mcp_cassette("fault", fault_scenario=%r)
def test_client(mcp_cassette):
    raw = %r
    if raw is not None:
        report = Path(mcp_cassette.argv[mcp_cassette.argv.index("--report") + 1])
        report.write_text(raw)
''' % (EOF, raw))
    result = run(pytester)
    result.assert_outcomes(passed=1, errors=1)
    message = "*MCP process did not finalize*" if raw is None else "*invalid MCP subprocess result*"
    result.stdout.fnmatch_lines([message])


@pytest.mark.parametrize("case,options,messages,diagnostic", [
    ("untriggered", "strict=False", [request("first")], "*fault scenario was not triggered*"),
    ("unused", "strict=True", [request("ping")], "*MCP cassette process failed*'unused': 1*"),
    ("unmatched", "strict=True", [request("wrong"), request("first"), request("ping")],
     "*MCP cassette process failed*'unmatched': 1*"),
])
def test_fault_does_not_hide_replay_failures(pytester, case, options, messages, diagnostic):
    session(pytester)
    raw = "".join(json.dumps(m) + "\n" for m in messages).encode()
    pytester.makepyfile('''
import subprocess
import pytest
@pytest.mark.mcp_cassette("fault", fault_scenario=%r, %s)
def test_client(mcp_cassette):
    # A client may swallow the exit status; the fixture must still fail.
    subprocess.run(mcp_cassette.argv, input=%r, capture_output=True, timeout=5)
''' % (EOF, options, raw))
    result = run(pytester)
    result.assert_outcomes(passed=1, errors=1)
    if case == "untriggered":
        # Stderr was captured by the client: the sidecar is the teardown proof.
        diagnostic = "*MCP cassette process failed*'triggered': False*"
    result.stdout.fnmatch_lines([diagnostic])


def test_cassette_metadata_does_not_arm_a_marker_fault(pytester):
    data = cassette(("client", request("ping")), ("server", response()))
    data["fault_scenario"] = {**EOF, "request_seq": 0}
    target = session(pytester, data)
    original = target.read_bytes()
    pytester.makepyfile('''
import json, subprocess
import pytest
@pytest.mark.mcp_cassette("fault")
def test_client(mcp_cassette):
    assert "--fault-scenario" not in mcp_cassette.argv
    result = subprocess.run(mcp_cassette.argv,
        input=b'{"jsonrpc":"2.0","id":99,"method":"ping"}\\n', capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"jsonrpc":"2.0", "id":99, "result":{}}
''')
    run(pytester).assert_outcomes(passed=1)
    assert target.read_bytes() == original
