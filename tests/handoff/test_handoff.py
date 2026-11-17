from __future__ import annotations

import base64
import json
import os
import subprocess
import sys

import pytest

pytest.importorskip("in_toto")
pytest.importorskip("pydantic")

from pydantic import ValidationError
from securesystemslib.signer import CryptoSigner

from agentwrench.handoff.cli import main
from agentwrench.handoff.core import capture, read_manifest, record, verify
from agentwrench.handoff.evidence import assess, load_evidence, load_keys
from agentwrench.handoff.git import snapshot
from agentwrench.handoff.io import HandoffError, atomic_json, parse_json

from .helpers import FIXTURES, git

CHECK = [sys.executable, "-B", "-c", "from retry import attempts; assert attempts() == 5"]


def test_record_capture_and_current_pass(repo, task, tmp_path):
    log = tmp_path / "check.link"
    code, recorded = record(repo=repo, argv=CHECK, output=log)
    assert code == 0
    assert recorded["source_unchanged"]
    output = tmp_path / "handoff.json"
    package = capture(repo=repo, task_path=task, logs=[log], output=output)
    result = verify(read_manifest(output), repo=repo, keys=[], require_current_pass=True)
    assert result["ok"]
    assert result["evidence"][0]["current_pass"]
    assert result["evidence"][0]["source_binding"] == "same_source_state"
    assert result["task_completion"] == "not_assessed"
    assert package.task_claims.unresolved == ["Decide jitter policy"]
    assert package.snapshot.head == git(repo, "rev-parse", "HEAD").decode().strip()
    assert package.verification[0].argv[:3] == ["agentwrench", "handoff", "verify"]


@pytest.mark.parametrize("filename,signed", [
    ("upstream-unsigned.link", False), ("upstream-signed.link", True),
    ("upstream-dsse.link", True),
    ("upstream-cli-run.link", True),
])
def test_real_upstream_fixtures_remain_unbound(repo, filename, signed):
    evidence = load_evidence(FIXTURES / filename)
    result = assess(evidence, repo, snapshot(repo), [])
    assert result["reported_result"] == "passed"
    assert result["declared_files_match"]
    assert result["source_binding"] == "unbound"
    assert not result["current_pass"]
    assert result["signature"] == ("unverified" if signed else "unsigned")
    trusted = assess(evidence, repo, snapshot(repo), load_keys([FIXTURES / "trusted-key.json"]))
    assert trusted["signature"] == ("verified_with_supplied_key" if signed else "no_trusted_signature")


def test_real_split_record_cli_does_not_invent_a_command_result(repo):
    evidence = load_evidence(FIXTURES / "upstream-cli-record.link")
    result = assess(evidence, repo, snapshot(repo), load_keys([FIXTURES / "trusted-key.json"]))
    assert result["declared_files_match"]
    assert result["signature"] == "verified_with_supplied_key"
    assert result["command_argv"] == []
    assert result["reported_result"] == "unknown"
    assert not result["current_pass"]


def test_real_in_toto_2_3_rsa_metadata(repo):
    evidence = load_evidence(FIXTURES / "upstream-v2.3.link")
    result = assess(evidence, repo, snapshot(repo), load_keys([FIXTURES / "trusted-v2-key.json"]))
    assert result["signature"] == "verified_with_supplied_key"
    assert result["reported_result"] == "passed"
    assert result["declared_files_match"]
    assert result["source_binding"] == "unbound"


def test_drift_reports_individual_files_index_and_patches(repo, task, tmp_path):
    output = tmp_path / "handoff.json"
    capture(repo=repo, task_path=task, logs=[], output=output)
    (repo / "retry.py").write_text("def attempts():\n    return 7\n")
    git(repo, "add", "retry.py")
    (repo / "retry.py").write_text("def attempts():\n    return 8\n")
    (repo / "new file.txt").write_text("untracked\n")
    result = verify(read_manifest(output), repo=repo, keys=[])
    assert not result["ok"]
    kinds = {d["kind"] for d in result["differences"]}
    assert {"source_file", "index", "staged_diff", "unstaged_diff", "artifact"} <= kinds
    assert {d.get("path") for d in result["differences"]} >= {"retry.py", "new file.txt"}


def test_new_commit_invalidates_even_identical_source(repo, task, tmp_path):
    output = tmp_path / "handoff.json"
    capture(repo=repo, task_path=task, logs=[], output=output)
    git(repo, "commit", "--allow-empty", "-qm", "new HEAD")
    result = verify(read_manifest(output), repo=repo, keys=[])
    assert not result["ok"]
    assert any(d["kind"] == "head" for d in result["differences"])


def test_native_binary_diffs_and_base_range(repo, task, tmp_path):
    base = git(repo, "rev-parse", "HEAD").decode().strip()
    (repo / "binary.dat").write_bytes(bytes(range(256)))
    git(repo, "add", "binary.dat")
    git(repo, "commit", "-qm", "add binary")
    (repo / "binary.dat").write_bytes(b"\x00changed\xff")
    git(repo, "add", "binary.dat")
    (repo / "binary.dat").write_bytes(b"\x00unstaged\xff")
    package = capture(repo=repo, task_path=task, logs=[], output=tmp_path / "handoff.json", base=base)
    assert b"GIT binary patch" in package.change_range.committed_diff.bytes()
    assert b"GIT binary patch" in package.snapshot.staged_diff.bytes()
    assert b"GIT binary patch" in package.snapshot.unstaged_diff.bytes()


def test_imported_diff_must_match(repo, task, tmp_path):
    (repo / "retry.py").write_text("def attempts():\n    return 6\n")
    diff = tmp_path / "change.patch"
    diff.write_bytes(git(repo, "diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames", "HEAD", "--"))
    package = capture(repo=repo, task_path=task, logs=[], output=tmp_path / "good.json", supplied_diff=diff)
    assert package.supplied_diff.bytes() == diff.read_bytes()
    diff.write_text("unrelated\n")
    with pytest.raises(HandoffError, match="supplied diff"):
        capture(repo=repo, task_path=task, logs=[], output=tmp_path / "bad.json", supplied_diff=diff)


def test_command_failure_is_preserved_without_claiming_completion(repo, task, tmp_path):
    log = tmp_path / "failed.link"
    code, _ = record(repo=repo, argv=[sys.executable, "-c", "raise SystemExit(7)"], output=log)
    assert code == 7
    package = capture(repo=repo, task_path=task, logs=[log], output=tmp_path / "handoff.json")
    result = verify(package, repo=repo, keys=[])
    assert result["ok"]  # A valid handoff can contain a failed command.
    assert result["evidence"][0]["reported_result"] == "failed"
    assert not verify(package, repo=repo, keys=[], require_current_pass=True)["ok"]


def test_command_that_edits_source_cannot_be_a_current_pass(repo, task, tmp_path):
    log = tmp_path / "mutating.link"
    record(repo=repo, argv=[sys.executable, "-c", "from pathlib import Path; Path('retry.py').write_text('changed')"], output=log)
    package = capture(repo=repo, task_path=task, logs=[log], output=tmp_path / "handoff.json")
    result = verify(package, repo=repo, keys=[])["evidence"][0]
    assert result["source_binding"] == "source_changed_during_command"
    assert not result["current_pass"]


@pytest.mark.parametrize("dsse", [False, True])
def test_signed_record_authenticates_state_receipt(repo, task, tmp_path, dsse):
    signer = CryptoSigner.generate_ed25519()
    private = tmp_path / "key.pem"
    private.write_bytes(signer.private_bytes)
    key = signer.public_key.to_dict()
    key["keyid"] = signer.public_key.keyid
    log = tmp_path / "signed.link"
    record(repo=repo, argv=CHECK, output=log, signing_key=private, dsse=dsse)
    package = capture(repo=repo, task_path=task, logs=[log], output=tmp_path / "handoff.json")
    result = verify(package, repo=repo, keys=[key], require_signed=True, require_current_pass=True)
    assert result["ok"]
    assert result["evidence"][0]["signature"] == "verified_with_supplied_key"
    other = CryptoSigner.generate_ed25519().public_key
    wrong = other.to_dict()
    wrong["keyid"] = other.keyid
    assert not verify(package, repo=repo, keys=[wrong], require_signed=True)["ok"]


def test_ignored_product_and_task_artifact_drift(repo, task, tmp_path):
    task.write_text(json.dumps({"goal": "Build", "artifacts": ["build/result.bin"]}))
    (repo / "build").mkdir()
    log = tmp_path / "build.link"
    record(repo=repo, argv=[sys.executable, "-c", "from pathlib import Path; Path('build/result.bin').write_bytes(b'build')"], output=log, products=["build/result.bin"])
    package = capture(repo=repo, task_path=task, logs=[log], output=tmp_path / "handoff.json")
    assert verify(package, repo=repo, keys=[], require_current_pass=True)["ok"]
    (repo / "build/result.bin").write_bytes(b"corrupt")
    result = verify(package, repo=repo, keys=[])
    assert not result["ok"]
    assert {"kind": "artifact", "path": "build/result.bin"} in result["differences"]
    assert not result["evidence"][0]["declared_files_match"]


def test_verify_never_executes_carried_commands(repo, task, tmp_path):
    sentinel = tmp_path / "executed"
    raw = json.loads((FIXTURES / "upstream-unsigned.link").read_text())
    raw["signed"]["command"] = [sys.executable, "-c", f"open({str(sentinel)!r}, 'w').write('bad')"]
    log = tmp_path / "untrusted.link"
    log.write_text(json.dumps(raw))
    package = capture(repo=repo, task_path=task, logs=[log], output=tmp_path / "handoff.json")
    assert verify(package, repo=repo, keys=[])["ok"]
    assert not sentinel.exists()


def test_manifest_integrity_and_embedded_hash_checks(repo, task, tmp_path):
    output = tmp_path / "handoff.json"
    package = capture(repo=repo, task_path=task, logs=[], output=output)
    package.task_claims.goal = "forged"
    with pytest.raises(HandoffError, match="integrity"):
        verify(package, repo=repo, keys=[])
    package.seal()
    package.snapshot.staged_diff.data = base64.b64encode(b"forged patch").decode()
    package.seal()
    with pytest.raises(HandoffError, match="hash or length"):
        package.check()


@pytest.mark.parametrize("path", ["../outside", "/absolute", ".git/config", "a/../../escape", "C:\\escape", "a//b"])
def test_untrusted_receipt_paths_are_rejected(tmp_path, path):
    raw = json.loads((FIXTURES / "upstream-unsigned.link").read_text())
    raw["signed"]["materials"] = {path: {"sha256": "0" * 64}}
    log = tmp_path / "unsafe.link"
    log.write_text(json.dumps(raw))
    with pytest.raises(HandoffError, match="repository-relative"):
        load_evidence(log)


def test_symlinks_are_snapshotted_without_following(repo, task, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("external content")
    (repo / "link").symlink_to(outside)
    state = snapshot(repo)
    assert next(f for f in state.files if f.path == "link").kind == "symlink"
    outside.write_text("different external content")
    assert snapshot(repo).state_sha256 == state.state_sha256
    with pytest.raises(HandoffError, match="source symlinks"):
        record(repo=repo, argv=CHECK, output=tmp_path / "check.link")


def test_symlink_ancestor_never_reads_outside(repo, task, tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    (external / "secret").write_text("do not read")
    (repo / "linked-dir").symlink_to(external, target_is_directory=True)
    task.write_text(json.dumps({"goal": "Safe", "artifacts": ["linked-dir/secret"]}))
    with pytest.raises(HandoffError, match="symlink ancestor"):
        capture(repo=repo, task_path=task, logs=[], output=tmp_path / "handoff.json")


def test_unusual_names_modes_and_missing_files(repo):
    for name in ["spaces and ü.txt", "line\nbreak.txt", "-leading", "[brackets].txt"]:
        (repo / name).write_text("real file")
    state = snapshot(repo)
    assert {f.path for f in state.files} >= {"line\nbreak.txt", "spaces and ü.txt", "-leading", "[brackets].txt"}
    os.chmod(repo / "retry.py", 0o755)
    (repo / ".gitignore").unlink()
    changed = snapshot(repo)
    assert changed.state_sha256 != state.state_sha256
    assert next(f for f in changed.files if f.path == ".gitignore").kind == "missing"


def test_output_must_not_change_source_state(repo, task, tmp_path):
    with pytest.raises(HandoffError, match="Git-ignored"):
        capture(repo=repo, task_path=task, logs=[], output=repo / "handoff.json")
    with pytest.raises(HandoffError, match="tracked"):
        capture(repo=repo, task_path=task, logs=[], output=repo / "retry.py")
    output = repo / ".evidence/handoff.json"
    capture(repo=repo, task_path=task, logs=[], output=output)
    assert verify(read_manifest(output), repo=repo, keys=[])["ok"]


def test_bundle_only_checks_integrity_but_not_current_applicability(repo, task, tmp_path):
    log = tmp_path / "check.link"
    record(repo=repo, argv=CHECK, output=log)
    package = capture(repo=repo, task_path=task, logs=[log], output=tmp_path / "handoff.json")
    result = verify(package, repo=None, keys=[])
    assert result["ok"]
    assert result["repository"] == "not_checked"
    assert result["evidence"][0]["source_binding"] == "not_checked"
    assert not result["evidence"][0]["current_pass"]
    assert not verify(package, repo=None, keys=[], require_current_pass=True)["ok"]


def test_invalid_json_and_cli_errors_do_not_echo_source(tmp_path, capsys):
    marker = "SUPER_SECRET_MARKER"
    bad = tmp_path / "bad.json"
    bad.write_text('{"secret": "' + marker + '",')
    assert main(["verify", str(bad), "--bundle-only"]) == 2
    assert marker not in capsys.readouterr().err
    with pytest.raises(HandoffError, match="duplicate"):
        parse_json(b'{"key": 1, "key": 2}')
    with pytest.raises(HandoffError, match="non-finite"):
        parse_json(b'{"key": NaN}')


def test_timeout_has_no_completed_receipt(repo, tmp_path):
    log = tmp_path / "timeout.link"
    with pytest.raises(subprocess.TimeoutExpired):
        record(repo=repo, argv=[sys.executable, "-c", "import time; time.sleep(3)"], output=log, timeout=1)
    assert not log.exists()


def test_schema_is_strict_and_atomic_output_is_private(repo, task, tmp_path):
    package = capture(repo=repo, task_path=task, logs=[], output=tmp_path / "handoff.json")
    bad = package.model_dump()
    bad["invented_complete"] = True
    with pytest.raises(ValidationError):
        type(package).model_validate(bad)
    path = tmp_path / "private.json"
    atomic_json(path, {"x": 1})
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(HandoffError, match="already exists"):
        atomic_json(path, {"x": 2})
