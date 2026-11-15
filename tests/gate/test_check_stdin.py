"""Raw stdin policy gates must retain file reports and fail before publication."""
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

import agentwrench.gate.cli as cli
import agentwrench.gate.streams as streams
from .test_normalize_stdin import install, no_writer, snapshot

FIX = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("source,fixture", [("codex-exec", "codex_exec_ok.jsonl"),
                                           ("claude-stream", "claude_stream_ok.jsonl")])
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("as_json", [False, True])
def test_check_stdin_matches_file(tmp_path, monkeypatch, capsys, source, fixture, explicit, failed, as_json):
    raw = b" \r\n" + (FIX / fixture).read_bytes().replace(b"Done", "Done ☃😀".encode()).replace(b"\n", b"\r\n").rstrip(b"\r\n")
    raw += b'\r\n{"type":"future"}'
    stream, policy = tmp_path / "input.jsonl", tmp_path / "policy.yaml"
    stream.write_bytes(raw)
    policy.write_text("forbid_command: {pattern: pytest}\n" if failed else "require_final_message: {pattern: '^Done'}\nrequire_success: true\n")
    args = ["--policy", str(policy), "--root", "/workspace/demo"] + (["--json"] if as_json else [])
    if explicit:
        args += ["--from", source]
    expected_xml, actual_xml = tmp_path / "expected.xml", tmp_path / "actual.xml"
    assert cli.main(["check", str(stream), *args, "--junit", str(expected_xml)]) == int(failed)
    expected = capsys.readouterr()
    pipe = install(monkeypatch, raw, prefix=b"consumed garbage\xff", chunk=3)
    assert cli.main(["check", "-", *args, "--junit", str(actual_xml)]) == int(failed)
    actual = capsys.readouterr()
    assert actual == expected and not actual.err
    assert actual_xml.read_bytes() == expected_xml.read_bytes()
    assert actual_xml.stat().st_mode & 0o777 == 0o600
    assert pipe.consumed == len(raw) and pipe.read(1) == b""
    if as_json:
        report = json.loads(actual.out)
        assert report["source"] == source and report["passed"] is not failed
        assert report["ignored"] == {"future": 1} and report["complete"]
        xml = ET.parse(actual_xml).getroot()
        assert int(xml.get("tests")) == report["summary"]["total"]
        assert int(xml.get("failures")) == report["summary"]["failed"] == int(failed)
        if failed:
            rule = next(r for r in report["results"] if not r["passed"])
            assert rule["evidence"] and "evidence event indexes=" + json.dumps(rule["evidence"]) in xml.find("testcase/failure").text
    else:
        assert f"{int(failed)} failed," in actual.out


@pytest.mark.parametrize("raw,diagnostic", [
    (b"", "empty stream"), (b" \r\n\t\n", "empty stream"),
    (b'\n{"type":', "line 2"), (b'{"type":"turn.started"}\n\xff', "line 2"),
    (b'[]', "expected an object"), (b'{"type":"turn.started","type":"turn.completed"}', "duplicate JSON key"),
    (b'{"type":"turn.completed","extra":NaN}', "invalid JSON number"),
    (b'{"type":"future"}', "auto-detect"),
    (b'{"type":"turn.started"}\n{"type":"assistant"}', "auto-detect"),
    (b'{"type":"item.completed","item":{}}', "line 1"),
])
@pytest.mark.parametrize("as_json", [False, True])
def test_check_stdin_invalid_has_no_report(tmp_path, monkeypatch, capsys, raw, diagnostic, as_json):
    install(monkeypatch, raw)
    monkeypatch.setattr(cli, "write_new_reports", no_writer)
    before = snapshot(tmp_path)
    args = ["check", "-", "--policy", str(FIX / "policy.yaml"), "--junit", str(tmp_path / "report.xml")]
    assert cli.main(args + (["--json"] if as_json else [])) == 2
    captured = capsys.readouterr()
    assert not captured.out and diagnostic in captured.err and snapshot(tmp_path) == before


@pytest.mark.parametrize("fail_after", [0, 7, 28, 75])
def test_check_stdin_read_failure(tmp_path, monkeypatch, capsys, fail_after):
    install(monkeypatch, b'{"type":"turn.completed"}\n' * 3, fail_after=fail_after)
    monkeypatch.setattr(cli, "write_new_reports", no_writer)
    assert cli.main(["check", "-", "--policy", str(FIX / "policy.yaml"), "--json", "--junit", str(tmp_path / "report.xml")]) == 2
    captured = capsys.readouterr()
    assert not captured.out and "synthetic raw stdin read failure" in captured.err
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("limit", ["MAX_STREAM_BYTES", "MAX_LINE_BYTES", "MAX_LINES"])
@pytest.mark.parametrize("terminated", [False, True])
def test_check_stdin_inclusive_bounds(tmp_path, monkeypatch, capsys, limit, terminated):
    raw = b' \r\n{"type":"turn.completed"}' + (b'\r\n' if terminated else b'')
    cap = {"MAX_STREAM_BYTES": len(raw), "MAX_LINE_BYTES": len(raw) - 3, "MAX_LINES": 2}[limit]
    policy = tmp_path / "policy.yaml"
    policy.write_text("max_commands: 10")
    monkeypatch.setattr(streams, limit, cap)
    pipe = install(monkeypatch, raw, chunk=1)
    assert cli.main(["check", "-", "--policy", str(policy), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["complete"] and pipe.consumed == len(raw)
    monkeypatch.setattr(streams, limit, cap - 1)
    pipe = install(monkeypatch, raw)
    monkeypatch.setattr(cli, "write_new_reports", no_writer)
    before = snapshot(tmp_path)
    assert cli.main(["check", "-", "--policy", str(policy), "--junit", str(tmp_path / "report.xml")]) == 2
    captured = capsys.readouterr()
    assert not captured.out and "stream exceeds limit" in captured.err
    assert snapshot(tmp_path) == before
    if limit == "MAX_STREAM_BYTES":
        assert pipe.consumed == cap  # One overflow-detection byte beyond the new cap.


@pytest.mark.parametrize("text", [None, "typo: true", "max_commands: [", "max_commands: 1\nmax_commands: 2", "require_command: {pattern: '['}"])
def test_invalid_policy_does_not_read_stdin(tmp_path, monkeypatch, capsys, text):
    policy = tmp_path / "policy.yaml"
    if text is not None:
        policy.write_text(text)
    pipe = install(monkeypatch, b'{"type":"turn.completed"}')
    monkeypatch.setattr(cli, "write_new_reports", no_writer)
    before = snapshot(tmp_path)
    assert cli.main(["check", "-", "--policy", str(policy), "--json", "--junit", str(tmp_path / "report.xml")]) == 2
    captured = capsys.readouterr()
    assert not captured.out and captured.err and not pipe.requests and snapshot(tmp_path) == before


@pytest.mark.parametrize("source,raw", [
    ("codex-exec", b'{"type":"item.started","item":{"id":"c","type":"command_execution","command":"pytest","status":"in_progress"}}\n{"type":"turn.completed"}'),
    ("claude-stream", b'{"type":"assistant","message":{"content":[{"type":"tool_use","id":"c","name":"Bash","input":{"command":"pytest"}}]}}\n{"type":"result","subtype":"success","is_error":false}'),
    ("codex-exec", b'{"type":"turn.started"}'),
    ("claude-stream", b'{"type":"system","subtype":"init"}'),
])
@pytest.mark.parametrize("rule", ["max_commands: 10", "require_success: true", "max_total_tokens: 100", "require_command: {pattern: pytest, exit_code: 0}"])
def test_check_stdin_incomplete_override(tmp_path, monkeypatch, capsys, source, raw, rule):
    policy = tmp_path / "policy.yaml"
    policy.write_text(rule)
    for allow in (False, True):
        install(monkeypatch, raw)
        args = ["check", "-", "--from", source, "--policy", str(policy), "--json"]
        code = cli.main(args + (["--allow-incomplete"] if allow else []))
        report = json.loads(capsys.readouterr().out)
        assert code == (0 if allow and rule == "max_commands: 10" else 1)
        assert not report["complete"]
        assert ("incomplete_stream" in [r["id"] for r in report["results"]]) is not allow
        assert bool(report["pending_calls"]) == (b'"id":"c"' in raw)


def test_check_stdin_dash_and_literal_file(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    Path("-").write_bytes(b'{"type":"turn.started"}')
    policy = tmp_path / "policy.yaml"
    policy.write_text("max_commands: 10")
    pipe = install(monkeypatch, b'{"type":"turn.completed"}')
    assert cli.main(["check", "-", "--policy", str(policy), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["complete"]
    requests = list(pipe.requests)
    assert cli.main(["check", "./-", "--policy", str(policy), "--json"]) == 1
    assert not json.loads(capsys.readouterr().out)["complete"] and pipe.requests == requests


@pytest.mark.parametrize("blocked", ["existing", "symlink", "missing-parent", "fsync", "link", "race"])
def test_check_stdin_junit_rollback(tmp_path, monkeypatch, capsys, blocked):
    output = tmp_path / "report.xml"
    if blocked == "existing":
        output.write_bytes(b"keep")
    elif blocked == "symlink":
        output.symlink_to(tmp_path / "absent")
    elif blocked == "missing-parent":
        output = tmp_path / "missing" / "report.xml"
    elif blocked == "fsync":
        def fail_fsync(*args):
            raise OSError("synthetic fsync failure")
        monkeypatch.setattr(cli.os, "fsync", fail_fsync)
    elif blocked in {"link", "race"}:
        real_link = cli.os.link

        def fail_link(src, dst):
            if blocked == "race":
                Path(dst).write_bytes(b"race winner")
                return real_link(src, dst)
            raise OSError("synthetic link failure")

        monkeypatch.setattr(cli.os, "link", fail_link)
    before = snapshot(tmp_path)
    install(monkeypatch, (FIX / "codex_exec_ok.jsonl").read_bytes())
    assert cli.main(["check", "-", "--policy", str(FIX / "policy.yaml"), "--json", "--junit", str(output)]) == 2
    captured = capsys.readouterr()
    assert not captured.out and captured.err and not list(tmp_path.glob(".trajgate-*"))
    if blocked == "race":
        assert output.read_bytes() == b"race winner"
    else:
        assert snapshot(tmp_path) == before
