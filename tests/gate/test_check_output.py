"""Saved policy JSON is the existing report, optionally committed with JUnit."""
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

import agentwrench.gate.cli as cli
import agentwrench.gate.streams as streams
from .test_normalize_stdin import install, no_writer, snapshot

FIX = Path(__file__).parent / "fixtures"
BASE = ["check", str(FIX / "codex_exec_ok.jsonl"), "--policy", str(FIX / "policy.yaml"), "--json"]


@pytest.mark.parametrize("fixture", ["codex_exec_ok.jsonl", "claude_stream_ok.jsonl"])
@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("layout", ["files", "log-stdin", "policy-stdin"])
@pytest.mark.parametrize("paired", [False, True])
def test_saved_check_matches_stdout(tmp_path, monkeypatch, capsys, fixture, failed, layout, paired):
    raw = b" \r\n" + (FIX / fixture).read_bytes().replace(b"Done", "Done ☃😀".encode()).replace(b"\n", b"\r\n").rstrip(b"\r\n") + b'\r\n{"type":"future"}'
    policy_raw = ("# ☃😀\r\n" + ("forbid_command: {pattern: pytest}" if failed else
                  "require_success: true\nrequire_final_message: {pattern: '^Done'}")).encode()
    stream, policy = tmp_path / "log", tmp_path / "policy"
    stream.write_bytes(raw)
    policy.write_bytes(policy_raw)
    expected_xml, actual_xml = tmp_path / "expected.xml", tmp_path / "actual.xml"
    args = ["--root", "/workspace/demo", "--json"]
    assert cli.main(["check", str(stream), "--policy", str(policy), *args, "--junit", str(expected_xml)]) == int(failed)
    expected = capsys.readouterr()
    pipe = None
    if layout == "log-stdin":
        pipe = install(monkeypatch, raw, prefix=b"consumed\xff", chunk=3)
    elif layout == "policy-stdin":
        pipe = install(monkeypatch, policy_raw, prefix=b"consumed\xff", chunk=1)
    output = tmp_path / "report.json"
    assert cli.main(["check", "-" if layout == "log-stdin" else str(stream), "--policy",
                     "-" if layout == "policy-stdin" else str(policy), *args,
                     "--output", str(output)] + (["--junit", str(actual_xml)] if paired else [])) == int(failed)
    actual = capsys.readouterr()
    assert not actual.out and not actual.err and not expected.err
    assert output.read_bytes() == expected.out.encode()
    assert output.stat().st_mode & 0o777 == 0o600
    report = json.loads(output.read_bytes())
    assert report["passed"] is not failed and report["complete"]
    assert report["ignored"] == {"future": 1}
    if paired:
        assert actual_xml.read_bytes() == expected_xml.read_bytes()
        assert actual_xml.stat().st_mode & 0o777 == 0o600
        xml = ET.parse(actual_xml).getroot()
        assert int(xml.get("tests")) == report["summary"]["total"]
        assert int(xml.get("failures")) == report["summary"]["failed"] == int(failed)
        for rule, case in zip(report["results"], xml.findall("testcase"), strict=True):
            assert case.get("name") == rule["id"]
            assert "evidence event indexes=" + json.dumps(rule["evidence"]) in case[0].text
    else:
        assert not actual_xml.exists()
    assert not list(tmp_path.glob(".trajgate-*"))
    if pipe:
        assert pipe.consumed == len(raw if layout == "log-stdin" else policy_raw)
        assert pipe.read(1) == b""


@pytest.mark.parametrize("layout", ["files", "log-stdin", "policy-stdin"])
@pytest.mark.parametrize("allow", [False, True])
def test_saved_check_incomplete(tmp_path, monkeypatch, capsys, layout, allow):
    raw, policy_raw = b'{"type":"turn.started"}', b"max_commands: 10"
    stream, policy = tmp_path / "log", tmp_path / "policy"
    stream.write_bytes(raw)
    policy.write_bytes(policy_raw)
    install(monkeypatch, raw if layout == "log-stdin" else policy_raw)
    output, xml = tmp_path / "report.json", tmp_path / "report.xml"
    args = ["check", "-" if layout == "log-stdin" else str(stream), "--policy",
            "-" if layout == "policy-stdin" else str(policy), "--json", "-o", str(output), "--junit", str(xml)]
    assert cli.main(args + (["--allow-incomplete"] if allow else [])) == int(not allow)
    captured = capsys.readouterr()
    assert not captured.out and not captured.err
    report = json.loads(output.read_bytes())
    assert not report["complete"] and report["passed"] is allow
    assert ("incomplete_stream" in [r["id"] for r in report["results"]]) is not allow
    assert int(ET.parse(xml).getroot().get("failures")) == int(not allow)


@pytest.mark.parametrize("case", ["no-json", "double-stdin"])
def test_saved_check_argument_errors_before_input(tmp_path, monkeypatch, capsys, case):
    monkeypatch.setattr(cli.sys, "stdin", object())  # Even buffer access is forbidden.
    monkeypatch.setattr(cli, "load_policy", no_writer)
    monkeypatch.setattr(cli, "normalize", no_writer)
    monkeypatch.setattr(cli, "write_new_reports", no_writer)
    args = ["check", "-", "--policy", "-", "-o", str(tmp_path / "report.json"), "--junit", str(tmp_path / "report.xml")]
    if case == "no-json":
        with pytest.raises(SystemExit) as exc:
            cli.main(args)
        assert exc.value.code == 2
    else:
        assert cli.main([*args, "--json"]) == 2
    captured = capsys.readouterr()
    assert not captured.out and ("requires --json" if case == "no-json" else "only one input") in captured.err
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("case", ["bad-log", "bad-policy", "log-read", "policy-read"])
def test_saved_check_invalid_before_staging(tmp_path, monkeypatch, capsys, case):
    stream, policy = tmp_path / "log", tmp_path / "policy"
    stream.write_bytes(b'{"type":' if case == "bad-log" else (FIX / "codex_exec_ok.jsonl").read_bytes())
    policy.write_text("typo: true" if case == "bad-policy" else "max_commands: 10")
    pipe = install(monkeypatch, b"unused", fail_after=0)
    monkeypatch.setattr(cli, "write_new_reports", no_writer)
    before = snapshot(tmp_path)
    assert cli.main(["check", "-" if case == "log-read" else str(stream), "--policy",
                     "-" if case == "policy-read" else str(policy), "--json", "-o", str(tmp_path / "report.json"),
                     "--junit", str(tmp_path / "report.xml")]) == 2
    captured = capsys.readouterr()
    assert not captured.out and captured.err and snapshot(tmp_path) == before
    if case.startswith("bad"):
        assert not pipe.requests


@pytest.mark.parametrize("limit", ["MAX_STREAM_BYTES", "MAX_LINE_BYTES", "MAX_LINES"])
def test_saved_check_stream_bounds(tmp_path, monkeypatch, capsys, limit):
    raw = b' \r\n{"type":"turn.completed"}\r\n'
    cap = {"MAX_STREAM_BYTES": len(raw), "MAX_LINE_BYTES": len(raw) - 3, "MAX_LINES": 2}[limit]
    policy = tmp_path / "policy"
    policy.write_text("max_commands: 10")
    monkeypatch.setattr(streams, limit, cap)
    install(monkeypatch, raw, chunk=1)
    assert cli.main(["check", "-", "--policy", str(policy), "--json", "-o", str(tmp_path / "accepted")]) == 0
    assert json.loads((tmp_path / "accepted").read_bytes())["complete"]
    capsys.readouterr()
    before = snapshot(tmp_path)
    monkeypatch.setattr(streams, limit, cap - 1)
    install(monkeypatch, raw)
    monkeypatch.setattr(cli, "write_new_reports", no_writer)
    assert cli.main(["check", "-", "--policy", str(policy), "--json", "-o", str(tmp_path / "absent")]) == 2
    captured = capsys.readouterr()
    assert not captured.out and "stream exceeds limit" in captured.err and snapshot(tmp_path) == before


@pytest.mark.parametrize("bad", ["object", "circular", "nan", "junit"])
@pytest.mark.parametrize("saved", [False, True])
def test_serialization_finishes_before_staging(tmp_path, monkeypatch, capsys, bad, saved):
    check = cli.check
    def corrupt(*args):
        report = check(*args)
        if bad == "object":
            report["extra"] = object()
        elif bad == "circular":
            report["extra"] = report
        elif bad == "nan":
            report["extra"] = float("nan")
        return report
    monkeypatch.setattr(cli, "check", corrupt)
    if bad == "junit":
        def broken(*args):
            raise ValueError("synthetic XML serialization failure")
        monkeypatch.setattr(cli, "junit", broken)
    monkeypatch.setattr(cli, "write_new_reports", no_writer)
    assert cli.main([*BASE, "--junit", str(tmp_path / "report.xml")] + (["-o", str(tmp_path / "report.json")] if saved else [])) == 2
    captured = capsys.readouterr()
    assert not captured.out and "serialize" in captured.err and not list(tmp_path.iterdir())


@pytest.mark.parametrize("position", [0, 1])
@pytest.mark.parametrize("blocked", ["existing", "symlink", "missing-parent", "write", "fsync", "link", "race"])
def test_saved_check_group_rollback(tmp_path, monkeypatch, capsys, position, blocked):
    outputs = [tmp_path / "report.json", tmp_path / "report.xml"]
    output = outputs[position]
    if blocked == "existing":
        output.write_bytes(b"keep")
    elif blocked == "symlink":
        output.symlink_to(tmp_path / "absent")
    elif blocked == "missing-parent":
        outputs[position] = tmp_path / "missing" / output.name
    elif blocked == "write":
        real_temp = cli.tempfile.NamedTemporaryFile
        count = 0
        class BrokenFile:
            def __init__(self, handle):
                self.handle, self.name = handle, handle.name
            def __enter__(self):
                self.handle.__enter__()
                return self
            def __exit__(self, *args):
                return self.handle.__exit__(*args)
            def write(self, content):
                self.handle.write(content[:3])
                raise OSError("synthetic write failure")
        def temporary(*args, **kwargs):
            nonlocal count
            handle = real_temp(*args, **kwargs)
            count += 1
            return BrokenFile(handle) if count == position + 1 else handle
        monkeypatch.setattr(cli.tempfile, "NamedTemporaryFile", temporary)
    elif blocked == "fsync":
        real_fsync = cli.os.fsync
        count = 0
        def fsync(fd):
            nonlocal count
            count += 1
            if count == position + 1:
                raise OSError("synthetic fsync failure")
            real_fsync(fd)
        monkeypatch.setattr(cli.os, "fsync", fsync)
    elif blocked in {"link", "race"}:
        real_link = cli.os.link
        def link(src, dst):
            if Path(dst) == output:
                if blocked == "race":
                    Path(dst).write_bytes(b"race winner")
                else:
                    raise OSError("synthetic link failure")
            real_link(src, dst)
        monkeypatch.setattr(cli.os, "link", link)
    before = snapshot(tmp_path)
    assert cli.main([*BASE, "-o", str(outputs[0]), "--junit", str(outputs[1])]) == 2
    captured = capsys.readouterr()
    assert not captured.out and captured.err and not list(tmp_path.glob(".trajgate-*"))
    if blocked == "race":
        assert output.read_bytes() == b"race winner" and not outputs[1 - position].exists()
    else:
        assert snapshot(tmp_path) == before


def test_group_rollback_preserves_replaced_created_file(tmp_path, monkeypatch, capsys):
    output, xml = tmp_path / "report.json", tmp_path / "report.xml"
    real_link = cli.os.link
    def link(src, dst):
        if Path(dst) == xml:
            output.unlink()
            output.write_bytes(b"replacement winner")
            raise OSError("synthetic second-link failure")
        real_link(src, dst)
    monkeypatch.setattr(cli.os, "link", link)
    assert cli.main([*BASE, "-o", str(output), "--junit", str(xml)]) == 2
    captured = capsys.readouterr()
    assert not captured.out and captured.err
    assert output.read_bytes() == b"replacement winner" and not xml.exists()
    assert not list(tmp_path.glob(".trajgate-*"))


def test_saved_check_duplicate_destination(tmp_path, capsys):
    output = tmp_path / "report"
    assert cli.main([*BASE, "-o", str(output), "--junit", str(output)]) == 2
    captured = capsys.readouterr()
    assert not captured.out and captured.err and not list(tmp_path.iterdir())


def test_saved_check_policy_stdin_real_cap(tmp_path, monkeypatch, capsys):
    prefix = b"max_commands: 10\n#"
    raw = prefix + b"x" * (65536 - len(prefix))
    pipe = install(monkeypatch, raw, chunk=7)
    output = tmp_path / "accepted"
    args = ["check", str(FIX / "codex_exec_ok.jsonl"), "--policy", "-", "--json"]
    assert cli.main([*args, "-o", str(output)]) == 0
    assert json.loads(output.read_bytes())["passed"] and pipe.consumed == 65536
    capsys.readouterr()
    before = snapshot(tmp_path)
    pipe = install(monkeypatch, raw + b"xextra", chunk=7)
    monkeypatch.setattr(cli, "write_new_reports", no_writer)
    assert cli.main([*args, "-o", str(tmp_path / "absent"), "--junit", str(tmp_path / "absent.xml")]) == 2
    captured = capsys.readouterr()
    assert not captured.out and "64 KiB" in captured.err and pipe.consumed == 65537
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("collision", ["log", "policy", "symlink-to-file", "directory"])
def test_saved_check_preserves_existing_inputs(tmp_path, capsys, collision):
    stream, policy = tmp_path / "log", tmp_path / "policy"
    stream.write_bytes((FIX / "codex_exec_ok.jsonl").read_bytes())
    policy.write_text("max_commands: 10")
    output = tmp_path / "output"
    if collision == "log":
        output = stream
    elif collision == "policy":
        output = policy
    elif collision == "symlink-to-file":
        output.symlink_to(stream)
    else:
        output.mkdir()
    originals = stream.read_bytes(), policy.read_bytes()
    assert cli.main(["check", str(stream), "--policy", str(policy), "--json", "-o", str(output), "--junit", str(tmp_path / "absent.xml")]) == 2
    captured = capsys.readouterr()
    assert not captured.out and captured.err
    assert originals == (stream.read_bytes(), policy.read_bytes())
    assert not (tmp_path / "absent.xml").exists() and not list(tmp_path.glob(".trajgate-*"))
    if collision == "symlink-to-file":
        assert output.is_symlink()


def test_saved_check_cross_directory_rollback(tmp_path, monkeypatch, capsys):
    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir()
    right.mkdir()
    output, xml = left / "report.json", right / "report.xml"
    xml.write_bytes(b"keep")
    assert cli.main([*BASE, "-o", str(output), "--junit", str(xml)]) == 2
    captured = capsys.readouterr()
    assert not captured.out and captured.err
    assert not list(left.iterdir()) and list(right.iterdir()) == [xml]
    assert xml.read_bytes() == b"keep"
