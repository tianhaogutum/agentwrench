"""Bound diagnostic retention/output without losing interaction accounting."""
import json

import pytest

from .conftest import cassette, request, response
from agentwrench.cassette.replay import Replayer


@pytest.mark.parametrize("notification", [False, True])
def test_retained_unmatched_details_are_bounded(notification):
    player = Replayer(cassette(), strict=True)
    for index in range(100):
        message = request(f"unknown-{index}", {"secret": "NOT-FOR-DIAGNOSTICS"}, id=str(index))
        if notification:
            del message["id"]
        output = player.handle(message)
        if notification:
            assert output == []
        else:
            assert output[0]["id"] == str(index)
            assert output[0]["error"]["code"] == -32001
            assert f'unknown-{index}' in output[0]["error"]["message"]
    assert len(player.unmatched) == 16
    assert player.unmatched_count == 100
    assert 'unknown-0"' in player.unmatched[0]
    assert 'unknown-15"' in player.unmatched[-1]
    assert "NOT-FOR-DIAGNOSTICS" not in " ".join(player.unmatched)
    assert player.finish() == 1


@pytest.mark.parametrize("unused", [False, True])
def test_many_unicode_diagnostics_have_bounded_summaries(unused):
    warnings = []
    method = "😀\n\x1b" * 100
    messages = [("client", {"jsonrpc": "2.0", "method": method})] * 100 if unused else []
    player = Replayer(cassette(*messages), strict=True, warn=warnings.append)
    if not unused:
        for _ in range(100):
            player.handle(request(method))
    assert player.finish() == 1
    summary = warnings[-1]
    assert len(summary.encode()) <= 4096
    assert f'{"unused" if unused else "unmatched"} interactions (100): ' in summary
    assert "omitted" in summary
    assert "\n" not in summary and "\x1b" not in summary
    shown = summary.count('method=')
    assert f"; ... {100 - shown} omitted" in summary


@pytest.mark.parametrize("count", [0, 1, 16, 17, 100])
@pytest.mark.parametrize("strict", [False, True])
def test_sample_boundary_and_small_summary_compatibility(count, strict):
    warnings = []
    player = Replayer(cassette(), strict=strict, warn=warnings.append)
    for _ in range(count):
        player.handle({"jsonrpc": "2.0", "method": "unknown"})
    details = warnings.copy()
    assert player.finish() == int(strict and count > 0)
    assert player.unmatched_count == count
    assert player.unused_count == 0
    assert player.unmatched == details[:16]
    if count == 0:
        assert warnings == []
    elif count <= 16:
        assert warnings[-1] == f"unmatched interactions ({count}): " + "; ".join(details)
    else:
        assert warnings[-1] == (f"unmatched interactions ({count}): " + "; ".join(details[:16])
                                + f"; ... {count - 16} omitted")


@pytest.mark.parametrize("extra_byte", [False, True])
def test_exact_summary_byte_boundary(extra_byte):
    warnings = []
    player = Replayer(cassette(), warn=warnings.append)
    for _ in range(2):
        player.handle(request("😀" * 140))
    # Build a third live method so the complete summary is exactly 4096 bytes,
    # or one byte beyond. Escape expansion counts; params must stay absent.
    player.handle(request("a"))
    remaining = 4096 - len(("unmatched interactions (3): " + "; ".join(warnings)).encode()) + 1
    astral, ascii_chars = divmod(remaining, 12)
    method = "😀" * astral + "a" * (ascii_chars + extra_byte)
    assert len(method) <= 160
    warnings.clear()
    player = Replayer(cassette(), warn=warnings.append)
    for method_name in ("😀" * 140, "😀" * 140, method):
        player.handle(request(method_name))
    complete = "unmatched interactions (3): " + "; ".join(warnings)
    assert len(complete.encode()) == 4096 + extra_byte
    assert player.finish() == 0
    if extra_byte:
        assert len(warnings[-1].encode()) < 4096
        assert warnings[-1].endswith("; ... 1 omitted")
    else:
        assert warnings[-1] == complete


def test_unused_summary_formats_only_bounded_sample(monkeypatch):
    import agentwrench.cassette.replay as replay

    data = cassette(*[("client", {"jsonrpc": "2.0", "method": "expected"})] * 100)
    warnings = []
    player = Replayer(data, strict=True, warn=warnings.append)
    player.handle({"jsonrpc": "2.0", "method": "expected"})
    assert len(player.unused) == player.unused_count == 99
    original = replay.json.dumps
    formatted = []

    def counted(*args, **kwargs):
        formatted.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(replay.json, "dumps", counted)
    assert player.finish() == 1
    assert len(formatted) == 16
    assert warnings == ["unused interactions (99): " + "; ".join(
        f'seq={i} method="expected"' for i in range(1, 17)) + "; ... 83 omitted"]


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("match", ["params", "sequence"])
@pytest.mark.parametrize("fault", [None, "eof", "timeout", "untriggered"])
def test_cli_exact_counts_and_fault_accounting(cli, tmp_path, strict, match, fault):
    path = tmp_path / "cassette.json"
    data = cassette(("client", request("ok")), ("server", response()),
                    ("client", request("target")), ("server", response()),
                    *[("client", {"jsonrpc": "2.0", "method": "unused"})] * 30)
    path.write_text(json.dumps(data))
    original = path.read_bytes()
    report = tmp_path / "report.json"
    args = ["replay", "--cassette", path, "--report", report, "--match", match]
    if strict:
        args.append("--strict")
    scenario = None
    if fault:
        scenario = {"version": 1, "request_seq": 2, "kind": "eof" if fault == "untriggered" else fault}
        if fault == "timeout":
            scenario["duration_ms"] = 1
        scenario_path = tmp_path / "fault.json"
        scenario_path.write_text(json.dumps(scenario))
        args += ["--fault-scenario", scenario_path]
    messages = []
    ids = []
    for index in range(100):
        message = request("unknown😀\n\x1b" * 30, {"secret": "NOT-FOR-DIAGNOSTICS"},
                          id=index if index % 4 == 0 else str(index))
        if index % 2:
            del message["id"]
        else:
            ids.append(message["id"])
        messages.append(message)
    messages.append(request("ok", id="live-ok"))
    if fault != "untriggered":
        messages.append(request("target", id="live-target"))
    result = cli(*args, messages=messages)
    expected_code = int(strict or fault == "untriggered")
    assert result.returncode == expected_code, result.stderr
    expected_report = {"exit_code": expected_code, "unmatched": 100,
                       "unused": 31 if fault == "untriggered" else 30}
    if scenario:
        expected_report["fault"] = {**scenario, "triggered": fault != "untriggered"}
    assert json.loads(report.read_bytes()) == expected_report
    output = [json.loads(line) for line in result.stdout.splitlines()]
    assert [message["id"] for message in output[:50]] == ids
    assert all(message["error"]["code"] == -32001 for message in output[:50])
    assert output[50:] == [response("live-ok")] + ([response("live-target")] if fault is None else [])
    summaries = [line for line in result.stderr.splitlines() if b" interactions (" in line]
    assert len(summaries) == 2
    assert all(len(line) + 1 <= 4110 for line in summaries)
    assert all(b"omitted" in line for line in summaries)
    assert b"NOT-FOR-DIAGNOSTICS" not in result.stdout + result.stderr
    assert path.read_bytes() == original


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("fault", [False, True])
def test_fixture_uses_exact_report_counts(pytester, strict, fault):
    directory = pytester.path / "cassettes"
    directory.mkdir()
    (directory / "ping.json").write_text(json.dumps(cassette(
        ("client", request("ping")), ("server", response()))))
    pytester.makepyfile('''
import json, subprocess
from pathlib import Path
import pytest
@pytest.mark.mcp_cassette("ping", strict=%r, %s)
def test_client(mcp_cassette):
    messages = [{"jsonrpc":"2.0","method":"unknown"}] * 100
    messages.append({"jsonrpc":"2.0","id":"live","method":"ping"})
    result = subprocess.run(mcp_cassette.argv, input="".join(json.dumps(m)+"\\n" for m in messages),
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == %r
    path = Path(mcp_cassette.argv[mcp_cassette.argv.index("--report") + 1])
    report = json.loads(path.read_text())
    assert report["unmatched"] == 100 and report["unused"] == 0
    assert len(path.read_bytes()) <= 4096
''' % (strict, 'fault_scenario={"version":1,"request_seq":0,"kind":"eof"}' if fault else "", int(strict)))
    result = pytester.runpytest_subprocess("-q", "--tb=line", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, errors=int(strict))
    if strict:
        result.stdout.fnmatch_lines(["*MCP cassette process failed*'unmatched': 100*"])
