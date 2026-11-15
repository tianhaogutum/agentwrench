"""Policy stdin is bounded, borrowed, strict YAML, and equivalent to a file."""
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

import agentwrench.gate.cli as cli
from agentwrench.gate.policy import load_policy
from .test_normalize_stdin import BinaryPipe, install, no_writer, snapshot

FIX = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("source,fixture", [("codex-exec", "codex_exec_ok.jsonl"),
                                           ("claude-stream", "claude_stream_ok.jsonl")])
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("as_json", [False, True])
def test_policy_stdin_matches_file(tmp_path, monkeypatch, capsys, source, fixture, explicit, failed, as_json):
    raw = ("forbid_command: {pattern: pytest}\r\n" if failed else
           "# ☃😀\r\nrequire_final_message: {pattern: '^Done'}\r\nrequire_success: true").encode()
    policy = tmp_path / "policy.yaml"
    policy.write_bytes(raw)
    args = ["check", str(FIX / fixture), "--root", "/workspace/demo"]
    if explicit:
        args += ["--from", source]
    if as_json:
        args += ["--json"]
    expected_xml, actual_xml = tmp_path / "expected.xml", tmp_path / "actual.xml"
    assert cli.main([*args, "--policy", str(policy), "--junit", str(expected_xml)]) == int(failed)
    expected = capsys.readouterr()
    pipe = install(monkeypatch, raw, prefix=b"already consumed\xff", chunk=1)
    assert cli.main([*args, "--policy", "-", "--junit", str(actual_xml)]) == int(failed)
    actual = capsys.readouterr()
    assert actual == expected and not actual.err
    assert actual_xml.read_bytes() == expected_xml.read_bytes()
    assert actual_xml.stat().st_mode & 0o777 == 0o600
    assert pipe.consumed == len(raw) and pipe.read(1) == b""
    if as_json:
        report = json.loads(actual.out)
        assert report["source"] == source and report["passed"] is not failed
        xml = ET.parse(actual_xml).getroot()
        assert int(xml.get("tests")) == report["summary"]["total"]
        assert int(xml.get("failures")) == report["summary"]["failed"] == int(failed)
        if failed:
            evidence = next(r["evidence"] for r in report["results"] if not r["passed"])
            assert evidence and "evidence event indexes=" + json.dumps(evidence) in xml.find("testcase/failure").text


@pytest.mark.parametrize("raw,diagnostic", [
    (b"", "expected an object"), (b" # comment\r\n", "expected an object"),
    (b"{}", "at least one"), (b"[]", "expected an object"),
    (b"max_commands: [", "YAML"), (b"max_commands: 2\n#\xff", "YAML"),
    (b"max_commands: 1\nmax_commands: 2", "duplicate"),
    (b"require_command: {pattern: x, pattern: y}", "duplicate"),
    (b"max_commands: &x 2", "anchors"), (b"max_commands: *x", "aliases"),
    (b"!!python/object/apply:os.system ['touch dangerous']", "YAML"),
    (b"typo: true", "unknown key"), (b"max_commands: true", "integer"),
    (b"require_command: {pattern: '['}", "invalid regex"),
    (b"forbid_file_change: {glob: '['}", "invalid glob"),
    (b"require_command:\n" + b"- {pattern: x}\n" * 201, "200 rules"),
    (b"max_commands: 10\n#" + b"x" * 65536, "64 KiB"),
])
@pytest.mark.parametrize("as_json", [False, True])
def test_invalid_policy_stdin_before_log_or_staging(tmp_path, monkeypatch, capsys, raw, diagnostic, as_json):
    monkeypatch.chdir(tmp_path)
    install(monkeypatch, raw)
    monkeypatch.setattr(cli, "write_new_reports", no_writer)
    monkeypatch.setattr(cli, "normalize", no_writer)
    before = snapshot(tmp_path)
    args = ["check", str(FIX / "codex_exec_ok.jsonl"), "--policy", "-", "--junit", "report.xml"]
    assert cli.main(args + (["--json"] if as_json else [])) == 2
    captured = capsys.readouterr()
    assert not captured.out and diagnostic in captured.err and snapshot(tmp_path) == before


@pytest.mark.parametrize("fail_after", [0, 7, 28, 35])
def test_policy_stdin_read_failure(tmp_path, monkeypatch, capsys, fail_after):
    install(monkeypatch, b"max_commands: 10\n#" + b"x" * 20, fail_after=fail_after)
    monkeypatch.setattr(cli, "normalize", no_writer)
    monkeypatch.setattr(cli, "write_new_reports", no_writer)
    assert cli.main(["check", str(FIX / "codex_exec_ok.jsonl"), "--policy", "-", "--json", "--junit", str(tmp_path / "report.xml")]) == 2
    captured = capsys.readouterr()
    assert not captured.out and "synthetic raw stdin read failure" in captured.err
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("chunk", [1, 13, 65537])
def test_policy_stdin_inclusive_real_cap(tmp_path, monkeypatch, capsys, chunk):
    start = b"max_commands: 10\n# "
    raw = start + b"x" * (65536 - len(start))
    args = ["check", str(FIX / "codex_exec_ok.jsonl"), "--policy", "-", "--json"]
    pipe = install(monkeypatch, raw, chunk=chunk)
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)["passed"] and pipe.consumed == 65536
    pipe = install(monkeypatch, raw + b"xextra", chunk=chunk)
    monkeypatch.setattr(cli, "write_new_reports", no_writer)
    monkeypatch.setattr(cli, "normalize", no_writer)
    assert cli.main([*args, "--junit", str(tmp_path / "report.xml")]) == 2
    captured = capsys.readouterr()
    assert not captured.out and "64 KiB" in captured.err and pipe.consumed == 65537
    assert not list(tmp_path.iterdir())


def test_double_stdin_rejected_before_buffer_access(tmp_path, monkeypatch, capsys):
    class Untouchable:
        @property
        def buffer(self):
            pytest.fail("double stdin must be rejected before buffer access")
    monkeypatch.setattr(cli.sys, "stdin", Untouchable())
    monkeypatch.setattr(cli, "load_policy", no_writer)
    monkeypatch.setattr(cli, "normalize", no_writer)
    monkeypatch.setattr(cli, "write_new_reports", no_writer)
    assert cli.main(["check", "-", "--policy", "-", "--json", "--junit", str(tmp_path / "report.xml")]) == 2
    captured = capsys.readouterr()
    assert not captured.out and "only one input may use stdin" in captured.err
    assert not list(tmp_path.iterdir())


def test_policy_dash_and_python_path_are_literal(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    Path("-").write_bytes(b"forbid_command: {pattern: pytest}")
    pipe = install(monkeypatch, b"max_commands: 10")
    args = ["check", str(FIX / "codex_exec_ok.jsonl"), "--json"]
    assert cli.main([*args, "--policy", "-"]) == 0
    assert json.loads(capsys.readouterr().out)["passed"]
    requests = list(pipe.requests)
    assert cli.main([*args, "--policy", "./-"]) == 1
    assert not json.loads(capsys.readouterr().out)["passed"]
    assert load_policy("-")[0].key == "forbid_command" and pipe.requests == requests


def test_load_policy_borrowed_stream():
    pipe = BinaryPipe(b"max_commands: 10\n", prefix=b"consumed\xff", chunk=1)
    assert load_policy(pipe)[0].value == 10
    assert pipe.consumed == 17 and pipe.read(1) == b""


@pytest.mark.parametrize("raw", [b'{"type":"turn.started"}', b'{"type":"system","subtype":"init"}'])
@pytest.mark.parametrize("rule", [b"max_commands: 10", b"require_success: true", b"max_total_tokens: 100"])
def test_policy_stdin_incomplete_override(tmp_path, monkeypatch, capsys, raw, rule):
    stream = tmp_path / "incomplete.jsonl"
    stream.write_bytes(raw)
    for allow in [False, True]:
        install(monkeypatch, rule)
        args = ["check", str(stream), "--policy", "-", "--json"] + (["--allow-incomplete"] if allow else [])
        assert cli.main(args) == (0 if allow and rule == b"max_commands: 10" else 1)
        report = json.loads(capsys.readouterr().out)
        assert not report["complete"]
        assert ("incomplete_stream" in [r["id"] for r in report["results"]]) is not allow


@pytest.mark.parametrize("blocked", ["existing", "symlink", "missing-parent", "fsync", "link", "race"])
def test_policy_stdin_junit_rollback(tmp_path, monkeypatch, capsys, blocked):
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
    install(monkeypatch, b"max_commands: 10")
    assert cli.main(["check", str(FIX / "codex_exec_ok.jsonl"), "--policy", "-", "--json", "--junit", str(output)]) == 2
    captured = capsys.readouterr()
    assert not captured.out and captured.err and not list(tmp_path.glob(".trajgate-*"))
    if blocked == "race":
        assert output.read_bytes() == b"race winner"
    else:
        assert snapshot(tmp_path) == before
