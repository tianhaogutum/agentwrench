import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agentwrench.doctor.cli import main
from agentwrench.doctor.engine import DoctorError, scan
from agentwrench.doctor.files import open_session, publish
from agentwrench.doctor.repair import make_plan
from .test_engine import raw
from .make_fixtures import cnode, item, result, use

FIXTURES = Path(__file__).parent / "fixtures"


def cli(*args):
    return subprocess.run([sys.executable, "-m", "agentwrench", "doctor", *map(str, args)], capture_output=True, text=True)


def test_cli_end_to_end_and_machine_schema(tmp_path):
    original = FIXTURES / "codex/orphan.jsonl"
    out = tmp_path / "fixed.jsonl"
    bad = cli("check", original, "--json")
    assert bad.returncode == 1
    report = json.loads(bad.stdout)
    assert set(report) == {"file", "format", "issues", "changes"}
    assert {issue["code"] for issue in report["issues"]} == {"RD101", "RD103"}
    assert report["changes"] == []
    repair = cli("repair", original, "--out", out, "--json")
    assert repair.returncode == 0, repair.stderr
    assert len(json.loads(repair.stdout)["changes"]) == 2
    clean = cli("check", out, "--json")
    assert clean.returncode == 0 and json.loads(clean.stdout)["issues"] == []
    again = cli("repair", out, "--in-place", "--backup", "--json")
    assert again.returncode == 0 and json.loads(again.stdout)["changes"] == []
    assert not list(tmp_path.glob("*.bak-*"))


def test_claude_real_format_fixture_e2e(tmp_path):
    out = tmp_path / "claude.jsonl"
    run = cli("check", FIXTURES / "claude/orphan_tool_use.jsonl", "--json")
    assert run.returncode == 1
    assert json.loads(run.stdout)["issues"][0]["code"] == "RD201"
    run = cli("repair", FIXTURES / "claude/orphan_tool_use.jsonl", "--out", out)
    assert run.returncode == 0
    assert cli("check", out).returncode == 0
    assert json.loads(out.read_text().splitlines()[1])["message"]["content"][0]["is_error"] is True


def test_dry_run_diff_no_writes(tmp_path):
    path = tmp_path / "orphan.jsonl"
    original = (FIXTURES / "codex/orphan.jsonl").read_bytes()
    path.write_bytes(original)
    before = {p.name: p.stat().st_mtime_ns for p in tmp_path.iterdir()}
    run = cli("repair", path, "--in-place", "--backup", "--dry-run")
    assert run.returncode == 0
    assert "@@" in run.stdout and "+" in run.stdout and "tool output missing" in run.stdout
    assert path.read_bytes() == original
    assert before == {p.name: p.stat().st_mtime_ns for p in tmp_path.iterdir()}
    run = cli("repair", path, "--dry-run", "--json")
    assert json.loads(run.stdout)["dry_run"] is True
    assert "@@" not in run.stdout


def test_in_place_always_backup_before_replacement(tmp_path):
    path = tmp_path / "session.jsonl"
    original = (FIXTURES / "codex/orphan.jsonl").read_bytes()
    path.write_bytes(original)
    path.chmod(0o640)
    run = cli("repair", path, "--in-place", "--backup", "--json")
    assert run.returncode == 0, run.stderr
    backups = list(tmp_path.glob("session.jsonl.bak-*Z"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert backups[0].stat().st_mode & 0o777 == 0o600
    assert path.stat().st_mode & 0o777 == 0o640
    assert cli("check", path).returncode == 0
    assert not list(tmp_path.glob(".rollout-doctor-*"))


@pytest.mark.parametrize("args", [[], ["check"], ["repair"], ["check", "--format", "bad", "x"],
                                   ["repair", "x", "--in-place"], ["repair", "x", "--backup"],
                                   ["repair", "x"], ["repair", "x", "--out", "a", "--in-place"]])
def test_invalid_cli_is_exit_2(args):
    run = cli(*args)
    assert run.returncode == 2
    assert "error:" in run.stderr and "Traceback" not in run.stderr


def test_unreadable_and_unknown_are_exit_2(tmp_path):
    assert cli("check", tmp_path / "missing").returncode == 2
    path = tmp_path / "unknown.jsonl"
    path.write_text('{"hello":"world"}\n')
    run = cli("check", path, "--json")
    assert run.returncode == 2
    assert "cannot detect" in json.loads(run.stdout)["error"]
    assert cli("check", path, "--format", "codex").returncode == 0


def test_directory_aggregate_continues_after_bad_file(tmp_path):
    (tmp_path / "a.jsonl").write_bytes((FIXTURES / "codex/clean.jsonl").read_bytes())
    sub = tmp_path / "nested"
    sub.mkdir()
    (sub / "b.jsonl").write_bytes((FIXTURES / "claude/orphan_tool_use.jsonl").read_bytes())
    (sub / "ignore.txt").write_text("not json")
    run = cli("check", tmp_path, "--json")
    report = json.loads(run.stdout)
    assert run.returncode == 1 and len(report["files"]) == 2 and report["errors"] == []
    (tmp_path / "c.jsonl").write_text("unknown\n")
    run = cli("check", tmp_path, "--json")
    report = json.loads(run.stdout)
    assert run.returncode == 2 and len(report["files"]) == 2 and len(report["errors"]) == 1


def test_empty_directory_clean(tmp_path):
    run = cli("check", tmp_path, "--json")
    assert run.returncode == 0 and json.loads(run.stdout) == {"files": [], "errors": []}


def test_refuse_existing_output_and_symlinks(tmp_path):
    source = tmp_path / "source.jsonl"
    source.write_bytes((FIXTURES / "codex/orphan.jsonl").read_bytes())
    out = tmp_path / "out.jsonl"
    out.write_bytes(b"keep this")
    run = cli("repair", source, "--out", out)
    assert run.returncode == 2 and out.read_bytes() == b"keep this"
    link = tmp_path / "link.jsonl"
    link.symlink_to(source)
    assert cli("check", link).returncode == 2
    assert cli("repair", link, "--in-place", "--backup").returncode == 2
    out.unlink()
    out.symlink_to(tmp_path / "absent")
    assert cli("repair", source, "--out", out).returncode == 2
    assert not (tmp_path / "absent").exists()


def test_symlink_input_reports_tool_refusal_not_errno(tmp_path):
    source = tmp_path / "source.jsonl"
    source.write_bytes((FIXTURES / "codex/clean.jsonl").read_bytes())
    link = tmp_path / "link.jsonl"
    link.symlink_to(source)
    repair = cli("repair", link, "--dry-run")
    assert repair.returncode == 2
    assert "symlink inputs are not supported" in repair.stderr
    assert "Errno" not in repair.stderr and "symbolic links" not in repair.stderr
    scan_dir = tmp_path / "scan"
    scan_dir.mkdir()
    (scan_dir / "linked.jsonl").symlink_to(source)
    run = cli("check", scan_dir, "--json")
    assert run.returncode == 2
    assert json.loads(run.stdout)["errors"] == [
        {"file": str(scan_dir / "linked.jsonl"), "error": "symlink inputs are not supported"}]


def test_fifo_does_not_block_or_get_read(tmp_path):
    path = tmp_path / "pipe"
    os.mkfifo(path)
    with pytest.raises(DoctorError, match="regular files"):
        with open_session(path):
            pytest.fail("must not open FIFO")


def test_cooperating_writer_lock_refused(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_bytes(b"")
    with path.open("rb") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run = cli("repair", path, "--in-place", "--backup")
        assert run.returncode == 2 and "locked" in run.stderr


def test_recent_file_warning_does_not_pollute_json(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.touch()
    run = cli("check", path, "--json")
    assert run.returncode == 0
    assert "live session" in run.stderr
    assert json.loads(run.stdout)["issues"] == []


def test_source_modified_after_scan_aborts_publication(tmp_path):
    source_path = tmp_path / "source.jsonl"
    source_path.write_bytes((FIXTURES / "codex/orphan.jsonl").read_bytes())
    out = tmp_path / "fixed.jsonl"
    with open_session(source_path, write=True) as source:
        plan = make_plan(scan(source, str(source_path)))
        source_path.write_bytes(raw(item("message", role="user", content=[])))
        with pytest.raises(DoctorError, match="source changed"):
            publish(source, source_path, out, plan, False)
    assert not out.exists() and not list(tmp_path.glob(".rollout-doctor-*"))


def test_failed_backup_cannot_replace_source(tmp_path, monkeypatch):
    source_path = tmp_path / "source.jsonl"
    original = (FIXTURES / "codex/orphan.jsonl").read_bytes()
    source_path.write_bytes(original)
    real_open = os.open
    def fail_backup(path, *args, **kwargs):
        if ".bak-" in str(path):
            raise PermissionError("simulated unavailable backup destination")
        return real_open(path, *args, **kwargs)
    with open_session(source_path, write=True) as source:
        plan = make_plan(scan(source, str(source_path)))
        monkeypatch.setattr(os, "open", fail_backup)
        with pytest.raises(PermissionError):
            publish(source, source_path, source_path, plan, True)
    assert source_path.read_bytes() == original
    assert not list(tmp_path.glob(".rollout-doctor-*"))


def test_payload_commands_are_never_executed(tmp_path):
    marker = tmp_path / "do-not-create"
    source = tmp_path / "input.jsonl"
    source.write_bytes(raw(item("function_call", "evil", name="shell", arguments=json.dumps({"command": f"touch {marker}"}))))
    assert cli("repair", source, "--dry-run", "--json").returncode == 0
    assert cli("repair", source, "--out", tmp_path / "out.jsonl").returncode == 0
    assert not marker.exists()


def test_drop_invalid_and_truncation_cli(tmp_path):
    out = tmp_path / "fixed.jsonl"
    source = FIXTURES / "codex/RD001.jsonl"
    assert cli("repair", source, "--out", out).returncode == 2
    assert not out.exists()
    assert cli("repair", source, "--drop-invalid", "--out", out).returncode == 0
    assert cli("check", out).returncode == 0
    out.unlink()
    assert cli("repair", FIXTURES / "codex/RD002.jsonl", "--out", out).returncode == 0
    assert cli("check", out).returncode == 0


def test_json_plan_excludes_argument_and_output_secrets(tmp_path):
    path = tmp_path / "private.jsonl"
    path.write_bytes(raw(item("function_call", "safe-id", name="shell", arguments="SYNTHETIC_SECRET_ARGUMENT"),
                         item("function_call_output", output="SYNTHETIC_SECRET_OUTPUT")))
    for args in [("check", path, "--json"), ("repair", path, "--dry-run", "--json")]:
        run = cli(*args)
        json.loads(run.stdout)
        assert "SYNTHETIC_SECRET" not in run.stdout + run.stderr


def test_directory_scan_does_not_follow_symlink_directory(tmp_path):
    scan_dir = tmp_path / "scan"
    outside = tmp_path / "outside"
    scan_dir.mkdir()
    outside.mkdir()
    (outside / "bad.jsonl").write_text("unknown format\n")
    (scan_dir / "linked").symlink_to(outside, target_is_directory=True)
    run = cli("check", scan_dir, "--json")
    assert run.returncode == 0
    assert json.loads(run.stdout) == {"files": [], "errors": []}


def test_dry_run_out_does_not_create_output(tmp_path):
    out = tmp_path / "fixed.jsonl"
    run = cli("repair", FIXTURES / "codex/orphan.jsonl", "--dry-run", "--out", out, "--json")
    assert run.returncode == 0
    assert json.loads(run.stdout)["clean_after"] is True
    assert not out.exists()
    assert list(tmp_path.iterdir()) == []
