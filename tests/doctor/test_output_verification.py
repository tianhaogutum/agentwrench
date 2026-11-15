"""Explicit v1/v2 output verification: hash agreement without original source bytes."""
import json
from pathlib import Path

import pytest

from .test_cli import cli
from .test_recovery import sha, snapshot
from .test_backup_verification import inventory


def prepare(tmp_path, fmt="codex", *, apply=True):
    source, manifest, candidate = (tmp_path / name for name in ("session", "plan", "candidate"))
    fixture = "codex/orphan.jsonl" if fmt == "codex" else "claude/orphan_tool_use.jsonl"
    original = (Path(__file__).parent / "fixtures" / fixture).read_bytes().replace(b"\n", b"\r\n").rstrip(b"\r\n")
    source.write_bytes(original)
    result = cli("preview", source, "--out", candidate, "--manifest", manifest, "--json")
    assert result.returncode == 0, result.stderr
    result = cli("apply", manifest, "--json") if apply else cli("repair", source, "--out", candidate, "--json")
    assert result.returncode == 0, result.stderr
    return source, manifest, candidate, candidate.read_bytes()


@pytest.mark.parametrize("fmt", ["codex", "claude"])
@pytest.mark.parametrize("current", ["missing", "modified"])
def test_verify_output_cli_contract(tmp_path, fmt, current):
    source, manifest, candidate, repaired = prepare(tmp_path, fmt)
    if current == "missing":
        source.unlink()
    else:
        source.write_bytes(b"PRIVATE CURRENT preserve exactly")
    before = inventory(tmp_path)
    result = cli("verify-output", manifest, "--candidate", candidate, "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "status": "verified", "version": 1, "manifest": str(manifest), "candidate": str(candidate),
        "expected_sha256": sha(repaired), "actual_sha256": sha(repaired), "verified_bytes": len(repaired)}
    assert before == inventory(tmp_path)
    assert source.exists() == (current != "missing")
    assert "PRIVATE" not in result.stdout + result.stderr


def test_verify_output_mismatch_contract(tmp_path):
    _, manifest, candidate, _ = prepare(tmp_path)
    candidate.write_bytes(b"PRIVATE WRONG BYTES")
    before = inventory(tmp_path)
    result = cli("verify-output", manifest, "--candidate", candidate, "--json")
    assert result.returncode == 2, result.stdout + result.stderr
    value = json.loads(result.stdout)
    assert set(value) == {"file", "error"} and value["file"] == str(manifest)
    assert "SHA-256" in value["error"]
    assert "PRIVATE" not in result.stdout + result.stderr
    assert before == inventory(tmp_path)


@pytest.mark.parametrize("mode", ["v1", "v2copy", "v2source"])
@pytest.mark.parametrize("check_pairing", [False, True])
def test_no_writes_or_session_open_and_human_output(tmp_path, monkeypatch, capsys, mode, check_pairing):
    import os
    import tempfile
    from agentwrench.doctor.cli import main
    from agentwrench.doctor import cli as cli_module, manifest as manifest_module

    source, manifest, candidate, repaired = prepare_mode(tmp_path, mode)
    before = {p: snapshot(p) for p in tmp_path.iterdir()}
    real_open = os.open
    opened = []

    def readonly(path, flags, *args, **kwargs):
        assert flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND) == 0
        assert path in (manifest, candidate)  # Only the two explicit inputs may be opened.
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
        patch.setattr(cli_module, "scan", forbidden)
        patch.setattr(manifest_module, "scan", forbidden)
        patch.setattr(manifest_module, "apply", forbidden)
        assert main(["verify-output", str(manifest), "--candidate", str(candidate),
                     *(["--check-pairing"] if check_pairing else [])]) == 0
    assert opened == [manifest, candidate]
    suffix = "; pairing clean [codex]" if check_pairing else ""
    assert capsys.readouterr().out == f"verified: {candidate} ({len(repaired)} bytes) SHA-256 {sha(repaired)}{suffix}\n"
    assert before == {p: snapshot(p) for p in tmp_path.iterdir()}
    # Independent repair produced these bytes; manifest apply never ran.
    if mode != "v2source":
        assert source.read_bytes() != repaired
    candidate.write_bytes(b"PRIVATE WRONG BYTES")
    before = inventory(tmp_path)
    opened.clear()
    with monkeypatch.context() as patch:
        patch.setattr(os, "open", readonly)
        for name in ("link", "replace", "unlink", "mkdir"):
            patch.setattr(os, name, forbidden)
        patch.setattr(tempfile, "mkstemp", forbidden)
        assert main(["verify-output", str(manifest), "--candidate", str(candidate), "--json"]) == 2
    output = capsys.readouterr()
    assert set(json.loads(output.out)) == {"file", "error"}
    assert "PRIVATE" not in output.out + output.err
    assert opened == [manifest, candidate]
    assert before == inventory(tmp_path)


@pytest.mark.parametrize("data", [b"", b"\x00\xff\r\n" + b"x" * (2 * 1024 * 1024) + b"tail"],
                         ids=["empty", "binary-multichunk"])
@pytest.mark.parametrize("fmt", ["codex", "claude"])
@pytest.mark.parametrize("mode", ["v1", "v2copy", "v2source"])
def test_arbitrary_bytes_are_not_parsed(tmp_path, fmt, data, mode):
    _, manifest, candidate, _ = prepare_mode(tmp_path, mode, fmt)
    value = json.loads(manifest.read_text())
    value["output"]["sha256"] = sha(data)
    manifest.write_text(json.dumps(value))
    candidate.write_bytes(data)
    before = {p: snapshot(p) for p in tmp_path.iterdir()}
    result = cli("verify-output", manifest, "--candidate", candidate, "--json")
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["verified_bytes"] == len(data)
    assert value["expected_sha256"] == value["actual_sha256"] == sha(data)
    assert before == {p: snapshot(p) for p in tmp_path.iterdir()}


@pytest.mark.parametrize("pair", ["manifest_candidate", "current_candidate", "current_manifest"])
@pytest.mark.parametrize("alias", ["exact", "parent", "hardlink"])
def test_aliases_refused_without_writes(tmp_path, pair, alias):
    import os

    source, manifest, candidate, _ = prepare(tmp_path, apply=False)
    value = json.loads(manifest.read_text())
    value["output"]["path"] = str(tmp_path / "intended-output")
    manifest.write_text(json.dumps(value))
    paths = {"manifest": manifest, "candidate": candidate, "current": source}
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
        value["source"]["path"] = value["report"]["file"] = str(paths[left])
        manifest.write_text(json.dumps(value))
    before = {p: snapshot(p) for p in (source, manifest, candidate)}
    entries = set(tmp_path.iterdir())
    refuse(paths["manifest"], paths["candidate"])
    assert before == {p: snapshot(p) for p in before}
    assert set(tmp_path.iterdir()) == entries


@pytest.mark.parametrize("endpoint", ["manifest", "candidate"])
def test_symlink_loop_refused(tmp_path, endpoint):
    _, manifest, candidate, _ = prepare(tmp_path, apply=False)
    path = manifest if endpoint == "manifest" else candidate
    path.unlink()
    path.symlink_to(path.name)
    before = inventory(tmp_path)
    result = cli("verify-output", manifest, "--candidate", candidate, "--json")
    assert result.returncode == 2 and "Traceback" not in result.stderr
    assert set(json.loads(result.stdout)) == {"file", "error"}
    assert before == inventory(tmp_path)


@pytest.mark.parametrize("kind", ["symlink", "fifo", "missing_parent", "locked"])
@pytest.mark.parametrize("mode", ["v1", "v2copy"])
def test_current_session_never_opened(tmp_path, kind, mode):
    import fcntl
    import os
    from contextlib import ExitStack

    source, manifest, candidate, _ = prepare_mode(tmp_path, mode)
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
                value["source"]["path"] = value["report"]["file"] = str(tmp_path / "absent" / "session")
                if mode == "v2copy":
                    value["output"]["path"] = value["source"]["path"]
                manifest.write_text(json.dumps(value))
        before = inventory(tmp_path)
        inputs = {p: snapshot(p) for p in (manifest, candidate)}
        result = cli("verify-output", manifest, "--candidate", candidate, "--json")
        assert result.returncode == 0, result.stderr
        assert inputs == {p: snapshot(p) for p in inputs}
        assert before == inventory(tmp_path)


@pytest.mark.parametrize("args", [[], ["--out", "unused"], ["--format", "codex"], ["--in-place"]])
def test_no_discovery_or_write_options(tmp_path, args):
    _, manifest, candidate, _ = prepare(tmp_path)
    before = {p: snapshot(p) for p in tmp_path.iterdir()}
    options = [] if not args else ["--candidate", candidate, *args]
    result = cli("verify-output", manifest, *options, "--json")
    assert result.returncode == 2 and not result.stdout
    assert before == {p: snapshot(p) for p in tmp_path.iterdir()}


@pytest.mark.parametrize("endpoint", ["manifest", "candidate"])
@pytest.mark.parametrize("stage", ["during_read", "after_read", "final_hash"])
@pytest.mark.parametrize("change", ["rewrite", "replace", "symlink", "unlink", "metadata", "restore_mtime"])
@pytest.mark.parametrize("mode", ["v1", "v2copy", "v2source"])
def test_input_races_refused_and_adversary_bytes_preserved(tmp_path, monkeypatch, endpoint, stage, change, mode):
    import os
    from agentwrench.doctor import recovery
    from agentwrench.doctor.engine import DoctorError

    source, manifest, candidate, _ = prepare_mode(tmp_path, mode)
    # Force multiple read chunks, also detecting first-chunk changes after consumption.
    data = b"PRIVATE\x00\xff" + b"x" * (2 * 1024 * 1024) + b"end"
    value = json.loads(manifest.read_text())
    value["output"]["sha256"] = sha(data)
    manifest.write_text(json.dumps(value))
    candidate.write_bytes(data)
    path = manifest if endpoint == "manifest" else candidate
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
            chunk = repaired.read(size)
            if observed is None:
                mutate()
            return chunk

    def read(stream, target=None):
        nonlocal repaired
        repaired = stream
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

    repaired = None
    read_completed = [False]
    def read_and_mark(stream, target=None):
        result = read(stream, target)
        read_completed[0] = True
        return result

    monkeypatch.setattr(recovery, "_read_backup", read_and_mark)
    monkeypatch.setattr(recovery, "fingerprint", fingerprint)
    with pytest.raises((DoctorError, OSError)):
        recovery.verify_output(manifest, candidate)
    assert observed is not None
    assert observed == inventory(tmp_path)
    if path != source:
        assert snapshot(source) == current_before


@pytest.mark.parametrize("mode", ["v1", "v2copy"])
def test_alias_created_during_final_recheck_refused(tmp_path, monkeypatch, mode):
    import os
    from agentwrench.doctor import recovery
    from agentwrench.doctor.engine import DoctorError

    source, manifest, candidate, _ = prepare_mode(tmp_path, mode)
    source.unlink()
    real = recovery.fingerprint
    calls = 0
    observed = None

    def fingerprint(stream):
        nonlocal calls, observed
        digest = real(stream)
        calls += 1
        if calls == 3:  # final manifest hash, after candidate recheck
            os.link(candidate, source)
            observed = {p: snapshot(p) for p in tmp_path.iterdir()}
        return digest

    monkeypatch.setattr(recovery, "fingerprint", fingerprint)
    with pytest.raises(DoctorError, match="separate"):
        recovery.verify_output(manifest, candidate)
    assert observed == {p: snapshot(p) for p in tmp_path.iterdir()}


def refuse(manifest, candidate):
    result = cli("verify-output", manifest, "--candidate", candidate, "--json")
    assert result.returncode == 2, result.stdout + result.stderr
    value = json.loads(result.stdout)
    assert set(value) == {"file", "error"} and value["file"] == str(manifest)
    assert "Traceback" not in result.stderr
    return result


@pytest.mark.parametrize("mutation", [
    "v2_without_policy", "unknown_version", "bool_version", "extra", "missing_options", "in_place", "backup",
    "drop_invalid", "strategy", "format", "relative_source", "relative_output", "nul_path", "empty_path",
    "source_hash", "output_hash", "uppercase_hash", "same_paths", "report_file", "report_format",
    "report_changes", "report_issues", "missing_report", "duplicate_key", "invalid_json",
])
def test_strict_v1_manifest_contract(tmp_path, mutation):
    source, manifest, candidate, _ = prepare(tmp_path)
    value = json.loads(manifest.read_text())
    if mutation == "v2_without_policy":
        value["version"] = 2
    elif mutation in ("unknown_version", "bool_version"):
        value["version"] = 3 if mutation == "unknown_version" else True
    elif mutation == "extra":
        value["extra"] = None
    elif mutation == "missing_options":
        value.pop("options")
    elif mutation in ("in_place", "backup", "drop_invalid", "strategy", "format"):
        value["options"][mutation] = 1
    elif mutation in ("relative_source", "relative_output"):
        value[mutation.removeprefix("relative_")]["path"] = "relative"
    elif mutation in ("nul_path", "empty_path"):
        value["source"]["path"] = "/bad\0path" if mutation == "nul_path" else ""
    elif mutation in ("source_hash", "output_hash"):
        value[mutation.removesuffix("_hash")]["sha256"] = "g" * 64
    elif mutation == "uppercase_hash":
        value["output"]["sha256"] = "A" * 64
    elif mutation == "same_paths":
        value["output"]["path"] = str(source)
    elif mutation.startswith("report_"):
        key = mutation.removeprefix("report_")
        value["report"][key] = {} if key in ("changes", "issues") else "wrong"
    elif mutation == "missing_report":
        value["report"] = {}
    manifest.write_text(json.dumps(value))
    if mutation == "duplicate_key":
        manifest.write_text(manifest.read_text().replace('"version": 1', '"version": 1, "version": 1'))
    elif mutation == "invalid_json":
        manifest.write_bytes(b"not json")
    before = inventory(tmp_path)
    refuse(manifest, candidate)
    assert before == inventory(tmp_path)


@pytest.mark.parametrize("change", ["first", "last", "truncated", "appended", "empty"])
@pytest.mark.parametrize("mode", ["v1", "v2copy", "v2source"])
def test_full_candidate_hash_required(tmp_path, change, mode):
    _, manifest, candidate, _ = prepare_mode(tmp_path, mode)
    data = b"PRIVATE\x00\xff" + b"x" * (2 * 1024 * 1024) + b"tail"
    value = json.loads(manifest.read_text())
    value["output"]["sha256"] = sha(data)
    manifest.write_text(json.dumps(value))
    bad = {"first": b"z" + data[1:], "last": data[:-1] + b"z", "truncated": data[:-1],
           "appended": data + b"z", "empty": b""}[change]
    candidate.write_bytes(bad)
    before = inventory(tmp_path)
    assert "SHA-256" in json.loads(refuse(manifest, candidate).stdout)["error"]
    assert before == inventory(tmp_path)


@pytest.mark.parametrize("endpoint", ["manifest", "candidate"])
@pytest.mark.parametrize("kind", ["symlink", "dangling", "directory", "fifo", "missing", "device", "locked",
                                  "missing_parent", "parent_loop"])
@pytest.mark.parametrize("mode", ["v1", "v2copy", "v2source"])
def test_unsafe_or_locked_inputs(tmp_path, endpoint, kind, mode):
    from contextlib import ExitStack
    import fcntl
    import os

    _, manifest, candidate, _ = prepare_mode(tmp_path, mode)
    paths = {"manifest": manifest, "candidate": candidate}
    path = paths[endpoint]
    with ExitStack() as stack:
        if kind == "locked":
            stream = stack.enter_context(path.open("rb"))
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif kind in ("missing_parent", "parent_loop"):
            parent = tmp_path / "absent"
            if kind == "parent_loop":
                parent.symlink_to(parent.name, target_is_directory=True)
            paths[endpoint] = parent / path.name
        elif kind == "device":
            paths[endpoint] = Path("/dev/null")
        else:
            moved = tmp_path / "moved"
            path.rename(moved)
            if kind in ("symlink", "dangling"):
                path.symlink_to(moved if kind == "symlink" else tmp_path / "absent")
            elif kind == "directory":
                path.mkdir()
            elif kind == "fifo":
                os.mkfifo(path)
        before = inventory(tmp_path)
        refuse(paths["manifest"], paths["candidate"])
        assert before == inventory(tmp_path)


@pytest.mark.parametrize("fmt", ["codex", "claude"])
def test_copy_at_different_path_and_no_apply_or_cleanliness_claim(tmp_path, fmt):
    source, manifest, candidate, _ = prepare(tmp_path, fmt, apply=False)
    # A trusted hash can bind bytes that are not JSONL or still have pairing issues.
    data = source.read_bytes()
    value = json.loads(manifest.read_text())
    value["output"]["sha256"] = sha(data)
    value["output"]["path"] = str(tmp_path / "missing-parent" / "never-opened-output")
    manifest.write_text(json.dumps(value))
    candidate.write_bytes(data)
    copy = tmp_path / "explicit-copy"
    candidate.rename(copy)
    before = inventory(tmp_path)
    result = cli("verify-output", manifest, "--candidate", copy, "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["candidate"] == str(copy)
    assert before == inventory(tmp_path)
    assert cli("check", copy).returncode == 1


@pytest.mark.parametrize("mode", ["v1", "v2copy", "v2source"])
def test_relative_paths_normalized(tmp_path, monkeypatch, capsys, mode):
    from agentwrench.doctor.cli import main

    _, manifest, candidate, _ = prepare_mode(tmp_path, mode)
    before = inventory(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["verify-output", manifest.name, "--candidate", candidate.name, "--json"]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["manifest"] == str(manifest) and value["candidate"] == str(candidate)
    assert before == inventory(tmp_path)


@pytest.mark.parametrize("command,check_pairing", [("verify-output", False), ("verify-backup", False),
                                                       ("recover", False), ("verify-output", True)])
@pytest.mark.parametrize("alias", ["exact", "hardlink", "parent"])
def test_protected_source_alias_refused_before_candidate_open(tmp_path, monkeypatch, capsys, command, alias, check_pairing):
    import os
    from agentwrench.doctor.cli import main
    from .test_recovery import prepare as prepare_backup

    if command == "verify-output":
        source, manifest, candidate, _ = prepare(tmp_path)
    else:
        source, manifest, candidate, _ = prepare_backup(tmp_path, apply=False)
    if alias == "exact":
        candidate = source
    elif alias == "hardlink":
        candidate.unlink()
        os.link(source, candidate)
    else:
        parent = tmp_path / "parent-alias"
        parent.symlink_to(tmp_path, target_is_directory=True)
        candidate = parent / source.name
    before = inventory(tmp_path)
    real = os.open
    opened = []

    def open_manifest_only(path, flags, *args, **kwargs):
        assert path == manifest, "protected original source was opened through the explicit candidate"
        opened.append(path)
        return real(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_manifest_only)
    option = "--candidate" if command == "verify-output" else "--backup"
    args = [command, str(manifest), option, str(candidate), "--json"]
    if command == "verify-output" and check_pairing:
        args += ["--check-pairing"]
    if command == "recover":
        args += ["--out", str(tmp_path / "must-not-exist")]
    assert main(args) == 2
    assert set(json.loads(capsys.readouterr().out)) == {"file", "error"}
    assert opened == [manifest]
    assert before == inventory(tmp_path)


def prepare_v2(tmp_path, fmt="codex", *, current="copy"):
    source, manifest, candidate = (tmp_path / name for name in ("session", "plan", "candidate"))
    fixture = "codex/orphan.jsonl" if fmt == "codex" else "claude/orphan_tool_use.jsonl"
    source.write_bytes((Path(__file__).parent / "fixtures" / fixture).read_bytes())
    result = cli("preview", source, "--in-place", "--backup", "--manifest", manifest, "--json")
    assert result.returncode == 0, result.stderr
    # Independent repair; no manifest apply or original backup is needed.
    result = cli("repair", source, "--out", candidate, "--json")
    assert result.returncode == 0, result.stderr
    repaired = candidate.read_bytes()
    if current == "source":
        source.write_bytes(repaired)
        candidate.unlink()
        candidate = source
    elif current == "missing":
        source.unlink()
    elif current == "modified":
        source.write_bytes(b"PRIVATE CURRENT preserve exactly")
    return source, manifest, candidate, repaired


@pytest.mark.parametrize("fmt", ["codex", "claude"])
@pytest.mark.parametrize("current", ["source", "missing", "modified"])
def test_v2_explicit_source_or_copy_contract(tmp_path, fmt, current):
    source, manifest, candidate, repaired = prepare_v2(tmp_path, fmt, current=current)
    before = inventory(tmp_path)
    inputs = {p: snapshot(p) for p in tmp_path.iterdir()}
    result = cli("verify-output", manifest, "--candidate", candidate, "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "status": "verified", "version": 2, "manifest": str(manifest), "candidate": str(candidate),
        "expected_sha256": sha(repaired), "actual_sha256": sha(repaired), "verified_bytes": len(repaired)}
    assert inputs == {p: snapshot(p) for p in tmp_path.iterdir()}
    assert before == inventory(tmp_path)
    assert source.exists() == (current != "missing")
    assert "PRIVATE" not in result.stdout + result.stderr


def prepare_mode(tmp_path, mode, fmt="codex"):
    if mode == "v1":
        return prepare(tmp_path, fmt, apply=False)
    return prepare_v2(tmp_path, fmt, current="source" if mode == "v2source" else "copy")


@pytest.mark.parametrize("alias", ["parent", "hardlink", "symlink"])
@pytest.mark.parametrize("check_pairing", [False, True])
def test_v2_source_alias_policy_before_open(tmp_path, monkeypatch, capsys, alias, check_pairing):
    import os
    from agentwrench.doctor.cli import main

    source, manifest, _, repaired = prepare_v2(tmp_path, current="source")
    candidate = tmp_path / "alias"
    if alias == "parent":
        candidate.symlink_to(tmp_path, target_is_directory=True)
        candidate = candidate / source.name
    elif alias == "hardlink":
        os.link(source, candidate)
    else:
        candidate.symlink_to(source)
    before = inventory(tmp_path)
    real_open = os.open
    opened = []

    def guarded(path, flags, *args, **kwargs):
        assert path == manifest or alias == "parent" and path == source
        opened.append(path)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded)
    code = main(["verify-output", str(manifest), "--candidate", str(candidate), "--json",
                 *(["--check-pairing"] if check_pairing else [])])
    assert code == (0 if alias == "parent" else 2)
    output = json.loads(capsys.readouterr().out)
    if alias == "parent":
        assert output["candidate"] == str(source) and output["actual_sha256"] == sha(repaired)
    else:
        assert set(output) == {"file", "error"}
    assert opened == ([manifest, source] if alias == "parent" else [manifest])
    assert before == inventory(tmp_path)


@pytest.mark.parametrize("pair", ["manifest_candidate", "current_manifest"])
@pytest.mark.parametrize("alias", ["exact", "parent", "hardlink"])
def test_v2_manifest_separation(tmp_path, monkeypatch, pair, alias):
    import os
    from agentwrench.doctor import recovery
    from agentwrench.doctor.engine import DoctorError

    source, manifest, candidate, _ = prepare_v2(tmp_path)
    value = json.loads(manifest.read_text())
    if pair == "manifest_candidate":
        target = manifest
        if alias == "exact":
            candidate = target
        elif alias == "parent":
            parent = tmp_path / "parent"
            parent.symlink_to(tmp_path, target_is_directory=True)
            candidate = parent / target.name
        else:
            candidate.unlink()
            os.link(target, candidate)
    else:
        if alias == "exact":
            bound = manifest
        elif alias == "parent":
            parent = tmp_path / "parent"
            parent.symlink_to(tmp_path, target_is_directory=True)
            bound = parent / manifest.name
        else:
            source.unlink()
            os.link(manifest, source)
            bound = source
        value["source"]["path"] = value["output"]["path"] = value["report"]["file"] = str(bound)
        manifest.write_text(json.dumps(value))
    before = inventory(tmp_path)
    real_open = os.open

    def guarded(path, flags, *args, **kwargs):
        assert path == manifest, "candidate opened before manifest/source separation"
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded)
    with pytest.raises(DoctorError, match="separate"):
        recovery.verify_output(manifest, candidate)
    assert before == inventory(tmp_path)


@pytest.mark.parametrize("mutation", ["missing_in_place", "missing_backup", "false_in_place", "false_backup",
    "int_in_place", "int_backup", "different_output", "bool_version", "unknown_version", "duplicate_key",
    "invalid_json", "extra", "source_hash", "output_hash", "report"])
def test_strict_v2_output_manifest_contract(tmp_path, mutation):
    _, manifest, candidate, _ = prepare_v2(tmp_path)
    value = json.loads(manifest.read_text())
    if mutation.startswith("missing_"):
        value["options"].pop(mutation.removeprefix("missing_"))
    elif mutation.startswith(("false_", "int_")):
        prefix, key = mutation.split("_", 1)
        value["options"][key] = False if prefix == "false" else 1
    elif mutation == "different_output":
        value["output"]["path"] = str(candidate)
    elif mutation in ("bool_version", "unknown_version"):
        value["version"] = True if mutation == "bool_version" else 3
    elif mutation in ("source_hash", "output_hash"):
        value[mutation.removesuffix("_hash")]["sha256"] = "z" * 64
    elif mutation == "extra":
        value["extra"] = None
    elif mutation == "report":
        value["report"] = {}
    manifest.write_text(json.dumps(value))
    if mutation == "duplicate_key":
        manifest.write_text(manifest.read_text().replace('"version": 2', '"version": 2, "version": 2'))
    elif mutation == "invalid_json":
        manifest.write_bytes(b"not json")
    before = inventory(tmp_path)
    refuse(manifest, candidate)
    assert before == inventory(tmp_path)


def test_help_names_both_manifest_versions(capsys):
    from agentwrench.doctor.cli import main

    with pytest.raises(SystemExit) as result:
        main(["verify-output", "--help"])
    assert result.value.code == 0
    assert "v1 new-file or v2 in-place" in capsys.readouterr().out


@pytest.mark.parametrize("fmt", ["codex", "claude"])
@pytest.mark.parametrize("mode", ["v1", "v2copy", "v2source"])
def test_check_pairing_cli_success_contract(tmp_path, fmt, mode):
    source, manifest, candidate, repaired = prepare_mode(tmp_path, mode, fmt)
    if mode != "v2source":
        source.unlink()  # No original or backup is required.
    before = inventory(tmp_path)
    result = cli("verify-output", manifest, "--candidate", candidate, "--check-pairing", "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "status": "verified", "version": 1 if mode == "v1" else 2,
        "manifest": str(manifest), "candidate": str(candidate),
        "expected_sha256": sha(repaired), "actual_sha256": sha(repaired), "verified_bytes": len(repaired),
        "pairing": {"format": fmt, "clean": True}}
    assert not result.stderr
    assert before == inventory(tmp_path)
    result = cli("verify-output", manifest, "--candidate", candidate, "--check-pairing")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == (f"verified: {candidate} ({len(repaired)} bytes) SHA-256 {sha(repaired)}; "
                             f"pairing clean [{fmt}]\n")
    assert not result.stderr
    assert before == inventory(tmp_path)


@pytest.mark.parametrize("fmt", ["codex", "claude"])
def test_check_pairing_dirty_refusal_contract(tmp_path, fmt):
    source, manifest, candidate, _ = prepare(tmp_path, fmt, apply=False)
    data = source.read_bytes()
    value = json.loads(manifest.read_text())
    value["output"]["sha256"] = sha(data)
    manifest.write_text(json.dumps(value))
    candidate.write_bytes(data)
    before = inventory(tmp_path)
    result = cli("verify-output", manifest, "--candidate", candidate, "--check-pairing", "--json")
    assert result.returncode == 2, result.stdout + result.stderr
    error = "candidate pairing check failed; no output published"
    assert json.loads(result.stdout) == {"file": str(manifest), "error": error}
    assert result.stderr == f"error: {error}\n"
    assert before == inventory(tmp_path)


@pytest.mark.parametrize("fmt", ["codex", "claude"])
@pytest.mark.parametrize("mode", ["v1", "v2copy", "v2source"])
@pytest.mark.parametrize("kind", ["dirty", "invalid_utf8", "invalid_json", "duplicate_key", "nonobject",
                                  "mixed", "mismatched", "truncated", "structural"])
def test_check_pairing_matching_bad_bytes_refuse_only_with_opt_in(tmp_path, fmt, mode, kind):
    _, manifest, candidate, _ = prepare_mode(tmp_path, mode, fmt)
    other = b'{"type":"session_meta"}\n' if fmt == "claude" else b'{"uuid":"PRIVATE-OTHER"}\n'
    marker = b'{"type":"session_meta"}\n' if fmt == "codex" else b'{"uuid":"PRIVATE-MARKER"}\n'
    fixture = "codex/RD101.jsonl" if fmt == "codex" else "claude/RD201.jsonl"
    data = {
        "dirty": (Path(__file__).parent / "fixtures" / fixture).read_bytes(),
        "invalid_utf8": b'\xffPRIVATE\n', "invalid_json": b'PRIVATE\n',
        "duplicate_key": b'{"PRIVATE":1,"PRIVATE":2}\n', "nonobject": b'["PRIVATE"]\n',
        "mixed": marker + other, "mismatched": other,
        "truncated": b'{"PRIVATE":"unterminated',
        "structural": (b'{"type":"response_item","payload":"PRIVATE"}\n' if fmt == "codex" else
                       b'{"type":"assistant","message":{"content":"PRIVATE"}}\n'),
    }[kind]
    value = json.loads(manifest.read_text())
    value["output"]["sha256"] = sha(data)
    # These untrusted historical entries must not be replayed or serialized.
    value["report"]["issues"] = ["PRIVATE historical issue"]
    value["report"]["changes"] = ["PRIVATE historical change"]
    manifest.write_text(json.dumps(value))
    candidate.write_bytes(data)
    before = inventory(tmp_path)
    default = cli("verify-output", manifest, "--candidate", candidate, "--json")
    assert default.returncode == 0, default.stderr
    assert "pairing" not in json.loads(default.stdout)
    error = "candidate pairing check failed; no output published"
    for json_mode in (True, False):
        result = cli("verify-output", manifest, "--candidate", candidate, "--check-pairing",
                     *(["--json"] if json_mode else []))
        assert result.returncode == 2, result.stdout + result.stderr
        assert result.stderr == f"error: {error}\n"
        assert result.stdout == (json.dumps({"file": str(manifest), "error": error}) + "\n" if json_mode else "")
        assert "PRIVATE" not in result.stdout + result.stderr
        assert before == inventory(tmp_path)


@pytest.mark.parametrize("fmt", ["codex", "claude"])
@pytest.mark.parametrize("mode", ["v1", "v2copy", "v2source"])
@pytest.mark.parametrize("case", ["saved_empty", "explicit_empty", "drop_empty", "drop_unmarked",
                                  "explicit_drop_unmarked"])
def test_check_pairing_saved_format_empty_and_lost_markers(tmp_path, fmt, mode, case):
    source, manifest, candidate = (tmp_path / name for name in ("session", "plan", "candidate"))
    # Real preview/repair, with no marker surviving invalid-line removal.
    marker = (b'{"type":"response_item","payload":null}\r\n' if fmt == "codex" else
              b'{"type":"assistant","uuid":"a","message":{}}\r\n')
    metadata = b'{"private_metadata":"PRIVATE payload"}'
    source.write_bytes((marker + (metadata if "unmarked" in case else b"")) if "drop" in case else b"")
    options = ["--strategy", "drop", "--drop-invalid"]
    if case.startswith("explicit"):
        options += ["--format", fmt]
    destinations = ["--out", candidate] if mode == "v1" else ["--in-place", "--backup"]
    result = cli("preview", source, *destinations, "--manifest", manifest, *options, "--json")
    assert result.returncode == 0, result.stderr
    assert cli("repair", source, "--out", candidate, *options).returncode == 0
    data = candidate.read_bytes()
    assert data == (metadata if "unmarked" in case else b"")
    if mode == "v2source":
        source.write_bytes(data)
        candidate.unlink()
        candidate = source
    else:
        source.unlink()
    before = inventory(tmp_path)
    result = cli("verify-output", manifest, "--candidate", candidate, "--check-pairing", "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["pairing"] == {"format": "empty" if case == "saved_empty" else fmt, "clean": True}
    assert payload["expected_sha256"] == payload["actual_sha256"] == sha(data)
    assert payload["verified_bytes"] == len(data)
    assert "PRIVATE" not in result.stdout + result.stderr
    assert before == inventory(tmp_path)


def test_check_pairing_saved_empty_refuses_nonempty_unsupported_candidate(tmp_path):
    from agentwrench.doctor import recovery
    from agentwrench.doctor.engine import DoctorError

    _, manifest, candidate, _ = prepare(tmp_path)
    value = json.loads(manifest.read_text())
    value["report"]["format"] = "empty"
    for data in (b'{"PRIVATE":"metadata"}\n', b'{"type":"session_meta"}\n'):
        value["output"]["sha256"] = sha(data)
        manifest.write_text(json.dumps(value))
        candidate.write_bytes(data)
        before = inventory(tmp_path)
        assert recovery.verify_output(manifest, candidate)["status"] == "verified"
        with pytest.raises(DoctorError, match="candidate pairing check failed"):
            recovery.verify_output(manifest, candidate, check_pairing=True)
        assert before == inventory(tmp_path)


@pytest.mark.parametrize("mode", ["v1", "v2copy", "v2source"])
def test_check_pairing_only_matching_candidate_scanned_under_both_locks(tmp_path, monkeypatch, mode):
    import fcntl
    from agentwrench.doctor import recovery
    from agentwrench.doctor.engine import DoctorError

    _, manifest, candidate, data = prepare_mode(tmp_path, mode)
    real_scan = recovery.scan
    scans = []

    def locked_scan(stream, file, format):
        assert Path(file) == candidate
        # A separate descriptor cannot obtain an exclusive lock on either input.
        for path in (manifest, candidate):
            with path.open("rb") as independent:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(independent, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = real_scan(stream, file, format)
        assert report.digest == sha(data)
        scans.append(file)
        return report

    monkeypatch.setattr(recovery, "scan", locked_scan)
    before = inventory(tmp_path)
    recovery.verify_output(manifest, candidate)  # Default must not parse.
    assert not scans
    recovery.verify_output(manifest, candidate, check_pairing=True)
    assert scans == [str(candidate)]
    assert before == inventory(tmp_path)
    candidate.write_bytes(b"PRIVATE wrong bytes")
    before = inventory(tmp_path)
    with pytest.raises(DoctorError, match="SHA-256"):
        recovery.verify_output(manifest, candidate, check_pairing=True)
    assert scans == [str(candidate)]  # Mismatched bytes must not parse either.
    assert before == inventory(tmp_path)


@pytest.mark.parametrize("mode", ["v1", "v2copy", "v2source"])
@pytest.mark.parametrize("endpoint", ["manifest", "candidate"])
@pytest.mark.parametrize("stage", ["during_scan", "after_scan"])
@pytest.mark.parametrize("change", ["rewrite", "replace", "symlink", "unlink", "metadata", "restore_mtime"])
def test_check_pairing_input_changes_during_parse_refused(tmp_path, monkeypatch, mode, endpoint, stage, change):
    import os
    from agentwrench.doctor import recovery
    from agentwrench.doctor.engine import DoctorError

    _, manifest, candidate, _ = prepare_mode(tmp_path, mode)
    path = manifest if endpoint == "manifest" else candidate
    old = snapshot(path)
    observed = None

    def mutate():
        nonlocal observed
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

    real_scan = recovery.scan

    def changed_lines(stream):
        for raw in stream:
            if observed is None:
                mutate()
            yield raw

    def scan(stream, file, format):
        result = real_scan(changed_lines(stream) if stage == "during_scan" else stream, file, format)
        if stage == "after_scan":
            mutate()
        return result

    monkeypatch.setattr(recovery, "scan", scan)
    with pytest.raises((DoctorError, OSError)):
        recovery.verify_output(manifest, candidate, check_pairing=True)
    assert observed is not None
    assert observed == inventory(tmp_path)  # Preserve the adversary's state, never roll back.


@pytest.mark.parametrize("mode", ["v1", "v2copy"])
def test_check_pairing_source_alias_created_after_parse_refused(tmp_path, monkeypatch, mode):
    import os
    from agentwrench.doctor import recovery
    from agentwrench.doctor.engine import DoctorError

    source, manifest, candidate, _ = prepare_mode(tmp_path, mode)
    source.unlink()
    real = recovery.scan
    observed = None

    def scan(*args):
        nonlocal observed
        report = real(*args)
        os.link(candidate, source)
        observed = inventory(tmp_path)
        return report

    monkeypatch.setattr(recovery, "scan", scan)
    # Adding a hardlink also changes ctime: either the stat guard or the alias
    # guard must refuse it before success, without changing the adversary's state.
    with pytest.raises(DoctorError, match="changed|separate"):
        recovery.verify_output(manifest, candidate, check_pairing=True)
    assert observed == inventory(tmp_path)
