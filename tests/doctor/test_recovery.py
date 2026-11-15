"""Verified original-byte recovery never writes the current session."""
import hashlib
import json
from pathlib import Path

import pytest

from .test_cli import cli


def sha(data):
    return hashlib.sha256(data).hexdigest()


def snapshot(path):
    st = path.stat()
    return path.read_bytes(), st.st_dev, st.st_ino, st.st_mode, st.st_mtime_ns, st.st_ctime_ns


def prepare(tmp_path, fmt="codex", *, apply=True):
    source, manifest = tmp_path / "session.jsonl", tmp_path / "plan.json"
    fixture = "codex/orphan.jsonl" if fmt == "codex" else "claude/orphan_tool_use.jsonl"
    original = (Path(__file__).parent / "fixtures" / fixture).read_bytes().replace(b"\n", b"\r\n").rstrip(b"\r\n")
    source.write_bytes(original)
    result = cli("preview", source, "--in-place", "--backup", "--manifest", manifest, "--json")
    assert result.returncode == 0, result.stderr
    if apply:
        result = cli("apply", manifest, "--json")
        assert result.returncode == 0, result.stderr
        backup = Path(json.loads(result.stdout)["backup"])
    else:
        backup = tmp_path / "explicit-backup"
        backup.write_bytes(original)
    return source, manifest, backup, original


@pytest.mark.parametrize("fmt", ["codex", "claude"])
@pytest.mark.parametrize("current", ["missing", "modified"])
def test_recovery_cli_original_bytes(tmp_path, fmt, current):
    source, manifest, backup, original = prepare(tmp_path, fmt)
    if current == "missing":
        source.unlink()
    else:
        source.write_bytes(b"CURRENT PRIVATE SESSION - preserve exactly")
    paths = [manifest, backup] + ([source] if source.exists() else [])
    before = {p: snapshot(p) for p in paths}
    output = tmp_path / "recovered.jsonl"
    result = cli("recover", manifest, "--backup", backup, "--out", output, "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "status": "recovered", "version": 2, "manifest": str(manifest), "backup": str(backup),
        "source": {"path": str(source), "sha256": sha(original)},
        "output": {"path": str(output), "sha256": sha(original)}, "restored_bytes": len(original)}
    assert output.read_bytes() == original
    assert output.stat().st_mode & 0o777 == 0o600
    assert {p: snapshot(p) for p in paths} == before
    assert source.exists() == (current != "missing")
    assert "PRIVATE" not in result.stdout + result.stderr
    assert not list(tmp_path.glob(".rollout-doctor-*"))


def refuse(manifest, backup, output, command="recover"):
    args = [command, manifest, "--backup", backup, "--json"]
    if command == "recover":
        args += ["--out", output]
    result = cli(*args)
    assert result.returncode == 2, result.stdout + result.stderr
    assert set(json.loads(result.stdout)) == {"file", "error"}
    assert not list(output.parent.glob(".rollout-doctor-*"))
    return result


@pytest.mark.parametrize("fmt", ["codex", "claude"])
def test_recovery_verifies_bytes_not_execution_provenance(tmp_path, fmt):
    # No apply ever ran; the explicit copy has no generated backup filename.
    source, manifest, backup, original = prepare(tmp_path, fmt, apply=False)
    before = {p: snapshot(p) for p in (source, manifest, backup)}
    out = tmp_path / "new"
    result = cli("recover", manifest, "--backup", backup, "--out", out)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"recovered: {out} ({len(original)} bytes) SHA-256 {sha(original)}\n"
    assert out.read_bytes() == original
    assert before == {p: snapshot(p) for p in before}
    # Original sessions may contain pairing errors; recovery is byte-for-byte.
    assert cli("check", out).returncode == 1


@pytest.mark.parametrize("mutation", [
    "v1", "version_bool", "unknown_version", "extra", "no_options", "missing_backup",
    "false_backup", "integer_backup", "false_in_place", "integer_in_place", "bad_drop_invalid",
    "bad_strategy", "bad_format", "relative_source", "relative_output", "empty_path", "nul_path",
    "bad_source_hash", "uppercase_hash", "bad_output_hash", "different_output", "missing_report",
    "report_file", "report_format", "report_changes", "duplicate_key", "invalid_json",
])
@pytest.mark.parametrize("command", ["recover", "verify-backup"])
def test_recovery_strict_manifest_contract(tmp_path, mutation, command):
    source, manifest, backup, _ = prepare(tmp_path, apply=False)
    value = json.loads(manifest.read_text())
    if mutation == "v1":
        value["version"] = 1
        del value["options"]["backup"], value["options"]["in_place"]
        value["output"]["path"] = str(tmp_path / "v1-dest")
    elif mutation == "version_bool":
        value["version"] = True
    elif mutation == "unknown_version":
        value["version"] = 3
    elif mutation == "extra":
        value["extra"] = 0
    elif mutation == "no_options":
        value["options"] = {}
    elif mutation == "missing_backup":
        del value["options"]["backup"]
    elif mutation in ("false_backup", "integer_backup", "false_in_place", "integer_in_place"):
        prefix, key = mutation.split("_", 1)
        value["options"][key] = False if prefix == "false" else 1
    elif mutation in ("bad_drop_invalid", "bad_strategy", "bad_format"):
        key = mutation.removeprefix("bad_")
        value["options"][key] = 1 if key == "drop_invalid" else "invalid"
    elif mutation in ("relative_source", "relative_output"):
        value[mutation.removeprefix("relative_")]["path"] = "relative"
    elif mutation in ("empty_path", "nul_path"):
        value["source"]["path"] = "" if mutation == "empty_path" else "/bad\0path"
    elif mutation == "bad_source_hash":
        value["source"]["sha256"] = "g" * 64
    elif mutation == "uppercase_hash":
        value["source"]["sha256"] = "A" * 64
    elif mutation == "bad_output_hash":
        value["output"]["sha256"] = "a" * 63
    elif mutation == "different_output":
        value["output"]["path"] = str(tmp_path / "elsewhere")
    elif mutation == "missing_report":
        value["report"] = {}
    elif mutation == "report_file":
        value["report"]["file"] = "elsewhere"
    elif mutation == "report_format":
        value["report"]["format"] = "unknown"
    elif mutation == "report_changes":
        value["report"]["changes"] = {}
    manifest.write_text(json.dumps(value))
    if mutation == "duplicate_key":
        manifest.write_text(manifest.read_text().replace('"version": 2', '"version": 2, "version": 2'))
    elif mutation == "invalid_json":
        manifest.write_bytes(b"not json")
    before = {p: snapshot(p) for p in (source, manifest, backup)}
    out = tmp_path / "new"
    refuse(manifest, backup, out, command)
    assert not out.exists()
    assert before == {p: snapshot(p) for p in before}


@pytest.mark.parametrize("change", ["first", "last", "truncated", "appended", "empty"])
@pytest.mark.parametrize("command", ["recover", "verify-backup"])
def test_entire_backup_hash_required(tmp_path, change, command):
    source, manifest, backup, _ = prepare(tmp_path, apply=False)
    # Exercise chunk boundaries and arbitrary original bytes without parsing them.
    original = b"private\x00\xff" + b"x" * (2 * 1024 * 1024) + b"tail"
    value = json.loads(manifest.read_text())
    value["source"]["sha256"] = sha(original)
    manifest.write_text(json.dumps(value))
    bad = {"first": b"z" + original[1:], "last": original[:-1] + b"z",
           "truncated": original[:-1], "appended": original + b"z", "empty": b""}[change]
    backup.write_bytes(bad)
    before = {p: snapshot(p) for p in (source, manifest, backup)}
    out = tmp_path / "new"
    assert "SHA-256" in json.loads(refuse(manifest, backup, out, command).stdout)["error"]
    assert not out.exists()
    assert before == {p: snapshot(p) for p in before}


@pytest.mark.parametrize("data", [b"", b"\x00\xff\r\n" + b"x" * (2 * 1024 * 1024) + b"tail"], ids=["empty", "binary-multichunk"])
def test_recovery_preserves_arbitrary_bytes_and_private_mode(tmp_path, data):
    import os
    from agentwrench.doctor.recovery import recover

    _, manifest, backup, _ = prepare(tmp_path, apply=False)
    value = json.loads(manifest.read_text())
    value["source"]["sha256"] = sha(data)
    manifest.write_text(json.dumps(value))
    backup.write_bytes(data)
    out = tmp_path / "new"
    old_umask = os.umask(0o777)
    try:
        recover(manifest, backup, out)
    finally:
        os.umask(old_umask)
    assert out.read_bytes() == data and out.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("endpoint", ["manifest", "backup"])
@pytest.mark.parametrize("kind", ["symlink", "dangling", "directory", "fifo", "missing"])
@pytest.mark.parametrize("command", ["recover", "verify-backup"])
def test_unsafe_input_endpoints(tmp_path, endpoint, kind, command):
    import os

    source, manifest, backup, _ = prepare(tmp_path, apply=False)
    path = manifest if endpoint == "manifest" else backup
    moved = tmp_path / "moved"
    path.rename(moved)
    if kind in ("symlink", "dangling"):
        path.symlink_to(moved if kind == "symlink" else tmp_path / "absent")
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    before = snapshot(source), snapshot(moved)
    out = tmp_path / "new"
    refuse(manifest, backup, out, command)
    assert not out.exists() and before == (snapshot(source), snapshot(moved))


@pytest.mark.parametrize("kind", ["matching", "different", "symlink", "dangling", "directory", "fifo"])
def test_any_existing_output_is_refused(tmp_path, kind):
    import os

    source, manifest, backup, original = prepare(tmp_path, apply=False)
    out = tmp_path / "new"
    if kind in ("matching", "different"):
        out.write_bytes(original if kind == "matching" else b"keep me")
    elif kind in ("symlink", "dangling"):
        out.symlink_to(backup if kind == "symlink" else tmp_path / "absent")
    elif kind == "directory":
        out.mkdir()
    else:
        os.mkfifo(out)
    before = out.lstat(), {p: snapshot(p) for p in (source, manifest, backup)}
    refuse(manifest, backup, out)
    assert out.lstat() == before[0]
    assert before[1] == {p: snapshot(p) for p in before[1]}


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_missing_or_non_directory_output_parent(tmp_path, kind):
    source, manifest, backup, _ = prepare(tmp_path, apply=False)
    parent = tmp_path / "parent"
    if kind == "file":
        parent.write_bytes(b"keep")
    before = {p: snapshot(p) for p in (source, manifest, backup)}
    refuse(manifest, backup, parent / "new")
    assert before == {p: snapshot(p) for p in before}


@pytest.mark.parametrize("pair", ["manifest_backup", "current_backup", "current_manifest", "out_current", "out_backup", "out_manifest"])
@pytest.mark.parametrize("alias", ["exact", "parent", "hardlink"])
def test_aliases_are_refused(tmp_path, pair, alias):
    import os

    source, manifest, backup, _ = prepare(tmp_path, apply=False)
    out = tmp_path / "new"
    paths = {"manifest": manifest, "backup": backup, "current": source, "out": out}
    left, right = pair.split("_")
    target = paths[right]
    if alias == "exact":
        paths[left] = target
    elif alias == "parent":
        parent_alias = tmp_path / "alias"
        parent_alias.symlink_to(tmp_path, target_is_directory=True)
        paths[left] = parent_alias / target.name
    else:
        paths[left].unlink(missing_ok=True)
        os.link(target, paths[left])
    if left == "current":
        value = json.loads(manifest.read_text())
        value["source"]["path"] = value["output"]["path"] = value["report"]["file"] = str(paths[left])
        manifest.write_text(json.dumps(value))
    before = {p: snapshot(p) for p in (source, manifest, backup) if p.exists()}
    refuse(paths["manifest"], paths["backup"], paths["out"])
    assert before == {p: snapshot(p) for p in before}
    if not paths["out"].exists():
        assert not out.exists()


def test_missing_current_destination_is_still_protected(tmp_path):
    source, manifest, backup, _ = prepare(tmp_path, apply=False)
    source.unlink()
    refuse(manifest, backup, source)
    assert not source.exists()


@pytest.mark.parametrize("missing_flag", ["--backup", "--out"])
def test_no_backup_discovery_or_implicit_output(tmp_path, missing_flag):
    _, manifest, backup, _ = prepare(tmp_path)
    out = tmp_path / "new"
    args = ["recover", manifest, "--json"]
    if missing_flag != "--backup":
        args += ["--backup", backup]
    if missing_flag != "--out":
        args += ["--out", out]
    before = {p: snapshot(p) for p in tmp_path.iterdir()}
    result = cli(*args)
    assert result.returncode == 2 and missing_flag in result.stderr
    assert before == {p: snapshot(p) for p in tmp_path.iterdir()}


@pytest.mark.parametrize("endpoint", ["manifest", "backup"])
@pytest.mark.parametrize("change", ["rewrite", "replace", "symlink", "unlink", "metadata", "restore_mtime"])
def test_input_changes_before_publication_refused(tmp_path, monkeypatch, endpoint, change):
    import os
    from agentwrench.doctor import recovery
    from agentwrench.doctor.engine import DoctorError

    source, manifest, backup, _ = prepare(tmp_path, apply=False)
    out = tmp_path / "new"
    current_before = snapshot(source)
    path = manifest if endpoint == "manifest" else backup
    before = snapshot(path)
    real_fsync = os.fsync
    injected = False

    def changed(fd):
        nonlocal injected
        real_fsync(fd)
        if injected:
            return
        injected = True
        if change == "rewrite":
            path.write_bytes(before[0] + b" ")
        elif change == "replace":
            replacement = tmp_path / "replacement"
            replacement.write_bytes(before[0])
            os.replace(replacement, path)
        elif change == "symlink":
            moved = tmp_path / "moved"
            path.rename(moved)
            path.symlink_to(moved)
        elif change == "unlink":
            path.unlink()
        elif change == "metadata":
            path.chmod(0o400)
        else:
            path.write_bytes(before[0])
            os.utime(path, ns=(path.stat().st_atime_ns, before[-2]))

    monkeypatch.setattr(os, "fsync", changed)
    with pytest.raises((DoctorError, OSError)):
        recovery.recover(manifest, backup, out)
    assert injected and not out.exists()
    assert snapshot(source) == current_before
    assert not list(tmp_path.glob(".rollout-doctor-*"))


@pytest.mark.parametrize("endpoint", ["manifest", "backup"])
def test_change_during_final_content_recheck_refused(tmp_path, monkeypatch, endpoint):
    from agentwrench.doctor import recovery
    from agentwrench.doctor.engine import DoctorError

    _, manifest, backup, _ = prepare(tmp_path, apply=False)
    path = manifest if endpoint == "manifest" else backup
    out = tmp_path / "new"
    real_fingerprint = recovery.fingerprint
    calls = 0

    def changed(stream):
        nonlocal calls
        calls += 1
        digest = real_fingerprint(stream)
        # Calls: initial manifest, final backup, final manifest.
        if calls == (2 if endpoint == "backup" else 3):
            path.write_bytes(path.read_bytes() + b" ")
        return digest

    monkeypatch.setattr(recovery, "fingerprint", changed)
    with pytest.raises(DoctorError, match="changed"):
        recovery.recover(manifest, backup, out)
    assert not out.exists() and not list(tmp_path.glob(".rollout-doctor-*"))


@pytest.mark.parametrize("kind", ["regular", "symlink"])
def test_concurrent_output_never_clobbered(tmp_path, monkeypatch, kind):
    import os
    from agentwrench.doctor import recovery

    source, manifest, backup, _ = prepare(tmp_path, apply=False)
    out = tmp_path / "new"
    before = {p: snapshot(p) for p in (source, manifest, backup)}
    real_link = os.link

    def compete(src, dst, **kwargs):
        if kind == "regular":
            out.write_bytes(b"concurrent winner")
        else:
            out.symlink_to(backup)
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(os, "link", compete)
    with pytest.raises(FileExistsError):
        recovery.recover(manifest, backup, out)
    assert (out.is_symlink() and out.readlink() == backup) if kind == "symlink" else out.read_bytes() == b"concurrent winner"
    assert before == {p: snapshot(p) for p in before}
    assert not list(tmp_path.glob(".rollout-doctor-*"))


@pytest.mark.parametrize("stage", ["file_fsync", "link", "directory_fsync"])
def test_publication_failure_cleanup_and_completed_output(tmp_path, monkeypatch, stage):
    import os
    from agentwrench.doctor import recovery

    source, manifest, backup, original = prepare(tmp_path, apply=False)
    out = tmp_path / "new"
    before = {p: snapshot(p) for p in (source, manifest, backup)}

    def fail(*args, **kwargs):
        raise OSError("injected publication failure")

    if stage == "file_fsync":
        monkeypatch.setattr(os, "fsync", fail)
    elif stage == "link":
        monkeypatch.setattr(os, "link", fail)
    else:
        monkeypatch.setattr(recovery, "_fsync_dir", fail)
    with pytest.raises(OSError, match="injected"):
        recovery.recover(manifest, backup, out)
    assert before == {p: snapshot(p) for p in before}
    assert not list(tmp_path.glob(".rollout-doctor-*"))
    if stage == "directory_fsync":
        assert out.read_bytes() == original and out.stat().st_mode & 0o777 == 0o600
        refuse(manifest, backup, out)
    else:
        assert not out.exists()


@pytest.mark.parametrize("endpoint", ["manifest", "backup"])
@pytest.mark.parametrize("command", ["recover", "verify-backup"])
def test_locked_inputs_refused(tmp_path, endpoint, command):
    import fcntl

    source, manifest, backup, _ = prepare(tmp_path, apply=False)
    out = tmp_path / "new"
    before = {p: snapshot(p) for p in (source, manifest, backup)}
    with (manifest if endpoint == "manifest" else backup).open("rb") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = refuse(manifest, backup, out, command)
        assert "locked" in json.loads(result.stdout)["error"]
    assert not out.exists() and before == {p: snapshot(p) for p in before}


@pytest.mark.parametrize("kind", ["symlink", "fifo", "missing_parent"])
def test_current_session_is_never_opened(tmp_path, kind):
    import os

    source, manifest, backup, original = prepare(tmp_path, apply=False)
    source.unlink()
    if kind == "symlink":
        other = tmp_path / "other-session"
        other.write_bytes(b"preserve")
        source.symlink_to(other)
    elif kind == "fifo":
        os.mkfifo(source)
    else:
        value = json.loads(manifest.read_text())
        value["source"]["path"] = value["output"]["path"] = value["report"]["file"] = str(tmp_path / "missing-parent" / "current")
        manifest.write_text(json.dumps(value))
    before = source.lstat() if kind != "missing_parent" else None
    out = tmp_path / "new"
    result = cli("recover", manifest, "--backup", backup, "--out", out, "--json")
    assert result.returncode == 0, result.stderr
    assert out.read_bytes() == original
    if before:
        assert source.lstat() == before
    if kind == "symlink":
        assert other.read_bytes() == b"preserve"


@pytest.mark.parametrize("endpoint", ["manifest", "backup", "out"])
def test_symlink_loop_is_operational_refusal(tmp_path, endpoint):
    _, manifest, backup, _ = prepare(tmp_path, apply=False)
    out = tmp_path / "new"
    path = {"manifest": manifest, "backup": backup, "out": out}[endpoint]
    path.unlink(missing_ok=True)
    path.symlink_to(path.name)
    result = refuse(manifest, backup, out)
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("endpoint", ["manifest", "backup"])
@pytest.mark.parametrize("command", ["recover", "verify-backup"])
def test_device_input_refused(tmp_path, endpoint, command):
    _, manifest, backup, _ = prepare(tmp_path, apply=False)
    out = tmp_path / "new"
    refuse(Path("/dev/null") if endpoint == "manifest" else manifest,
           Path("/dev/null") if endpoint == "backup" else backup, out, command)
    assert not out.exists()


@pytest.mark.parametrize("data", [b"", b"PRIVATE\x00\xff\r\n\xe5\x9b\x9b", b"PRIVATE\x00\xff" + b"x" * (2 * 1024 * 1024) + b"tail"],
                         ids=["empty", "binary", "multichunk"])
@pytest.mark.parametrize("current", ["missing", "modified"])
@pytest.mark.parametrize("json_output", [True, False], ids=["json", "human"])
def test_recovery_receipt_counts_exact_restored_bytes(tmp_path, data, current, json_output):
    # A trusted synthetic manifest may bind arbitrary original bytes. No apply or
    # current-source match is necessary, and byte count must not count characters.
    source, manifest, backup, _ = prepare(tmp_path, apply=False)
    value = json.loads(manifest.read_text())
    value["source"]["sha256"] = sha(data)
    manifest.write_text(json.dumps(value))
    backup.write_bytes(data)
    if current == "missing":
        source.unlink()
    else:
        source.write_bytes(b"PRIVATE-CURRENT must stay unchanged")
    before = {p: snapshot(p) for p in tmp_path.iterdir()}
    output = tmp_path / "restored"
    args = ["recover", manifest, "--backup", backup, "--out", output]
    if json_output:
        args.append("--json")
    result = cli(*args)
    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == data
    assert output.stat().st_mode & 0o777 == 0o600
    assert before == {p: snapshot(p) for p in before}
    assert set(tmp_path.iterdir()) == {*before, output}
    assert "PRIVATE" not in result.stdout + result.stderr
    if json_output:
        assert json.loads(result.stdout) == {
            "status": "recovered", "version": 2, "manifest": str(manifest), "backup": str(backup),
            "source": {"path": str(source), "sha256": sha(data)},
            "output": {"path": str(output), "sha256": sha(data)}, "restored_bytes": len(data)}
    else:
        assert result.stdout == f"recovered: {output} ({len(data)} bytes) SHA-256 {sha(data)}\n"
