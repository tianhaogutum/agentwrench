"""Opt-in transport failures over actual stdio, without a real upstream."""
import json
import selectors
import signal
import subprocess
import sys
import time

import pytest

from agentwrench.cassette.format import CassetteError
from agentwrench.cassette.replay import Replayer
from .conftest import cassette, request, response


def write_inputs(tmp_path, scenario, data=None):
    path = tmp_path / "session.json"
    path.write_text(json.dumps(data if data is not None else cassette(
        ("client", request("ping")), ("server", response()))))
    fault = tmp_path / "fault.json"
    fault.write_text(json.dumps(scenario))
    return path, fault


def test_eof_suppresses_target_outputs_preserves_success_bytes(cli, tmp_path):
    note = {"jsonrpc": "2.0", "method": "notifications/message", "params": {"data": "你好"}}
    data = cassette(("server", note), ("client", request("a")), ("server", note),
                    ("server", response(result={"n": 1})), ("client", request("a")),
                    ("server", note), ("server", response(result={"n": 2})), ("server", note))
    scenario = {"version": 1, "request_seq": 4, "kind": "eof"}
    path, fault = write_inputs(tmp_path, scenario, data)
    original = path.read_bytes(), fault.read_bytes()
    messages = [request("a", id="live"), request("a", id=999)]
    success = cli("replay", "--cassette", path, "--strict", messages=messages)
    assert success.returncode == 0, success.stderr
    expected_prefix = ('{"jsonrpc":"2.0","method":"notifications/message","params":{"data":"你好"}}\n'
                       '{"jsonrpc":"2.0","method":"notifications/message","params":{"data":"你好"}}\n'
                       '{"jsonrpc":"2.0","id":"live","result":{"n":1}}\n').encode()
    assert success.stdout == expected_prefix + (
        '{"jsonrpc":"2.0","method":"notifications/message","params":{"data":"你好"}}\n'
        '{"jsonrpc":"2.0","id":999,"result":{"n":2}}\n'
        '{"jsonrpc":"2.0","method":"notifications/message","params":{"data":"你好"}}\n').encode()
    report = tmp_path / "exit.json"
    result = cli("replay", "--cassette", path, "--strict", "--fault-scenario", fault,
                 "--report", report, messages=messages)
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected_prefix
    assert json.loads(report.read_text()) == {"exit_code": 0, "unmatched": 0, "unused": 0,
                                             "fault": {**scenario, "triggered": True}}
    again = cli("replay", "--cassette", path, "--strict", messages=messages)
    assert again.stdout == success.stdout and again.stderr == success.stderr
    assert (path.read_bytes(), fault.read_bytes()) == original


def test_timeout_stays_silent_then_exits_with_client_stdin_open(tmp_path):
    note = {"jsonrpc": "2.0", "method": "notifications/ready"}
    path, fault = write_inputs(tmp_path, {"version": 1, "request_seq": 2, "kind": "timeout", "duration_ms": 300},
                               cassette(("client", request("first")), ("server", response()),
                                        ("client", request("ping")), ("server", note), ("server", response())))
    report = tmp_path / "exit.json"
    with subprocess.Popen([sys.executable, "-m", "agentwrench", "cassette", "replay", "--cassette", str(path),
                           "--fault-scenario", str(fault), "--report", str(report), "--strict"],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
        try:
            proc.stdin.write((json.dumps(request("first")) + "\n").encode())
            proc.stdin.flush()
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                assert selector.select(timeout=3), proc.stderr.read() if proc.poll() is not None else "no response"
                assert json.loads(proc.stdout.readline()) == response()
                started = time.monotonic()
                proc.stdin.write((json.dumps(request("ping")) + "\n").encode())
                proc.stdin.flush()
                # Includes EOF readiness: neither a response nor EOF is allowed yet.
                assert not selector.select(timeout=0.05)
            assert proc.wait(timeout=3) == 0, proc.stderr.read()
            assert time.monotonic() - started >= 0.25
            assert proc.stdout.read() == b""
            assert not proc.stdin.closed
        finally:
            if proc.poll() is None:
                proc.kill()
    assert json.loads(report.read_text())["fault"]["triggered"] is True


def test_untriggered_scenario_fails_even_without_strict(cli, tmp_path):
    scenario = {"version": 1, "request_seq": 0, "kind": "eof"}
    path, fault = write_inputs(tmp_path, scenario)
    report = tmp_path / "exit.json"
    result = cli("replay", "--cassette", path, "--fault-scenario", fault, "--report", report)
    assert result.returncode == 1
    assert b"fault scenario was not triggered" in result.stderr
    assert json.loads(report.read_text())["fault"] == {**scenario, "triggered": False}


@pytest.mark.parametrize("scenario", [
    None, [], {}, {"version": True}, {"version": 2},
    {"version": 1, "request_seq": 0, "kind": []},
    {"version": 1, "request_seq": 0, "kind": "delay"},
    *({"version": 1, "request_seq": seq, "kind": "eof"} for seq in (-1, 1, 2, True, 0.0, "0")),
    {"version": 1, "request_seq": 0, "kind": "eof", "duration_ms": 0},
    {"version": 1, "request_seq": 0, "kind": "eof", "typo": True},
    {"version": 1, "request_seq": 0, "kind": "timeout"},
    *({"version": 1, "request_seq": 0, "kind": "timeout", "duration_ms": duration}
      for duration in (-1, 0, 60001, True, 0.5, "100")),
])
def test_invalid_scenario_rejected_before_replay_output(cli, tmp_path, scenario):
    path, fault = write_inputs(tmp_path, scenario)
    # A response and its notification must not escape invalid configuration.
    data = json.loads(path.read_text())
    data["events"].append({"seq": 2, "ms": 2, "direction": "server",
                           "message": {"jsonrpc": "2.0", "method": "notifications/message"}})
    path.write_text(json.dumps(data))
    result = cli("replay", "--cassette", path, "--fault-scenario", fault, messages=[request("ping")])
    assert result.returncode == 2
    assert result.stdout == b""
    assert b"Traceback" not in result.stderr


def test_invalid_scenario_blocks_startup_notifications(cli, tmp_path):
    note = {"jsonrpc": "2.0", "method": "notifications/message"}
    path, fault = write_inputs(tmp_path, {"version": 1, "request_seq": 1, "kind": "eof", "typo": True},
                               cassette(("server", note), ("client", request("ping")), ("server", response())))
    result = cli("replay", "--cassette", path, "--fault-scenario", fault)
    assert result.returncode == 2 and result.stdout == b""


@pytest.mark.parametrize("raw", [b'{', b'null', b'{"version":1,"version":1}', b'NaN', b'x' * 4097])
def test_bad_scenario_file(cli, tmp_path, raw):
    path, fault = write_inputs(tmp_path, {})
    fault.write_bytes(raw)
    result = cli("replay", "--cassette", path, "--fault-scenario", fault)
    assert result.returncode == 2 and result.stdout == b""
    assert b"Traceback" not in result.stderr


def test_missing_scenario_file(cli, tmp_path):
    path, fault = write_inputs(tmp_path, {})
    fault.unlink()
    result = cli("replay", "--cassette", path, "--fault-scenario", fault)
    assert result.returncode == 2 and result.stdout == b""
    assert b"cannot read fault scenario" in result.stderr


@pytest.mark.parametrize("symlink", [False, True])
def test_report_cannot_overwrite_scenario(cli, tmp_path, symlink):
    path, fault = write_inputs(tmp_path, {"version": 1, "request_seq": 0, "kind": "eof"})
    original = fault.read_bytes()
    report = fault
    if symlink:
        report = tmp_path / "alias.json"
        report.symlink_to(fault)
    result = cli("replay", "--cassette", path, "--fault-scenario", fault, "--report", report)
    assert result.returncode == 2 and result.stdout == b""
    assert b"report path must differ" in result.stderr
    assert fault.read_bytes() == original


@pytest.mark.parametrize("strict,code", [(False, 0), (True, 1)])
def test_terminal_fault_ignores_pipelined_input_but_keeps_unused_accounting(cli, tmp_path, strict, code):
    scenario = {"version": 1, "request_seq": 0, "kind": "eof"}
    path, fault = write_inputs(tmp_path, scenario, cassette(
        ("client", request("a")), ("server", response()),
        ("client", request("b")), ("server", response())))
    report = tmp_path / "exit.json"
    result = cli("replay", "--cassette", path, "--fault-scenario", fault, "--report", report,
                 *(["--strict"] if strict else []), messages=[request("a"), request("b"), request("wrong")])
    assert result.returncode == code and result.stdout == b""
    assert json.loads(report.read_text()) == {"exit_code": code, "unmatched": 0, "unused": 1,
                                             "fault": {**scenario, "triggered": True}}


@pytest.mark.parametrize("match", ["params", "sequence"])
def test_target_is_recorded_seq_after_matching_not_live_id_or_ordinal(match):
    data = cassette(("client", request("a", id=1)), ("client", request("b", id="1")),
                    ("server", response("1")), ("server", response(1)))
    scenario = {"version": 1, "request_seq": 1, "kind": "eof"}
    player = Replayer(data, match=match, fault_scenario=scenario)
    # Mutating the caller's scenario must not alter the armed plan.
    scenario["request_seq"] = 0
    if match == "params":
        assert player.handle(request("b", id=777)) == []
    else:
        assert "error" in player.handle(request("b", id=777))[0]
        assert not player.fault_triggered
        assert player.handle(request("a", id=888)) == [response(888)]
        assert player.handle(request("b", id=999)) == []
    assert player.fault_triggered
    with pytest.raises(CassetteError, match="terminated replay"):
        player.handle(request("a"))


def test_notification_cannot_be_fault_target(cli, tmp_path):
    path, fault = write_inputs(tmp_path, {"version": 1, "request_seq": 0, "kind": "eof"},
                               cassette(("client", {"jsonrpc": "2.0", "method": "notifications/initialized"})))
    result = cli("replay", "--cassette", path, "--fault-scenario", fault)
    assert result.returncode == 2 and result.stdout == b""


def test_initialize_mismatch_does_not_trigger_fault(cli, tmp_path):
    path, fault = write_inputs(tmp_path, {"version": 1, "request_seq": 0, "kind": "eof"},
                               cassette(("client", request("initialize", {"protocolVersion": "2025-11-25"})),
                                        ("server", response(result={"protocolVersion": "2025-11-25"}))))
    report = tmp_path / "exit.json"
    result = cli("replay", "--cassette", path, "--strict", "--fault-scenario", fault, "--report", report,
                 messages=[request("initialize", {"protocolVersion": "wrong"})])
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["code"] == -32002
    assert json.loads(report.read_text())["fault"]["triggered"] is False


def test_signal_interrupts_maximum_timeout_and_writes_report(tmp_path):
    path, fault = write_inputs(tmp_path, {"version": 1, "request_seq": 0, "kind": "timeout", "duration_ms": 60000})
    report = tmp_path / "exit.json"
    with subprocess.Popen([sys.executable, "-m", "agentwrench", "cassette", "replay", "--cassette", str(path),
                           "--fault-scenario", str(fault), "--report", str(report), "--strict"],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
        try:
            proc.stdin.write((json.dumps(request("ping")) + "\n").encode())
            proc.stdin.flush()
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stderr, selectors.EVENT_READ)
                assert selector.select(timeout=3), "fault did not trigger"
                assert b"transport fault triggered" in proc.stderr.readline()
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=3) == 0, proc.stderr.read()
            assert proc.stdout.read() == b""
        finally:
            if proc.poll() is None:
                proc.kill()
    assert json.loads(report.read_text())["fault"]["triggered"] is True
