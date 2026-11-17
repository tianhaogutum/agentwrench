"""Plain handoff packs (ported from the original relayproof test suite). No optional dependencies."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from agentwrench.handoff.cli import is_pack
from agentwrench.handoff.io import HandoffError
from agentwrench.handoff.pack import capture_pack, load_pack, render_markdown, task_input, verify_pack
from agentwrench.handoff.pack_runs import freshness, load_evidence, result_status, run_evidence
from agentwrench.handoff.pack_safety import digest, json_text, read_json, redact, utf8
from agentwrench.handoff.pack_scope import diffs, output_path, repository, snapshot

from .helpers import git, subprocess_env

PY = sys.executable


@dataclass
class Fixture:
    dir: Path
    repo: str

    def write(self, name: str, data: str | bytes) -> None:
        path = Path(self.repo) / name
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data, encoding="utf-8")

    def out(self, name: str) -> str:
        return str(self.dir / name)


def make(tmp_path: Path, committed: bool = True) -> Fixture:
    base = tmp_path / "work"
    repo = base / "repo with spaces"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Sample Fixture")
    git(repo, "config", "user.email", "sample@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "config", "core.hooksPath", str(base / "no-hooks"))
    (repo / "code.txt").write_text("initial\n")
    if committed:
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "sample base")
    return Fixture(base, repository(str(repo)))


def sample_task() -> dict:
    return task_input({"goal": "Sample: finish task", "completed": ["A user claim"], "pending": ["Run tests"],
                       "blockers": [], "next": ["Inspect changes"]})


def cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([PY, "-m", "agentwrench", "handoff", *args], capture_output=True, text=True,
                          timeout=60, env=subprocess_env())


def by_path(state: dict, name: str) -> dict:
    return next(item for item in state["files"] if item["path"] == name)


def test_manifest_covers_commit_staged_unstaged_untracked_and_staged_deletion(tmp_path):
    f = make(tmp_path)
    f.write("code.txt", "staged\n")
    git(Path(f.repo), "add", "code.txt")
    f.write("code.txt", "unstaged\n")
    f.write("新 file.txt", "untracked")
    f.write("gone.txt", "delete me")
    git(Path(f.repo), "add", ".")
    git(Path(f.repo), "commit", "-qm", "add")
    git(Path(f.repo), "rm", "-q", "gone.txt")
    f.write("code.txt", "new unstaged\n")
    f.write("untracked.txt", "sample")
    state = snapshot(f.repo)
    assert len(state["head"]) in (40, 64)
    assert state["branch"] == "main"
    assert by_path(state, "code.txt")["status"] == " M"
    assert by_path(state, "untracked.txt")["status"] == "??"
    assert by_path(state, "gone.txt")["status"] == "D "
    assert by_path(state, "gone.txt")["kind"] == "missing"


def test_freshness_detects_content_index_untracked_branch_and_head_changes(tmp_path):
    f = make(tmp_path)
    repo = Path(f.repo)
    previous = snapshot(f.repo)

    def changed():
        nonlocal previous
        current = snapshot(f.repo)
        assert current["fingerprint"] != previous["fingerprint"]
        previous = current

    git(repo, "status")
    assert snapshot(f.repo)["fingerprint"] == previous["fingerprint"]
    f.write("code.txt", "modified")
    changed()
    git(repo, "add", "code.txt")
    changed()
    f.write("extra", "new")
    changed()
    (repo / "extra").unlink()
    changed()
    git(repo, "commit", "-qm", "changed")
    changed()
    git(repo, "switch", "-q", "-c", "other")
    changed()
    git(repo, "checkout", "-q", "--detach")
    changed()
    assert previous["branch"] is None


def test_failed_command_keeps_true_result_and_bound_scope_and_verify_does_not_rerun(tmp_path):
    f = make(tmp_path)
    record_file = f.out("failed.json")
    record = run_evidence(f.repo, [PY, "-c", "import sys; sys.stderr.write('sample failure'); sys.exit(1)"],
                          record_file)
    assert record["exitCode"] == 1 and result_status(record) == "failed"
    assert "sample failure" in record["stderr"]
    assert load_evidence(record_file)["id"] == record["id"]
    output = output_path(f.out("handoff"), f.repo)
    bundle = capture_pack(f.repo, sample_task(), [record_file], output)
    assert bundle["task"]["completed"]["items"][0]["verification"] == "unverified"
    assert bundle["evidence"][0]["source"] == "imported_local_record"
    assert bundle["evidence"][0]["result"] == "failed"
    assert verify_pack(f.repo, load_pack(output))["status"] == "matches_captured_scope"
    assert verify_pack(f.repo, bundle)["testsRerun"] is False
    f.write("code.txt", "later change")
    assert verify_pack(f.repo, bundle)["status"] == "stale_or_unbound"
    assert verify_pack(f.repo, bundle)["evidence"][0]["freshness"] == "stale"
    new_bundle = capture_pack(f.repo, sample_task(), [record_file], f.out("new-handoff"))
    report = verify_pack(f.repo, new_bundle)
    assert report["snapshot"]["status"] == "matches_captured_scope"
    assert report["status"] == "stale_or_unbound"
    assert "failed" in Path(output, "handoff.md").read_text(encoding="utf-8")


def test_passed_mutated_missing_signaled_and_timeout_outcomes_are_distinct(tmp_path):
    f = make(tmp_path)

    def run(name, argv, **options):
        return run_evidence(f.repo, argv, f.out(f"{name}.json"), **options)

    passed = run("pass", [PY, "-c", "print('ok')"])
    assert result_status(passed) == "passed"
    mutated = run("mutate", [PY, "-c", "open('code.txt', 'w').write('changed')"])
    assert result_status(mutated) == "passed"
    assert freshness(mutated, snapshot(f.repo)) == "mutated_during_run"
    missing = run("missing", ["relayproof-command-that-does-not-exist-6312"])
    assert result_status(missing) == "error" and missing["spawnError"] == "ENOENT"
    assert load_evidence(f.out("missing.json"))["exitCode"] is None
    timeout = run("timeout", [PY, "-c", "import time; time.sleep(10)"], timeout_ms=50)
    assert result_status(timeout) == "timeout" and timeout["timedOut"] is True
    signaled = run("signal", [PY, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"])
    assert result_status(signaled) == "signaled" and signaled["signal"] == "SIGTERM"


def test_sensitive_ignored_and_symlinked_files_are_not_bundled_and_credentials_are_redacted(tmp_path):
    f = make(tmp_path)
    f.write(".gitignore", "ignored.txt\n")
    f.write("ignored.txt", "PRIVATE_IGNORED_VALUE_6161")
    f.write(".env", "PRIVATE_ENV_VALUE_3434")
    f.write("secret.key", "PRIVATE_KEY_FILE_VALUE_2454")
    external = f.dir / "outside.txt"
    external.write_text("PRIVATE_SYMLINK_VALUE_6161")
    os.symlink(external, Path(f.repo) / "linked")
    f.write("code.txt", 'api_key="PRIVATE_DIFF_VALUE_7979"\n')
    output = f.out("bundle")
    bundle = capture_pack(f.repo, task_input({"goal": "password=PRIVATE_TASK_VALUE_0000"}), [], output)
    serialized = Path(output, "handoff.json").read_text(encoding="utf-8") + Path(output, "handoff.md").read_text(
        encoding="utf-8")
    for secret in ("PRIVATE_ENV_VALUE_3434", "PRIVATE_IGNORED_VALUE_6161", "PRIVATE_KEY_FILE_VALUE_2454",
                   "PRIVATE_SYMLINK_VALUE_6161", "PRIVATE_DIFF_VALUE_7979", "PRIVATE_TASK_VALUE_0000"):
        assert secret not in serialized, secret
    omitted = bundle["repository"]["snapshot"]["omitted"]
    assert any(item["reason"] == "symlink" for item in omitted)
    assert any(item["path"] == ".env" for item in omitted)
    assert not any(item["path"] == "ignored.txt" for item in bundle["repository"]["snapshot"]["files"])
    assert "[REDACTED]" in serialized


def test_logs_redact_tokens_and_private_keys_and_huge_output_stays_bounded(tmp_path):
    f = make(tmp_path)
    token = "ghp_" + "a" * 36
    program = (f"import sys; print('token=LOG_SECRET_VALUE'); sys.stderr.write('{token}\\n'); "
               "print('-----BEGIN PRIVATE KEY-----\\nPRIVATE_PEM_VALUE\\n-----END PRIVATE KEY-----'); "
               "print('x' * 200000)")
    record = run_evidence(f.repo, [PY, "-c", program], f.out("log.json"))
    serialized = json_text(record)
    for secret in ("LOG_SECRET_VALUE", token, "PRIVATE_PEM_VALUE"):
        assert secret not in serialized
    assert record["stdoutTruncated"] is True
    assert len(record["stdout"]) < 65000


def test_paired_credential_arguments_and_imported_evidence_are_sanitized(tmp_path):
    f = make(tmp_path)
    path = f.out("record.json")
    record = run_evidence(f.repo, [PY, "-c", "pass", "--api-key", "ARG_PRIVATE_VALUE_1759"], path)
    assert "ARG_PRIVATE_VALUE_1759" not in json_text(record)
    payload = {key: value for key, value in record.items() if key != "id"}
    payload["stdout"] = "password=IMPORT_PRIVATE_VALUE_2177"
    payload["stderr"] = "Authorization: Bearer IMPORT_BEARER_VALUE_3562"
    Path(path).write_text(json.dumps({**payload, "id": digest(payload)}))
    output = f.out("bundle")
    bundle = capture_pack(f.repo, sample_task(), [path], output)
    text = Path(output, "handoff.json").read_text(encoding="utf-8") + Path(output, "handoff.md").read_text(
        encoding="utf-8")
    assert "IMPORT_PRIVATE_VALUE_2177" not in text and "IMPORT_BEARER_VALUE_3562" not in text
    assert bundle["evidence"][0]["sanitizedOnImport"] is True
    assert verify_pack(f.repo, load_pack(output))["status"] == "matches_captured_scope"
    extra = {**payload, "untrustedExtra": "SHOULD_NEVER_BE_BUNDLED"}
    Path(path).write_text(json.dumps({**extra, "id": digest(extra)}))
    with pytest.raises(HandoffError, match="Unknown or missing"):
        load_evidence(path)


def test_timeout_stops_reading_pipes_held_by_a_detached_descendant(tmp_path):
    f = make(tmp_path)
    program = ("import subprocess, sys, time; "
               "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'], start_new_session=True); "
               "print(c.pid, flush=True); time.sleep(60)")
    record = run_evidence(f.repo, [PY, "-c", program], f.out("timeout.json"), timeout_ms=150)
    descendant = record["stdout"].strip()
    if descendant.isdigit():
        try:
            os.killpg(int(descendant), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    assert record["timedOut"] is True
    assert record["durationMs"] < 1500, record["durationMs"]


def test_unborn_head_strange_paths_rename_and_literal_pathspecs(tmp_path):
    f = make(tmp_path, committed=False)
    repo = Path(f.repo)
    assert snapshot(f.repo)["head"] is None
    names = ["中文 name.txt", "line\nbreak.txt", "-option.txt", ":(glob)**.txt"]
    for name in names:
        f.write(name, "sample\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    git(repo, "mv", "code.txt", "renamed.txt")
    f.write(":(glob)**.txt", "modified\n")
    f.write(".env", "NEVER_INCLUDE_DIFF_MAGIC")
    state = snapshot(f.repo)
    for name in names:
        assert any(item["path"] == name for item in state["files"])
    assert by_path(state, "code.txt")["status"] == "D "
    assert by_path(state, "renamed.txt")["status"] == "A "
    assert "NEVER_INCLUDE_DIFF_MAGIC" not in json_text(diffs(f.repo, state))


def test_trailing_space_paths_are_preserved_and_non_utf8_git_metadata_fails_closed(tmp_path):
    f = make(tmp_path)
    trailing = f.repo + " "
    os.rename(f.repo, trailing)
    assert repository(trailing) == trailing
    assert any(item["path"] == "code.txt" for item in snapshot(trailing)["files"])
    # Some filesystems reject non-UTF-8 file names; exercise the same strict decoder
    # with invalid bytes in a filter subsection instead.
    with open(Path(trailing, ".git", "config"), "ab") as handle:
        handle.write(b'\n[filter "\xff"]\nclean = true\n')
    with pytest.raises(HandoffError, match="non-UTF-8"):
        repository(trailing)


def test_existing_repository_and_symlinked_outputs_and_inputs_are_rejected(tmp_path):
    f = make(tmp_path)
    with pytest.raises(HandoffError, match="outside"):
        output_path(str(Path(f.repo) / "bundle"), f.repo)
    existing = f.dir / "existing"
    existing.write_text("keep")
    with pytest.raises(HandoffError, match="exists"):
        output_path(str(existing), f.repo)
    os.symlink(f.dir, f.dir / "alias")
    with pytest.raises(HandoffError, match="Symlink"):
        output_path(str(f.dir / "alias" / "new"), f.repo)
    os.symlink(existing, f.dir / "input.json")
    with pytest.raises(HandoffError, match="Symlink"):
        read_json(str(f.dir / "input.json"))
    assert existing.read_text() == "keep"


def test_git_ignored_output_inside_the_repository_is_accepted_and_out_of_scope(tmp_path):
    f = make(tmp_path)
    f.write(".gitignore", ".handoff/\n")
    git(Path(f.repo), "add", ".gitignore")
    git(Path(f.repo), "commit", "-qm", "ignore handoff output")
    (Path(f.repo) / ".handoff").mkdir()
    before = snapshot(f.repo)["fingerprint"]
    output = output_path(str(Path(f.repo) / ".handoff" / "pack-01"), f.repo)
    capture_pack(f.repo, sample_task(), [], output)
    assert snapshot(f.repo)["fingerprint"] == before
    assert verify_pack(f.repo, load_pack(output))["status"] == "matches_captured_scope"
    with pytest.raises(HandoffError, match="Git-ignored"):
        output_path(str(Path(f.repo) / "code.txt"), f.repo)


def test_malformed_and_tampered_records_are_rejected_instead_of_reporting_passed(tmp_path):
    f = make(tmp_path)
    path = f.out("record.json")
    record = run_evidence(f.repo, [PY, "-c", "raise SystemExit(1)"], path)
    Path(path).write_text(json.dumps({**record, "exitCode": 0}))
    with pytest.raises(HandoffError, match="integrity"):
        load_evidence(path)
    invalid = {**record, "exitCode": None, "signal": None, "spawnError": None, "timedOut": False}
    payload = {key: value for key, value in invalid.items() if key != "id"}
    invalid["id"] = digest(payload)
    Path(path).write_text(json.dumps(invalid))
    with pytest.raises(HandoffError, match="Missing execution"):
        load_evidence(path)
    Path(path).write_text("{bad")
    with pytest.raises(HandoffError, match="Invalid JSON"):
        load_evidence(path)


def test_git_fsmonitor_external_diff_textconv_and_clean_filters_never_execute(tmp_path):
    f = make(tmp_path)
    repo = Path(f.repo)
    marker = f.dir / "should-not-exist"
    script = f.dir / "driver.py"
    script.write_text(f"open({str(marker)!r}, 'w').write('executed')\n")
    driver = f"{PY} '{script}'"
    git(repo, "config", "core.fsmonitor", driver)
    git(repo, "config", "diff.external", driver)
    git(repo, "config", "diff.evil.textconv", driver)
    git(repo, "config", "filter.evil.clean", driver)
    git(repo, "config", "filter.evil.required", "true")
    f.write(".gitattributes", "*.txt filter=evil diff=evil\n")
    f.write("code.txt", "changed")
    root = repository(f.repo)
    diffs(root, snapshot(root))
    assert not marker.exists()


def test_cli_runs_the_complete_flow_with_meaningful_exit_codes(tmp_path):
    f = make(tmp_path)
    record = f.out("failed.json")
    result = cli("run", "--repo", f.repo, "--output", record, "--", PY, "-c", "raise SystemExit(1)")
    assert result.returncode == 1, result.stderr
    assert read_json(record)["exitCode"] == 1
    assert json.loads(result.stdout) == {"result": "failed", "exitCode": 1, "signal": None, "evidenceSaved": True}
    output = f.out("bundle")
    result = cli("pack", "--repo", f.repo, "--goal", "sample task", "--evidence", record, "--output", output)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["handoffCreated"] is True
    result = cli("verify", output, "--repo", f.repo)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "matches_captured_scope"
    f.write("new.txt", "new")
    result = cli("verify", output, "--repo", f.repo)
    assert result.returncode == 1, result.stderr
    result = cli("pack", "--repo", f.repo, "--goal", "task", "--output", output)
    assert result.returncode == 2 and "exists" in json.loads(result.stderr)["error"]
    result = cli("run", "--help")
    assert result.returncode == 0 and "usage: agentwrench handoff run" in result.stdout
    result = cli("pack", "--repo", f.repo, "--output", f.out("new"))
    assert result.returncode == 2
    result = cli("run", "--repo", f.repo, "--output", f.out("missing.json"), "--", "relayproof-nonexistent-817")
    assert result.returncode == 127


def test_unknown_progress_and_missing_tests_stay_explicitly_unknown(tmp_path):
    f = make(tmp_path)
    bundle = capture_pack(f.repo, task_input({"goal": "No assertions supplied"}), [], f.out("bundle"))
    assert bundle["task"]["completed"]["status"] == "unknown"
    assert bundle["testCoverage"]["status"] == "unknown_not_run"
    with pytest.raises(HandoffError, match="array"):
        task_input({"goal": "sample", "completed": "done"})
    with pytest.raises(HandoffError, match="Unknown"):
        task_input({"goal": "sample", "extra": True})
    assert redact("Bearer samplecredentialvalue") == "Bearer [REDACTED]"
    assert "PRIVATE_BASIC_4422" not in redact("Authorization: Basic PRIVATE_BASIC_4422")
    assert "PRIVATE_URL_4422" not in redact("postgres://sample:PRIVATE_URL_4422@localhost/db")


# ---- agentwrench additions: render, format detection, receipt-only flags, no extra needed


def test_render_reproduces_the_written_markdown(tmp_path):
    f = make(tmp_path)
    output = f.out("bundle")
    capture_pack(f.repo, sample_task(), [], output)
    written = Path(output, "handoff.md").read_bytes()
    assert utf8(render_markdown(load_pack(output))) == written
    result = subprocess.run([PY, "-m", "agentwrench", "handoff", "render", output], capture_output=True,
                            timeout=60, env=subprocess_env())
    assert result.returncode == 0 and result.stdout == written
    target = f.out("copy.md")
    assert cli("render", str(Path(output, "handoff.json")), "--output", target).returncode == 0
    assert Path(target).read_bytes() == written
    assert Path(target).stat().st_mode & 0o777 == 0o600
    assert cli("render", output, "--output", target).returncode == 2


def test_verify_detects_the_format_and_rejects_receipt_flags_for_packs(tmp_path):
    f = make(tmp_path)
    output = f.out("bundle")
    capture_pack(f.repo, sample_task(), [], output)
    assert is_pack(Path(output)) and is_pack(Path(output, "handoff.json"))
    manifest = f.dir / "receipts.json"
    manifest.write_text(json.dumps({"schema_version": "1", "created_at": "x"}, indent=2))
    assert not is_pack(manifest)
    assert is_pack(f.dir / "does-not-exist.json")
    result = cli("verify", output, "--repo", f.repo, "--require-current-pass")
    assert result.returncode == 2 and "in-toto handoffs only" in json.loads(result.stderr)["error"]


BLOCK_EXTRA = """
import sys
class Block:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("in_toto", "pydantic", "securesystemslib"):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
sys.meta_path.insert(0, Block())
from agentwrench.cli import main
raise SystemExit(main(sys.argv[1:]))
"""


def test_pack_workflow_needs_no_optional_dependency(tmp_path):
    f = make(tmp_path)

    def run(*args):
        return subprocess.run([PY, "-c", BLOCK_EXTRA, "handoff", *args], capture_output=True, text=True,
                              timeout=60, env=subprocess_env())

    record = f.out("ok.json")
    assert run("run", "--repo", f.repo, "-o", record, "--", PY, "-c", "pass").returncode == 0
    output = f.out("bundle")
    assert run("pack", "--repo", f.repo, "--goal", "g", "--evidence", record, "-o", output).returncode == 0
    result = run("verify", output, "--repo", f.repo)
    assert result.returncode == 0, result.stderr
    assert run("render", output).returncode == 0
    receipts = run("capture", "--repo", f.repo, "--task", record, "-o", f.out("receipts.json"))
    assert receipts.returncode == 2 and receipts.stdout == ""
    assert receipts.stderr.endswith("pip install 'agentwrench[handoff]'\n")
