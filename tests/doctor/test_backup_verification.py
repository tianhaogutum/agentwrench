"""Read-only explicit-backup contracts: metadata only, no filesystem writes."""
import json

import pytest

from .test_cli import cli
from .test_recovery import prepare, sha, snapshot


def inventory(root):
    import stat

    result = {}
    for path in root.iterdir():
        st = path.lstat()
        metadata = st.st_dev, st.st_ino, st.st_mode, st.st_mtime_ns, st.st_ctime_ns
        content = path.readlink() if path.is_symlink() else path.read_bytes() if stat.S_ISREG(st.st_mode) else None
        result[path] = metadata, content
    return result


@pytest.mark.parametrize("fmt", ["codex", "claude"])
@pytest.mark.parametrize("current", ["missing", "modified"])
def test_verify_backup_cli_contract(tmp_path, fmt, current):
    source, manifest, backup, original = prepare(tmp_path, fmt)
    if current == "missing":
        source.unlink()
    else:
        source.write_bytes(b"PRIVATE CURRENT preserve exactly")
    before = {p: snapshot(p) for p in tmp_path.iterdir()}
    result = cli("verify-backup", manifest, "--backup", backup, "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "status": "verified", "version": 2, "manifest": str(manifest), "backup": str(backup),
        "expected_sha256": sha(original), "actual_sha256": sha(original), "verified_bytes": len(original)}
    assert before == {p: snapshot(p) for p in tmp_path.iterdir()}
    assert source.exists() == (current != "missing")
    assert "PRIVATE" not in result.stdout + result.stderr


def test_verify_backup_mismatch_contract(tmp_path):
    _, manifest, backup, _ = prepare(tmp_path, apply=False)
    backup.write_bytes(b"PRIVATE WRONG BYTES")
    before = {p: snapshot(p) for p in tmp_path.iterdir()}
    result = cli("verify-backup", manifest, "--backup", backup, "--json")
    assert result.returncode == 2, result.stdout + result.stderr
    value = json.loads(result.stdout)
    assert set(value) == {"file", "error"} and value["file"] == str(manifest)
    assert "SHA-256" in value["error"]
    assert "PRIVATE" not in result.stdout + result.stderr
    assert before == {p: snapshot(p) for p in tmp_path.iterdir()}


def test_no_writes_or_session_open_and_human_output(tmp_path, monkeypatch, capsys):
    import os
    import tempfile
    from agentwrench.doctor.cli import main

    source, manifest, backup, original = prepare(tmp_path, apply=False)
    before = {p: snapshot(p) for p in tmp_path.iterdir()}
    real_open = os.open
    opened = []

    def readonly(path, flags, *args, **kwargs):
        assert flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND) == 0
        assert path in (manifest, backup)  # Never open even a valid current session.
        opened.append(path)
        return real_open(path, flags, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("verification attempted a filesystem write")

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", readonly)
        for name in ("link", "replace", "unlink", "mkdir"):
            patch.setattr(os, name, forbidden)
        patch.setattr(tempfile, "mkstemp", forbidden)
        patch.setattr(tempfile, "mkdtemp", forbidden)
        assert main(["verify-backup", str(manifest), "--backup", str(backup)]) == 0
    assert opened == [manifest, backup]
    assert capsys.readouterr().out == f"verified: {backup} ({len(original)} bytes) SHA-256 {sha(original)}\n"
    assert before == {p: snapshot(p) for p in tmp_path.iterdir()}
    # A matching explicit copy suffices even though apply never ran.
    assert source.read_bytes() == original


@pytest.mark.parametrize("data", [b"", b"\x00\xff\r\n" + b"x" * (2 * 1024 * 1024) + b"tail"],
                         ids=["empty", "binary-multichunk"])
@pytest.mark.parametrize("fmt", ["codex", "claude"])
def test_arbitrary_bytes_are_not_parsed(tmp_path, fmt, data):
    _, manifest, backup, _ = prepare(tmp_path, fmt, apply=False)
    value = json.loads(manifest.read_text())
    value["source"]["sha256"] = sha(data)
    manifest.write_text(json.dumps(value))
    backup.write_bytes(data)
    before = {p: snapshot(p) for p in tmp_path.iterdir()}
    result = cli("verify-backup", manifest, "--backup", backup, "--json")
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["verified_bytes"] == len(data)
    assert value["expected_sha256"] == value["actual_sha256"] == sha(data)
    assert before == {p: snapshot(p) for p in tmp_path.iterdir()}


@pytest.mark.parametrize("pair", ["manifest_backup", "current_backup", "current_manifest"])
@pytest.mark.parametrize("alias", ["exact", "parent", "hardlink"])
def test_aliases_refused_without_writes(tmp_path, pair, alias):
    import os
    from .test_recovery import refuse

    source, manifest, backup, _ = prepare(tmp_path, apply=False)
    paths = {"manifest": manifest, "backup": backup, "current": source}
    left, right = pair.split("_")
    target = paths[right]
    if alias == "exact":
        paths[left] = target
    elif alias == "parent":
        parent = tmp_path / "alias"
        parent.symlink_to(tmp_path, target_is_directory=True)
        paths[left] = parent / target.name
    else:
        paths[left].unlink()
        os.link(target, paths[left])
    if left == "current":
        value = json.loads(manifest.read_text())
        value["source"]["path"] = value["output"]["path"] = value["report"]["file"] = str(paths[left])
        manifest.write_text(json.dumps(value))
    before = {p: snapshot(p) for p in (source, manifest, backup)}
    entries = set(tmp_path.iterdir())
    refuse(paths["manifest"], paths["backup"], tmp_path / "unused", "verify-backup")
    assert before == {p: snapshot(p) for p in before}
    assert set(tmp_path.iterdir()) == entries


@pytest.mark.parametrize("endpoint", ["manifest", "backup"])
def test_symlink_loop_refused(tmp_path, endpoint):
    _, manifest, backup, _ = prepare(tmp_path, apply=False)
    path = manifest if endpoint == "manifest" else backup
    path.unlink()
    path.symlink_to(path.name)
    before = inventory(tmp_path)
    result = cli("verify-backup", manifest, "--backup", backup, "--json")
    assert result.returncode == 2 and "Traceback" not in result.stderr
    assert set(json.loads(result.stdout)) == {"file", "error"}
    assert before == inventory(tmp_path)


@pytest.mark.parametrize("kind", ["symlink", "fifo", "missing_parent", "locked"])
def test_current_session_never_opened(tmp_path, kind):
    import fcntl
    import os
    from contextlib import ExitStack

    source, manifest, backup, _ = prepare(tmp_path, apply=False)
    with ExitStack() as stack:
        if kind == "locked":
            stream = stack.enter_context(source.open("rb"))
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            source.unlink()
            if kind == "symlink":
                other = tmp_path / "other-session"
                other.write_bytes(b"PRIVATE")
                source.symlink_to(other)
            elif kind == "fifo":
                os.mkfifo(source)
            else:
                value = json.loads(manifest.read_text())
                value["source"]["path"] = value["output"]["path"] = value["report"]["file"] = str(tmp_path / "absent" / "session")
                manifest.write_text(json.dumps(value))
        before = inventory(tmp_path)
        inputs = {p: snapshot(p) for p in (manifest, backup)}
        result = cli("verify-backup", manifest, "--backup", backup, "--json")
        assert result.returncode == 0, result.stderr
        assert inputs == {p: snapshot(p) for p in inputs}
        assert before == inventory(tmp_path)


@pytest.mark.parametrize("args", [[], ["--out", "unused"], ["--format", "codex"], ["--in-place"]])
def test_no_discovery_or_write_options(tmp_path, args):
    _, manifest, backup, _ = prepare(tmp_path)
    before = {p: snapshot(p) for p in tmp_path.iterdir()}
    options = [] if not args else ["--backup", backup, *args]
    result = cli("verify-backup", manifest, *options, "--json")
    assert result.returncode == 2 and not result.stdout
    assert before == {p: snapshot(p) for p in tmp_path.iterdir()}


@pytest.mark.parametrize("endpoint", ["manifest", "backup"])
@pytest.mark.parametrize("stage", ["during_read", "after_read", "final_hash"])
@pytest.mark.parametrize("change", ["rewrite", "replace", "symlink", "unlink", "metadata", "restore_mtime"])
def test_input_races_refused_and_adversary_bytes_preserved(tmp_path, monkeypatch, endpoint, stage, change):
    import os
    from agentwrench.doctor import recovery
    from agentwrench.doctor.engine import DoctorError

    source, manifest, backup, _ = prepare(tmp_path, apply=False)
    # Force multiple read chunks, also detecting first-chunk changes after consumption.
    data = b"PRIVATE\x00\xff" + b"x" * (2 * 1024 * 1024) + b"end"
    value = json.loads(manifest.read_text())
    value["source"]["sha256"] = sha(data)
    manifest.write_text(json.dumps(value))
    backup.write_bytes(data)
    path = manifest if endpoint == "manifest" else backup
    old = snapshot(path)
    current_before = snapshot(source)
    observed = None

    def mutate():
        nonlocal observed
        assert observed is None
        if change == "rewrite":
            with path.open("r+b") as target:
                target.write(b"!")
        elif change == "replace":
            replacement = tmp_path / "replacement"
            replacement.write_bytes(old[0])
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
            path.write_bytes(old[0])
            os.utime(path, ns=(path.stat().st_atime_ns, old[-2]))
        observed = inventory(tmp_path)

    real_read = recovery._read_backup
    real_fingerprint = recovery.fingerprint

    class Reader:
        def read(self, size):
            chunk = original.read(size)
            if observed is None:
                mutate()
            return chunk

    def read(stream, target=None):
        nonlocal original
        original = stream
        result = real_read(Reader() if stage == "during_read" else stream, target)
        if stage == "after_read":
            mutate()
        return result

    # fdopen streams have integer names, so identify the final hash via inode.
    def fingerprint(stream):
        digest = real_fingerprint(stream)
        if stage == "final_hash" and read_completed[0] and os.fstat(stream.fileno()).st_ino == old[2]:
            mutate()
        return digest

    original = None
    read_completed = [False]
    def read_and_mark(stream, target=None):
        result = read(stream, target)
        read_completed[0] = True
        return result

    monkeypatch.setattr(recovery, "_read_backup", read_and_mark)
    monkeypatch.setattr(recovery, "fingerprint", fingerprint)
    with pytest.raises((DoctorError, OSError)):
        recovery.verify_backup(manifest, backup)
    assert observed is not None
    assert observed == inventory(tmp_path)
    assert snapshot(source) == current_before


def test_alias_created_during_final_recheck_refused(tmp_path, monkeypatch):
    import os
    from agentwrench.doctor import recovery
    from agentwrench.doctor.engine import DoctorError

    source, manifest, backup, _ = prepare(tmp_path, apply=False)
    source.unlink()
    real = recovery.fingerprint
    calls = 0
    observed = None

    def fingerprint(stream):
        nonlocal calls, observed
        digest = real(stream)
        calls += 1
        if calls == 3:  # final manifest hash, after backup recheck
            os.link(backup, source)
            observed = {p: snapshot(p) for p in tmp_path.iterdir()}
        return digest

    monkeypatch.setattr(recovery, "fingerprint", fingerprint)
    with pytest.raises(DoctorError, match="separate"):
        recovery.verify_backup(manifest, backup)
    assert observed == {p: snapshot(p) for p in tmp_path.iterdir()}


@pytest.mark.parametrize("command", ["verify-backup", "recover"])
@pytest.mark.parametrize("endpoint", ["manifest", "backup"])
def test_parent_symlink_loop_is_json_refusal(tmp_path, command, endpoint):
    _, manifest, backup, _ = prepare(tmp_path, apply=False)
    parent = tmp_path / "loop"
    parent.symlink_to(parent.name, target_is_directory=True)
    paths = {"manifest": manifest, "backup": backup}
    paths[endpoint] = parent / paths[endpoint].name
    before = inventory(tmp_path)
    args = [command, paths["manifest"], "--backup", paths["backup"], "--json"]
    if command == "recover":
        args += ["--out", tmp_path / "new"]
    result = cli(*args)
    assert result.returncode == 2 and "Traceback" not in result.stderr
    assert set(json.loads(result.stdout)) == {"file", "error"}
    assert before == inventory(tmp_path)
