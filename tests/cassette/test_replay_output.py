"""Bound the actual UTF-8 replay frame after live bindings, including its LF."""
import copy
import json

import pytest

from .conftest import cassette, request, response
from agentwrench.cassette.format import MAX_LINE, canonical, validate_cassette


DIAGNOSTIC = b"agentwrench cassette: replay output line exceeds 1 MiB (including newline)\n"


def wire(message):
    return (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def note(text):
    return {"jsonrpc": "2.0", "method": "notifications/message", "params": {"data": text}}


def boundary_case(kind, live, extra):
    """Both stored and input frames fit; only live rebinding can exceed the bound."""
    recorded = request("work", id=0)
    incoming = request("work", id=live if kind == "response" else "live-response")
    if kind == "response":
        stored = response(0, {"text": "你好😀\n\"\\"})
        rewritten = response(live, copy.deepcopy(stored["result"]))
        stored_payload, live_payload = stored["result"], rewritten["result"]
        field = "text"
    else:
        recorded["params"] = {"_meta": {"progressToken": 0}}
        incoming["params"] = {"_meta": {"progressToken": live}}
        stored = {"jsonrpc": "2.0", "method": "notifications/progress",
                  "params": {"progressToken": 0, "progress": 1, "message": "你好😀\n\"\\"}}
        rewritten = copy.deepcopy(stored)
        rewritten["params"]["progressToken"] = live
        stored_payload, live_payload = stored["params"], rewritten["params"]
        field = "message"
    padding = "x" * (MAX_LINE + extra - len(wire(rewritten)))
    stored_payload[field] += padding
    live_payload[field] += padding
    prefix = [note("startup"), response("prefix"), note("before-boundary")]
    trailing = [note("after-boundary")]
    if kind == "progress":
        trailing.append(response("live-response"))
    events = [("server", prefix[0]), ("client", request("first")), ("server", response()),
              ("client", recorded), ("server", prefix[2]), ("server", stored),
              ("server", trailing[0])]
    if kind == "progress":
        events.append(("server", response(0)))
    events += [("client", request("last")), ("server", response())]
    data = validate_cassette(cassette(*events))
    messages = [request("first", id="prefix"), incoming, request("last", id=42)]
    assert len(wire(rewritten)) == MAX_LINE + extra
    assert len(wire(stored)) <= MAX_LINE
    assert len(canonical(stored).encode("utf-8")) + 1 <= MAX_LINE
    assert all(len(wire(m)) <= MAX_LINE for m in messages)
    expected = prefix + [rewritten] + trailing + [response(42)]
    return data, messages, prefix, expected


@pytest.mark.parametrize("match", ["params", "sequence"])
@pytest.mark.parametrize("kind", ["response", "progress"])
@pytest.mark.parametrize("live", ["live-" + "a" * 1024, "😀\"\\\n" * 64, 10**100],
                         ids=["ascii", "unicode-escaping", "integer"])
@pytest.mark.parametrize("extra", [0, 1], ids=["exact-limit", "one-byte-over"])
def test_rewritten_output_byte_boundary(cli, tmp_path, match, kind, live, extra):
    data, messages, prefix, expected = boundary_case(kind, live, extra)
    path, report = tmp_path / "case.json", tmp_path / "report.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    original = path.read_bytes()
    result = cli("replay", "--cassette", path, "--match", match, "--strict", "--report", report,
                 raw=b"".join(map(wire, messages)))
    code = 2 if extra else 0
    assert result.returncode == code, result.stderr
    assert result.stderr == (DIAGNOSTIC if extra else b"")
    assert json.loads(report.read_bytes()) == {"exit_code": code, "unmatched": 0, "unused": int(extra)}
    assert result.stdout == b"".join(map(wire, prefix if extra else expected))
    assert all(len(line) <= MAX_LINE for line in result.stdout.splitlines(keepends=True))
    assert path.read_bytes() == original


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("fault", [None, "eof", "timeout", "untriggered"])
def test_output_failure_accounting_and_fault_suppression(cli, tmp_path, strict, fault):
    data, messages, prefix, _ = boundary_case("response", "expanded" * 128, 1)
    path, report = tmp_path / "case.json", tmp_path / "report.json"
    path.write_text(json.dumps(data))
    args = ["replay", "--cassette", path, "--report", report]
    if strict:
        args.append("--strict")
    scenario = None
    if fault:
        scenario = {"version": 1, "request_seq": 7 if fault == "untriggered" else 3,
                    "kind": "eof" if fault == "untriggered" else fault}
        if fault == "timeout":
            scenario["duration_ms"] = 1
        scenario_path = tmp_path / "fault.json"
        scenario_path.write_text(json.dumps(scenario))
        args += ["--fault-scenario", scenario_path]
    # An unmatched notification before the valid prefix counts once, emits no MCP frame.
    messages.insert(0, {"jsonrpc": "2.0", "method": "unknown"})
    result = cli(*args, raw=b"".join(map(wire, messages)))
    suppressed = fault in ("eof", "timeout")
    code = int(strict) if suppressed else 2
    assert result.returncode == code, result.stderr
    assert result.stdout == b"".join(map(wire, prefix[:2] if suppressed else prefix))
    expected_report = {"exit_code": code, "unmatched": 1, "unused": 1}
    if scenario:
        expected_report["fault"] = {**scenario, "triggered": suppressed}
    assert json.loads(report.read_bytes()) == expected_report
    assert (DIAGNOSTIC in result.stderr) is not suppressed
    assert b"Traceback" not in result.stderr


def test_generated_error_cannot_outgrow_valid_live_request(cli, tmp_path):
    path, report = tmp_path / "empty.json", tmp_path / "report.json"
    path.write_text(json.dumps(cassette()))
    incoming = request("unknown", id="")
    incoming["id"] = "x" * (MAX_LINE - len(wire(incoming)))
    assert len(wire(incoming)) == MAX_LINE
    result = cli("replay", "--cassette", path, "--report", report, raw=wire(incoming))
    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr.endswith(DIAGNOSTIC)
    assert len(result.stderr) < 512 and b"Traceback" not in result.stderr
    assert json.loads(report.read_bytes()) == {"exit_code": 2, "unmatched": 1, "unused": 0}


@pytest.mark.parametrize("extra", [0, 1], ids=["exact-limit", "one-byte-over"])
def test_fixture_checks_real_output_failure(pytester, extra):
    data, messages, prefix, expected = boundary_case("progress", "😀\"\\\n" * 64, extra)
    directory = pytester.path / "cassettes"
    directory.mkdir()
    (directory / "bound.json").write_text(json.dumps(data))
    (pytester.path / "input.jsonl").write_bytes(b"".join(map(wire, messages)))
    (pytester.path / "expected.jsonl").write_bytes(b"".join(map(wire, prefix if extra else expected)))
    pytester.makepyfile('''
import subprocess
from pathlib import Path
import pytest
@pytest.mark.mcp_cassette("bound")
def test_client(mcp_cassette):
    result = subprocess.run(mcp_cassette.argv, input=Path("input.jsonl").read_bytes(),
                            capture_output=True, timeout=10)
    assert result.returncode == %d, result.stderr
    assert result.stdout == Path("expected.jsonl").read_bytes()
''' % (2 if extra else 0))
    result = pytester.runpytest_subprocess("-q", "--tb=line", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, errors=extra)
    if extra:
        result.stdout.fnmatch_lines(["*MCP cassette process failed*'exit_code': 2*'unused': 1*"])
