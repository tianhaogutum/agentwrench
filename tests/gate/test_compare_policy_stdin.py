"""Comparison consumes and fingerprints one bounded, borrowed policy snapshot."""
import hashlib
import json
from pathlib import Path

import pytest

import agentwrench.gate.cli as cli
import agentwrench.gate.compare as comparison
from .test_normalize_stdin import BinaryPipe, install, no_writer, snapshot

FIX = Path(__file__).parent / "fixtures"
BASE = ["compare", str(FIX / "compare_baseline.json"), str(FIX / "compare_candidate.json")]


@pytest.mark.parametrize("unit,count", [("task", 1), ("rule", 4)])
@pytest.mark.parametrize("allowed", [False, True])
@pytest.mark.parametrize("destination", ["stdout", "file"])
def test_compare_policy_stdin_matches_file(tmp_path, monkeypatch, capsys, unit, count, allowed, destination):
    raw = "# ☃😀\r\n".encode() + (FIX / "policy.yaml").read_bytes().replace(b"\n", b"\r\n").rstrip()
    policy = tmp_path / "policy.yaml"
    policy.write_bytes(raw)
    args = [*BASE, "--regression-unit", unit, "--max-regressions", str(count if allowed else count - 1)]
    assert cli.main([*args, "--policy", str(policy)]) == int(not allowed)
    expected = capsys.readouterr()
    pipe = install(monkeypatch, raw, prefix=b"consumed garbage\xff", chunk=1)
    output = tmp_path / "report.json"
    assert cli.main([*args, "--policy", "-"] + (["-o", str(output)] if destination == "file" else [])) == int(not allowed)
    actual = capsys.readouterr()
    assert not actual.err
    data = output.read_bytes() if destination == "file" else actual.out.encode()
    assert data == expected.out.encode()
    report = json.loads(data)
    assert report["policy_sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["threshold"]["observed_regressions"] == count
    assert report["summary"]["regressed"] == 1
    assert report["tasks"][1]["candidate"]["results"]
    if unit == "rule":
        assert report["rule_summary"]["regressed"] == 4
        assert sum(r["outcome"] == "regressed" for r in report["tasks"][1]["rules"]) == 4
    assert pipe.consumed == len(raw) and pipe.read(1) == b""
    if destination == "file":
        assert not actual.out and output.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("raw,diagnostic", [
    (b"", "expected an object"), (b" #comment\r\n", "expected an object"),
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
@pytest.mark.parametrize("destination", ["stdout", "file"])
def test_compare_invalid_policy_before_evaluation_or_staging(tmp_path, monkeypatch, capsys, raw, diagnostic, destination):
    monkeypatch.chdir(tmp_path)
    install(monkeypatch, raw)
    monkeypatch.setattr(comparison, "_evaluate", no_writer)
    monkeypatch.setattr(cli, "write_new_reports", no_writer)
    before = snapshot(tmp_path)
    assert cli.main([*BASE, "--policy", "-"] + (["-o", "absent.json"] if destination == "file" else [])) == 2
    captured = capsys.readouterr()
    assert not captured.out and diagnostic in captured.err and snapshot(tmp_path) == before


@pytest.mark.parametrize("fail_after", [0, 7, 28, 35])
def test_compare_policy_read_failure(tmp_path, monkeypatch, capsys, fail_after):
    install(monkeypatch, b"max_commands: 10\n#" + b"x" * 20, fail_after=fail_after)
    monkeypatch.setattr(comparison, "_evaluate", no_writer)
    monkeypatch.setattr(cli, "write_new_reports", no_writer)
    assert cli.main([*BASE, "--policy", "-", "-o", str(tmp_path / "absent.json")]) == 2
    captured = capsys.readouterr()
    assert not captured.out and "synthetic raw stdin read failure" in captured.err
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("chunk", [1, 13, 65537])
def test_compare_policy_real_cap(tmp_path, monkeypatch, capsys, chunk):
    start = b"max_commands: 10\n# "
    raw = start + b"x" * (65536 - len(start))
    pipe = install(monkeypatch, raw, chunk=chunk)
    assert cli.main([*BASE, "--policy", "-"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["policy_sha256"] == hashlib.sha256(raw).hexdigest() and pipe.consumed == 65536
    pipe = install(monkeypatch, raw + b"xextra", chunk=chunk)
    monkeypatch.setattr(comparison, "_evaluate", no_writer)
    monkeypatch.setattr(cli, "write_new_reports", no_writer)
    assert cli.main([*BASE, "--policy", "-", "-o", str(tmp_path / "absent.json")]) == 2
    captured = capsys.readouterr()
    assert not captured.out and "64 KiB" in captured.err and pipe.consumed == 65537
    assert not list(tmp_path.iterdir())


def test_compare_api_snapshot_and_literal_path(tmp_path, monkeypatch):
    raw = b"# exact snapshot\r\nmax_commands: 10"
    policy = tmp_path / "policy.yaml"
    policy.write_bytes(raw)
    expected = comparison.compare(BASE[1], BASE[2], policy, regression_unit="rule")
    pipe = BinaryPipe(raw, prefix=b"consumed\xff", chunk=1)
    assert comparison.compare(BASE[1], BASE[2], pipe, regression_unit="rule") == expected
    assert pipe.consumed == len(raw) and pipe.read(1) == b""
    monkeypatch.chdir(tmp_path)
    Path("-").write_bytes(raw)
    assert comparison.compare(BASE[1], BASE[2], "-", regression_unit="rule") == expected
    # Evaluation may mutate the file, but rules and fingerprint must share the already-read bytes.
    evaluate = comparison._evaluate
    def mutate(*args):
        policy.write_bytes(b"max_commands: 0")
        return evaluate(*args)
    monkeypatch.setattr(comparison, "_evaluate", mutate)
    assert comparison.compare(BASE[1], BASE[2], policy, regression_unit="rule") == expected


def test_compare_policy_dash_is_cli_stdin(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    Path("-").write_bytes(b"max_commands: 0")
    pipe = install(monkeypatch, b"max_commands: 10")
    assert cli.main([*BASE, "--policy", "-"]) == 0
    assert json.loads(capsys.readouterr().out)["policy_sha256"] == hashlib.sha256(b"max_commands: 10").hexdigest()
    requests = list(pipe.requests)
    assert cli.main([*BASE, "--policy", "./-"]) == 0
    assert json.loads(capsys.readouterr().out)["policy_sha256"] == hashlib.sha256(b"max_commands: 0").hexdigest()
    assert pipe.requests == requests


def test_compare_manifests_and_relative_dash_log_remain_files(tmp_path, monkeypatch, capsys):
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    (manifests / "-").write_bytes((FIX / "codex_exec_ok.jsonl").read_bytes())
    manifest = json.dumps({"schema_version": 1, "tasks": [{"id": "literal", "stream": "-"}]})
    (tmp_path / "-").write_text(manifest.replace('"stream": "-"', '"stream": "manifests/-"'))
    (manifests / "candidate.json").write_text(manifest)
    monkeypatch.chdir(tmp_path)
    install(monkeypatch, b"require_success: true")
    assert cli.main(["compare", "-", str(manifests / "candidate.json"), "--policy", "-"]) == 0
    report = json.loads(capsys.readouterr().out)
    row = report["tasks"][0]
    assert row["baseline"]["trajectory_sha256"] == row["candidate"]["trajectory_sha256"]
    assert row["outcome"] == "unchanged_pass"


@pytest.mark.parametrize("blocked", ["existing", "symlink", "missing-parent", "fsync", "link", "race"])
def test_compare_policy_stdin_output_rollback(tmp_path, monkeypatch, capsys, blocked):
    output = tmp_path / "report.json"
    if blocked == "existing":
        output.write_bytes(b"keep")
    elif blocked == "symlink":
        output.symlink_to(tmp_path / "absent")
    elif blocked == "missing-parent":
        output = tmp_path / "missing" / "report.json"
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
    assert cli.main([*BASE, "--policy", "-", "-o", str(output)]) == 2
    captured = capsys.readouterr()
    assert not captured.out and captured.err and not list(tmp_path.glob(".trajgate-*"))
    if blocked == "race":
        assert output.read_bytes() == b"race winner"
    else:
        assert snapshot(tmp_path) == before
