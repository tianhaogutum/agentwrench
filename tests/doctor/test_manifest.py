"""Local synthetic integration cases for persisted repair approval boundaries."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from agentwrench.doctor import manifest as manifests
from agentwrench.doctor.cli import main
from agentwrench.doctor.engine import DoctorError, STUB_TEXT, scan
from agentwrench.doctor.files import open_session, publish
from agentwrench.doctor.repair import make_plan
from .make_fixtures import item
from .test_cli import cli
from .test_engine import raw


def sha(data):
    return hashlib.sha256(data).hexdigest()


def test_preview_apply_and_already_applied_without_rewrite(tmp_path):
    source = tmp_path / "session.jsonl"
    output = tmp_path / "fixed.jsonl"
    manifest = tmp_path / "plan.json"
    original = raw(item("function_call", "c1", name="shell", arguments="SYNTHETIC_SECRET"))
    source.write_bytes(original)
    expected = original + (
        '{"type":"response_item","payload":{"type":"function_call_output",'
        '"call_id":"c1","output":' + json.dumps(STUB_TEXT) + '},"timestamp":"2026-01-01T00:00:00Z"}\n'
    ).encode()

    preview = cli("preview", source, "--out", output, "--manifest", manifest, "--json")
    assert preview.returncode == 0, preview.stderr
    saved = manifest.read_bytes()
    plan = json.loads(saved)
    assert plan["source"] == {"path": str(source), "sha256": sha(original)}
    assert plan["output"] == {"path": str(output), "sha256": sha(expected)}
    assert plan["report"]["changes"][0]["action"] == "insert_stub"
    assert "SYNTHETIC_SECRET" not in saved.decode() + preview.stdout + preview.stderr
    assert source.read_bytes() == original and not output.exists()
    assert manifest.stat().st_mode & 0o777 == 0o600

    applied = cli("apply", manifest, "--json")
    assert applied.returncode == 0, applied.stderr
    assert json.loads(applied.stdout)["status"] == "applied"
    assert output.read_bytes() == expected
    assert output.stat().st_mode & 0o777 == 0o600
    assert cli("check", output).returncode == 0
    before = {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in tmp_path.iterdir()}
    again = cli("apply", manifest, "--json")
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["status"] == "already_applied"
    assert before == {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in tmp_path.iterdir()}
    assert manifest.read_bytes() == saved and source.read_bytes() == original


def test_apply_refuses_stale_input_even_when_output_already_applied(tmp_path):
    source, output, manifest = (tmp_path / name for name in ("in.jsonl", "out.jsonl", "plan.json"))
    source.write_bytes(raw(item("function_call", "c1", name="shell", arguments="{}")))
    assert cli("preview", source, "--out", output, "--manifest", manifest).returncode == 0
    original = source.read_bytes()
    source.write_bytes(original + raw(item("message", role="user", content=[])))
    stale = cli("apply", manifest, "--json")
    assert stale.returncode == 2 and "stale input" in json.loads(stale.stdout)["error"]
    assert not output.exists()
    source.write_bytes(original)
    assert cli("apply", manifest).returncode == 0
    fixed = output.read_bytes()
    source.write_bytes(b"")
    stale = cli("apply", manifest, "--json")
    assert stale.returncode == 2 and "stale input" in json.loads(stale.stdout)["error"]
    assert output.read_bytes() == fixed


def prepare(tmp_path, fixture="codex/orphan.jsonl", *options):
    source, output, manifest = (tmp_path / name for name in ("input.jsonl", "output.jsonl", "plan.json"))
    source.write_bytes((Path(__file__).parent / "fixtures" / fixture).read_bytes())
    run = cli("preview", source, "--out", output, "--manifest", manifest, *options, "--json")
    assert run.returncode == 0, run.stderr
    return source, output, manifest


@pytest.mark.parametrize("fixture,options", [
    ("claude/orphan_tool_use.jsonl", ()),
    ("claude/sidechains.jsonl", ("--strategy", "drop")),
    ("codex/orphan.jsonl", ("--strategy", "drop")),
    ("codex/RD001.jsonl", ("--drop-invalid",)),
    ("codex/clean.jsonl", ()),
])
def test_saved_policy_matches_direct_repair(tmp_path, fixture, options):
    source, output, manifest = prepare(tmp_path, fixture, *options)
    direct = tmp_path / "direct.jsonl"
    run = cli("repair", source, "--out", direct, *options)
    assert run.returncode == 0, run.stderr
    applied = cli("apply", manifest, "--json")
    assert applied.returncode == 0, applied.stderr
    assert output.read_bytes() == direct.read_bytes()
    assert cli("apply", manifest, "--json").returncode == 0


def test_explicit_format_crlf_and_unterminated_input_survive(tmp_path):
    source, output, manifest = (tmp_path / name for name in ("input", "output", "plan"))
    data = b'{"custom":"metadata"}\r\n{"custom":"tail"}'
    source.write_bytes(data)
    assert cli("preview", source, "--out", output, "--manifest", manifest, "--json").returncode == 2
    assert not manifest.exists()
    run = cli("preview", source, "--out", output, "--manifest", manifest, "--format", "codex")
    assert run.returncode == 0, run.stderr
    assert cli("apply", manifest).returncode == 0
    assert output.read_bytes() == data


@pytest.mark.parametrize("kind", ["regular", "matching_symlink", "dangling_symlink", "directory", "fifo"])
def test_existing_output_is_never_overwritten_or_followed(tmp_path, kind):
    source, output, manifest = prepare(tmp_path)
    saved = manifest.read_bytes()
    if kind == "regular":
        output.write_bytes(b"keep me")
    elif kind == "matching_symlink":
        target = tmp_path / "target"
        assert cli("repair", source, "--out", target).returncode == 0
        output.symlink_to(target)
    elif kind == "dangling_symlink":
        output.symlink_to(tmp_path / "missing")
    elif kind == "directory":
        output.mkdir()
    else:
        os.mkfifo(output)
    before = output.lstat()
    run = cli("apply", manifest, "--json")
    assert run.returncode == 2 and "error" in json.loads(run.stdout)
    after = output.lstat()
    # Verification may update atime; it must not replace or modify the target.
    assert (after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns, after.st_ctime_ns) == (
        before.st_ino, before.st_mode, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    if kind == "regular":
        assert output.read_bytes() == b"keep me"
    assert manifest.read_bytes() == saved


def test_repaired_output_later_modified_is_conflict(tmp_path):
    _, output, manifest = prepare(tmp_path)
    assert cli("apply", manifest).returncode == 0
    output.write_bytes(output.read_bytes() + b"\n")
    modified = output.read_bytes()
    run = cli("apply", manifest, "--json")
    assert run.returncode == 2 and "conflicts" in json.loads(run.stdout)["error"]
    assert output.read_bytes() == modified


@pytest.mark.parametrize("target", ["output", "manifest", "same_path"])
def test_preview_refuses_path_collisions(tmp_path, target):
    source, output, manifest = (tmp_path / name for name in ("input", "output", "plan"))
    source.write_bytes(raw(item("function_call", "c1")))
    if target == "same_path":
        manifest = output
    else:
        (output if target == "output" else manifest).write_bytes(b"keep me")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert cli("preview", source, "--out", output, "--manifest", manifest, "--json").returncode == 2
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


@pytest.mark.parametrize("mutation", [
    "version", "bool_version", "extra", "relative_source", "relative_output", "bad_hash",
    "same_path", "bad_options", "bad_bool", "report", "bool_line", "float_line",
    "expected_hash", "strategy", "duplicate_key",
])
def test_invalid_or_drifted_manifest_refused_without_writes(tmp_path, mutation):
    source, output, manifest = prepare(tmp_path)
    value = json.loads(manifest.read_text())
    if mutation == "version":
        value["version"] = 2
    elif mutation == "bool_version":
        value["version"] = True
    elif mutation == "extra":
        value["unrecognized"] = "no"
    elif mutation.startswith("relative_"):
        value[mutation.removeprefix("relative_")]["path"] = "relative.jsonl"
    elif mutation == "bad_hash":
        value["source"]["sha256"] = "bad"
    elif mutation == "same_path":
        value["output"]["path"] = value["source"]["path"]
    elif mutation == "bad_options":
        value["options"]["strategy"] = "unknown"
    elif mutation == "bad_bool":
        value["options"]["drop_invalid"] = 1
    elif mutation == "report":
        value["report"]["changes"] = []
    elif mutation in ("bool_line", "float_line"):
        value["report"]["issues"][0]["line"] = True if mutation == "bool_line" else 1.0
    elif mutation == "expected_hash":
        value["output"]["sha256"] = "0" * 64
    elif mutation == "strategy":
        value["options"]["strategy"] = "drop"
    manifest.write_text(json.dumps(value))
    if mutation == "duplicate_key":
        manifest.write_text(manifest.read_text()[:-1] + ',"version":1}')
    saved, original = manifest.read_bytes(), source.read_bytes()
    run = cli("apply", manifest, "--json")
    assert run.returncode == 2 and "error" in json.loads(run.stdout)
    assert not output.exists() and manifest.read_bytes() == saved and source.read_bytes() == original
    assert not list(tmp_path.glob(".rollout-doctor-*"))


def test_relative_paths_bound_at_preview_and_apply_from_another_directory(tmp_path, monkeypatch):
    original = raw(item("function_call", "c1"))
    (tmp_path / "input").write_bytes(original)
    monkeypatch.chdir(tmp_path)
    assert main(["preview", "input", "--out", "output", "--manifest", "plan", "--json"]) == 0
    plan = json.loads((tmp_path / "plan").read_text())
    assert plan["source"]["path"] == str(tmp_path / "input")
    assert plan["output"]["path"] == str(tmp_path / "output")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert main(["apply", str(tmp_path / "plan"), "--json"]) == 0
    assert (tmp_path / "output").exists() and not (elsewhere / "output").exists()


def test_preview_invalid_input_does_not_save_manifest(tmp_path):
    source, output, manifest = (tmp_path / name for name in ("input", "output", "plan"))
    source.write_bytes(b'{"type":"response_item","payload":{}}\n')
    run = cli("preview", source, "--out", output, "--manifest", manifest, "--json")
    assert run.returncode == 2
    assert not output.exists() and not manifest.exists()
    assert list(tmp_path.iterdir()) == [source]


def test_preview_manifest_publication_race_is_no_clobber(tmp_path, monkeypatch):
    source, output, manifest = (tmp_path / name for name in ("input", "output", "plan"))
    source.write_bytes(raw(item("function_call", "c1")))
    real_link = os.link

    def race(src, dst, **kwargs):
        Path(dst).write_bytes(b"other writer")
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(os, "link", race)
    with pytest.raises(FileExistsError):
        manifests.preview(source, output, manifest)
    assert manifest.read_bytes() == b"other writer" and not output.exists()
    assert not list(tmp_path.glob(".rollout-doctor-*"))


def test_apply_source_change_before_publish_refused(tmp_path, monkeypatch):
    source, output, manifest = prepare(tmp_path)
    real_publish = manifests.publish

    def changed(*args, **kwargs):
        source.write_bytes(raw(item("message", role="user", content=[])))
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(manifests, "publish", changed)
    with pytest.raises(DoctorError, match="source changed|differs from preview"):
        manifests.apply(manifest)
    assert not output.exists() and not list(tmp_path.glob(".rollout-doctor-*"))


def test_apply_destination_publication_race_is_no_clobber(tmp_path, monkeypatch):
    _, output, manifest = prepare(tmp_path)
    real_link = os.link

    def race(src, dst, **kwargs):
        output.write_bytes(b"other writer")
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(os, "link", race)
    with pytest.raises(FileExistsError):
        manifests.apply(manifest)
    assert output.read_bytes() == b"other writer"
    assert not list(tmp_path.glob(".rollout-doctor-*"))


def test_publish_checks_rendered_digest_before_publication(tmp_path):
    source, output, _ = prepare(tmp_path)
    with open_session(source) as handle:
        plan = make_plan(scan(handle, str(source)))
        with pytest.raises(DoctorError, match="rendered output differs"):
            publish(handle, source, output, plan, False, expected_digest="0" * 64)
    assert not output.exists() and not list(tmp_path.glob(".rollout-doctor-*"))


def test_preview_source_change_does_not_publish_manifest(tmp_path, monkeypatch):
    source, output, manifest = (tmp_path / name for name in ("input", "output", "plan"))
    source.write_bytes(raw(item("function_call", "c1")))
    real_digest = manifests._render_digest

    def changed(*args):
        digest = real_digest(*args)
        source.write_bytes(b"")
        return digest

    monkeypatch.setattr(manifests, "_render_digest", changed)
    with pytest.raises(DoctorError, match="source changed"):
        manifests.preview(source, output, manifest)
    assert not output.exists() and not manifest.exists()


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_preview_requires_existing_output_directory(tmp_path, kind):
    source, manifest, parent = (tmp_path / name for name in ("input", "plan", "parent"))
    source.write_bytes(raw(item("function_call", "c1")))
    if kind == "file":
        parent.write_bytes(b"file")
    assert cli("preview", source, "--out", parent / "output", "--manifest", manifest).returncode == 2
    assert not manifest.exists()


@pytest.mark.parametrize("endpoint", ["source", "manifest"])
def test_apply_rejects_symlink_endpoint(tmp_path, endpoint):
    source, output, manifest = prepare(tmp_path)
    path = source if endpoint == "source" else manifest
    moved = path.with_suffix(".moved")
    path.rename(moved)
    path.symlink_to(moved)
    assert cli("apply", manifest, "--json").returncode == 2
    assert not output.exists()


@pytest.mark.parametrize("fixture,options", [
    ("codex/orphan.jsonl", ()),
    ("claude/orphan_tool_use.jsonl", ()),
    ("claude/sidechains.jsonl", ("--strategy", "drop")),
    ("codex/RD001.jsonl", ("--drop-invalid",)),
])
def test_in_place_manifest_binds_backup_and_reapply_never_writes(tmp_path, fixture, options):
    source, manifest, direct = (tmp_path / name for name in ("session.jsonl", "plan.json", "direct.jsonl"))
    original = (Path(__file__).parent / "fixtures" / fixture).read_bytes()
    source.write_bytes(original)
    source.chmod(0o640)
    assert cli("repair", source, "--out", direct, *options).returncode == 0
    expected = direct.read_bytes()
    before = source.stat()

    preview = cli("preview", source, "--in-place", "--backup", "--manifest", manifest, *options, "--json")
    assert preview.returncode == 0, preview.stderr
    saved = manifest.read_bytes()
    value = json.loads(saved)
    assert value["version"] == 2
    assert value["options"]["in_place"] is True and value["options"]["backup"] is True
    assert value["source"] == {"path": str(source), "sha256": sha(original)}
    assert value["output"] == {"path": str(source), "sha256": sha(expected)}
    assert source.read_bytes() == original and source.stat().st_ino == before.st_ino
    assert source.stat().st_mtime_ns == before.st_mtime_ns
    assert not list(tmp_path.glob("*.bak-*"))
    assert manifest.stat().st_mode & 0o777 == 0o600

    applied = cli("apply", manifest, "--json")
    assert applied.returncode == 0, applied.stderr
    payload = json.loads(applied.stdout)
    assert payload["status"] == "applied"
    backups = list(tmp_path.glob("session.jsonl.bak-*Z"))
    assert len(backups) == 1 and payload["backup"] == str(backups[0])
    assert backups[0].read_bytes() == original and backups[0].stat().st_mode & 0o777 == 0o600
    assert source.read_bytes() == expected and source.stat().st_mode & 0o777 == 0o640
    assert cli("check", source).returncode == 0
    before = {p.name: (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes()) for p in tmp_path.iterdir()}
    again = cli("apply", manifest, "--json")
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["status"] == "already_applied"
    assert json.loads(again.stdout)["backup"] is None
    assert before == {p.name: (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes()) for p in tmp_path.iterdir()}
    assert manifest.read_bytes() == saved


@pytest.mark.parametrize("options", [(), ("--in-place",), ("--backup",),
                                     ("--out", "out", "--backup"), ("--out", "out", "--in-place", "--backup")])
def test_preview_requires_explicit_destination_and_matching_backup_consent(tmp_path, options):
    source, manifest = tmp_path / "source", tmp_path / "plan"
    source.write_bytes(raw(item("function_call", "c1")))
    before = source.read_bytes()
    assert cli("preview", source, "--manifest", manifest, *options).returncode == 2
    assert list(tmp_path.iterdir()) == [source] and source.read_bytes() == before


def test_in_place_noop_manifest_does_not_rewrite_or_backup(tmp_path):
    source, manifest = tmp_path / "source", tmp_path / "plan"
    source.write_bytes(raw(item("function_call", "c1")) + raw(item("function_call_output", "c1", output="ok")))
    assert cli("preview", source, "--in-place", "--backup", "--manifest", manifest).returncode == 0
    before = {p.name: (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes()) for p in tmp_path.iterdir()}
    for _ in range(2):
        run = cli("apply", manifest, "--json")
        assert run.returncode == 0, run.stderr
        assert json.loads(run.stdout)["status"] == "already_applied"
        assert json.loads(run.stdout)["backup"] is None
    assert before == {p.name: (p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes()) for p in tmp_path.iterdir()}


def prepare_in_place(tmp_path):
    source, manifest = tmp_path / "source", tmp_path / "plan"
    source.write_bytes(raw(item("function_call", "c1", arguments="PRIVATE-ARG")))
    run = cli("preview", source, "--in-place", "--backup", "--manifest", manifest, "--json")
    assert run.returncode == 0, run.stderr
    assert "PRIVATE-ARG" not in manifest.read_text() + run.stdout + run.stderr
    return source, manifest


@pytest.mark.parametrize("after_apply", [False, True])
def test_in_place_stale_input_refused_before_and_after_apply(tmp_path, after_apply):
    source, manifest = prepare_in_place(tmp_path)
    if after_apply:
        assert cli("apply", manifest).returncode == 0
    source.write_bytes(source.read_bytes() + b"\n")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    run = cli("apply", manifest, "--json")
    assert run.returncode == 2 and "stale input" in json.loads(run.stdout)["error"]
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


@pytest.mark.parametrize("mutation", ["no_backup", "no_in_place", "int_backup", "missing_backup", "extra",
                                     "other_output", "downgrade", "report", "output_hash", "strategy"])
def test_in_place_invalid_policy_or_plan_refused_without_backup(tmp_path, mutation):
    source, manifest = prepare_in_place(tmp_path)
    value = json.loads(manifest.read_text())
    if mutation in ("no_backup", "no_in_place"):
        value["options"][mutation.removeprefix("no_")] = False
    elif mutation == "int_backup":
        value["options"]["backup"] = 1
    elif mutation == "missing_backup":
        del value["options"]["backup"]
    elif mutation == "extra":
        value["options"]["backup_path"] = str(tmp_path / "unapproved")
    elif mutation == "other_output":
        value["output"]["path"] = str(tmp_path / "other")
    elif mutation == "downgrade":
        value["version"] = 1
    elif mutation == "report":
        value["report"]["changes"] = []
    elif mutation == "output_hash":
        value["output"]["sha256"] = "0" * 64
    else:
        value["options"]["strategy"] = "drop"
    manifest.write_text(json.dumps(value))
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    run = cli("apply", manifest, "--json")
    assert run.returncode == 2 and "error" in json.loads(run.stdout)
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


def test_in_place_backup_creation_failure_never_replaces_source(tmp_path, monkeypatch):
    source, manifest = prepare_in_place(tmp_path)
    original = source.read_bytes()
    real_open = os.open

    def fail_backup(path, *args, **kwargs):
        if ".bak-" in str(path):
            raise PermissionError("simulated backup creation failure")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", fail_backup)
    with pytest.raises(PermissionError, match="backup creation failure"):
        manifests.apply(manifest)
    assert source.read_bytes() == original and not list(tmp_path.glob("*.bak-*"))
    assert not list(tmp_path.glob(".rollout-doctor-*"))


@pytest.mark.parametrize("fmt", ["codex", "claude"])
def test_in_place_reapply_after_drop_removes_all_format_markers(tmp_path, fmt):
    source, manifest = tmp_path / "source", tmp_path / "plan"
    metadata = b'{"custom":"metadata"}\r\n'
    call = (item("function_call", "c1") if fmt == "codex" else
            {"type": "assistant", "uuid": "a", "parentUuid": None,
             "message": {"content": [{"type": "tool_use", "id": "c1"}]}})
    # Codex drops the call line; Claude keeps a text message/UUID. Both must
    # retain the preview's detected format when validating the repaired bytes.
    source.write_bytes(metadata + raw(call))
    assert cli("preview", source, "--in-place", "--backup", "--manifest", manifest,
               "--strategy", "drop").returncode == 0
    assert cli("apply", manifest).returncode == 0
    if fmt == "codex":
        assert source.read_bytes() == metadata
    backups = list(tmp_path.glob("*.bak-*"))
    # Verification does not require a surviving backup and must not recreate it.
    for backup in backups:
        backup.unlink()
    before = source.stat()
    again = cli("apply", manifest, "--json")
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["status"] == "already_applied"
    assert source.stat().st_ino == before.st_ino and source.stat().st_mtime_ns == before.st_mtime_ns
    assert not list(tmp_path.glob("*.bak-*"))


@pytest.mark.parametrize("after_apply", [False, True])
@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_in_place_unsafe_source_never_followed(tmp_path, after_apply, kind):
    source, manifest = prepare_in_place(tmp_path)
    if after_apply:
        assert cli("apply", manifest).returncode == 0
    moved = tmp_path / "moved"
    source.rename(moved)
    if kind == "symlink":
        source.symlink_to(moved)
    else:
        os.mkfifo(source)
    before = moved.read_bytes(), list(tmp_path.glob("*.bak-*"))
    assert cli("apply", manifest, "--json").returncode == 2
    assert before == (moved.read_bytes(), list(tmp_path.glob("*.bak-*")))


@pytest.mark.parametrize("after_apply", [False, True])
def test_in_place_source_change_during_verification_refused(tmp_path, monkeypatch, after_apply):
    source, manifest = prepare_in_place(tmp_path)
    if after_apply:
        assert cli("apply", manifest).returncode == 0
    real_fingerprint = manifests.fingerprint
    backups = list(tmp_path.glob("*.bak-*"))

    def changed(handle):
        digest = real_fingerprint(handle)
        source.write_bytes(source.read_bytes() + b"\n")
        return digest

    monkeypatch.setattr(manifests, "fingerprint", changed)
    with pytest.raises(DoctorError, match="source changed|source differs"):
        manifests.apply(manifest)
    assert list(tmp_path.glob("*.bak-*")) == backups
    assert not list(tmp_path.glob(".rollout-doctor-*"))


def test_in_place_backup_exists_before_replace_and_survives_replace_failure(tmp_path, monkeypatch):
    source, manifest = prepare_in_place(tmp_path)
    original = source.read_bytes()

    def fail_replace(src, dst):
        backups = list(tmp_path.glob("*.bak-*"))
        assert len(backups) == 1 and backups[0].read_bytes() == original
        assert source.read_bytes() == original
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replacement failure"):
        manifests.apply(manifest)
    assert source.read_bytes() == original
    assert len(list(tmp_path.glob("*.bak-*"))) == 1
    assert not list(tmp_path.glob(".rollout-doctor-*"))


def test_in_place_failure_after_replacement_can_reapply_without_second_backup(tmp_path, monkeypatch):
    from agentwrench.doctor import files

    source, manifest = prepare_in_place(tmp_path)
    original = source.read_bytes()
    real_fsync_dir = files._fsync_dir
    calls = 0

    def fail_after_replace(parent):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated post-replacement fsync failure")
        return real_fsync_dir(parent)

    monkeypatch.setattr(files, "_fsync_dir", fail_after_replace)
    with pytest.raises(OSError, match="post-replacement"):
        manifests.apply(manifest)
    assert source.read_bytes() != original
    backups = list(tmp_path.glob("*.bak-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    before = {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in tmp_path.iterdir()}
    again = manifests.apply(manifest)
    assert again["status"] == "already_applied" and again["backup"] is None
    assert before == {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in tmp_path.iterdir()}
    assert calls == 2 and not list(tmp_path.glob(".rollout-doctor-*"))


@pytest.mark.parametrize("mutation", ["missing_report", "report_format", "report_file", "unknown_version", "no_backup"])
def test_in_place_already_applied_still_validates_manifest(tmp_path, mutation):
    source, manifest = prepare_in_place(tmp_path)
    assert cli("apply", manifest).returncode == 0
    value = json.loads(manifest.read_text())
    if mutation == "missing_report":
        value["report"] = {}
    elif mutation == "report_format":
        value["report"]["format"] = "unknown"
    elif mutation == "report_file":
        value["report"]["file"] = "elsewhere"
    elif mutation == "unknown_version":
        value["version"] = 3
    else:
        value["options"]["backup"] = False
    manifest.write_text(json.dumps(value))
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    run = cli("apply", manifest, "--json")
    assert run.returncode == 2 and "invalid repair manifest" in json.loads(run.stdout)["error"]
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}
