"""Pin complete locked manifest bytes before opening an explicit candidate."""
import json
import os

import pytest

from agentwrench.doctor import recovery
from agentwrench.doctor.engine import DoctorError
from .test_output_summary import bind, dirty, invoke, MODES
from .test_output_verification import prepare_mode
from .test_backup_verification import inventory
from .test_recovery import sha

MISMATCH = "manifest SHA-256 differs from --expect-manifest-sha256; no candidate opened"
INVALID = "--expect-manifest-sha256 requires exactly 64 lowercase hex characters"
OPTIONS = [[], ["--check-pairing"], ["--check-pairing", "--diagnostic-summary"]]


@pytest.mark.parametrize("fmt", ["codex", "claude"])
@pytest.mark.parametrize("mode", MODES)
def test_pin_mismatch_before_candidate_open(tmp_path, capsys, monkeypatch, fmt, mode):
    _, manifest, candidate, _ = prepare_mode(tmp_path, mode, fmt)
    before = inventory(tmp_path)
    opened = []
    real_open = os.open

    def readonly(path, flags, *args, **kwargs):
        assert path == manifest, "mismatch must never open candidate or source"
        assert flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND) == 0
        opened.append(path)
        return real_open(path, flags, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("pin mismatch attempted scan, manifest parse or repair/write")

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", readonly)
        patch.setattr(recovery, "scan", forbidden)
        patch.setattr(recovery, "_read", forbidden)
        for options in OPTIONS:
            for output in ([], ["--json"]):
                code, out, err = invoke(capsys, manifest, candidate, "--expect-manifest-sha256", "0" * 64,
                                        *options, *output)
                assert code == 2 and err == f"error: {MISMATCH}\n"
                assert (json.loads(out) == {"file": str(manifest), "error": MISMATCH}) if output else out == ""
    assert opened == [manifest] * 6
    assert before == inventory(tmp_path)


@pytest.mark.parametrize("fmt", ["codex", "claude"])
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("kind", ["clean", "binary", "empty", "dirty", "malformed", "format"])
def test_pin_agreement_preserves_all_receipts(tmp_path, capsys, fmt, mode, kind):
    source, manifest, candidate, _ = prepare_mode(tmp_path, mode, fmt)
    if kind != "clean":
        data = {"binary": b"\xff\x00PRIVATE\r\n", "empty": b"", "dirty": dirty(fmt)[0],
                "malformed": b"PRIVATE not JSON\n",
                "format": b'{"uuid":"PRIVATE-other"}\n' if fmt == "codex" else b'{"type":"session_meta"}\n'}[kind]
        bind(manifest, candidate, data)
    if mode != "v2source":
        source.unlink()  # Pinned copies require no original or backup.
    before = inventory(tmp_path)
    pin = sha(manifest.read_bytes())
    for options in OPTIONS:
        for output in ([], ["--json"]):
            baseline = invoke(capsys, manifest, candidate, *options, *output)
            assert baseline[0] == (2 if options and kind not in ("clean", "empty") else 0)
            assert invoke(capsys, manifest, candidate, "--expect-manifest-sha256", pin,
                          *options, *output) == baseline
    assert before == inventory(tmp_path)


@pytest.mark.parametrize("pin", ["", "0" * 63, "0" * 65, "A" * 64, "g" * 64,
                                 "0" * 63 + "\n", " " + "0" * 64, "0" * 64 + " ",
                                 "é" * 64, "０" * 64, "sha256:" + "a" * 64])
def test_invalid_pin_never_opens_either_input(tmp_path, capsys, monkeypatch, pin):
    manifest, candidate = tmp_path / "missing-plan", tmp_path / "missing-candidate"

    def forbidden(*args, **kwargs):
        pytest.fail("invalid pin attempted input open")

    monkeypatch.setattr(os, "open", forbidden)
    for output in ([], ["--json"]):
        code, out, err = invoke(capsys, manifest, candidate, "--expect-manifest-sha256", pin,
                                "--check-pairing", "--diagnostic-summary", *output)
        assert code == 2 and err == f"error: {INVALID}\n"
        assert (json.loads(out) == {"file": str(manifest), "error": INVALID}) if output else out == ""


@pytest.mark.parametrize("pin", [False, 0, [], {}, b"0" * 64])
def test_api_pin_type_is_strict(tmp_path, pin):
    with pytest.raises(DoctorError, match="requires exactly 64 lowercase hex characters"):
        recovery.verify_output(tmp_path / "missing", tmp_path / "candidate", expect_manifest_sha256=pin)


def test_missing_pin_value_is_argument_error(tmp_path, capsys):
    code, out, err = invoke(capsys, tmp_path / "plan", tmp_path / "candidate",
                            "--json", "--expect-manifest-sha256")
    assert code == 2 and out == ""
    assert err == "error: argument --expect-manifest-sha256: expected one argument\n"


@pytest.mark.parametrize("fmt", ["codex", "claude"])
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("change", ["whitespace", "keys", "newline", "historical_report", "multichunk"])
def test_pin_binds_every_manifest_byte(tmp_path, capsys, monkeypatch, fmt, mode, change):
    _, manifest, candidate, _ = prepare_mode(tmp_path, mode, fmt)
    original = manifest.read_bytes()
    value = json.loads(original)
    if change == "whitespace":
        altered = json.dumps(value, separators=(",", ":")).encode()
    elif change == "keys":
        altered = json.dumps(dict(reversed(list(value.items()))), indent=2).encode() + b"\n"
    elif change == "newline":
        altered = original.rstrip(b"\n")
    elif change == "multichunk":
        altered = original + b" " * (2 * 1024 * 1024) + b"\n"
    else:
        value["report"]["issues"] = ["PRIVATE replaced historical metadata"]
        altered = json.dumps(value).encode()
    assert altered != original
    if change != "historical_report":
        assert json.loads(altered) == value
    manifest.write_bytes(altered)
    before = inventory(tmp_path)
    real_open = os.open

    def manifest_only(path, *args, **kwargs):
        assert path == manifest, "byte-different manifest must refuse before candidate open"
        return real_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", manifest_only)
        code, out, err = invoke(capsys, manifest, candidate, "--expect-manifest-sha256", sha(original), "--json")
        assert code == 2 and json.loads(out) == {"file": str(manifest), "error": MISMATCH}
        assert err == f"error: {MISMATCH}\n"
    # A newly retained exact full-byte pin agrees, even with arbitrary historical metadata.
    code, _, _ = invoke(capsys, manifest, candidate, "--expect-manifest-sha256", sha(altered), "--json")
    assert code == 0
    assert before == inventory(tmp_path)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("options", OPTIONS)
def test_pin_single_open_hashes_locks_and_no_writes(tmp_path, capsys, monkeypatch, mode, options):
    import fcntl
    import tempfile
    from agentwrench.doctor import manifest as manifest_module

    _, manifest, candidate, _ = prepare_mode(tmp_path, mode)
    pin = sha(manifest.read_bytes())
    before = inventory(tmp_path)
    opened, hashes, scans = [], [], []
    real_open, real_fingerprint, real_scan = os.open, recovery.fingerprint, recovery.scan

    def readonly(path, flags, *args, **kwargs):
        assert path in (manifest, candidate)
        assert flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND) == 0
        opened.append(path)
        return real_open(path, flags, *args, **kwargs)

    def fingerprint(stream):
        hashes.append(stream.name)
        return real_fingerprint(stream)

    def scan(*args):
        for path in (manifest, candidate):
            with path.open("rb") as stream:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        scans.append(args[1])
        return real_scan(*args)

    def forbidden(*args, **kwargs):
        pytest.fail("pinning attempted write, source scan or repair replay")

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", readonly)
        patch.setattr(recovery, "fingerprint", fingerprint)
        patch.setattr(recovery, "scan", scan if options else forbidden)
        for name in ("link", "replace", "unlink", "mkdir"):
            patch.setattr(os, name, forbidden)
        for name in ("mkstemp", "mkdtemp"):
            patch.setattr(tempfile, name, forbidden)
        for name in ("scan", "apply", "make_plan", "validate_plan", "render"):
            patch.setattr(manifest_module, name, forbidden)
        code, _, err = invoke(capsys, manifest, candidate, "--expect-manifest-sha256", pin, *options, "--json")
        assert code == 0 and err == ""
    assert opened == [manifest, candidate]
    assert len(hashes) == (5 if options else 3)  # Existing initial/final passes only.
    assert scans == ([str(candidate)] if options else [])
    assert before == inventory(tmp_path)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("stage", ["initial_hash", "manifest_parse", "candidate_read", "scan", "final_hash"])
@pytest.mark.parametrize("change", ["bytes", "replace", "unlink", "metadata", "restore_mtime"])
def test_pinned_manifest_mutations_still_refuse(tmp_path, capsys, monkeypatch, mode, stage, change):
    _, manifest, candidate, _ = prepare_mode(tmp_path, mode)
    original = manifest.read_bytes()
    pin = sha(original)
    st = manifest.stat()
    observed = None
    hash_calls = 0

    def mutate():
        nonlocal observed
        if change == "bytes":
            manifest.write_bytes(original + b" ")
        elif change == "replace":
            replacement = tmp_path / "replacement"
            replacement.write_bytes(original)
            os.replace(replacement, manifest)
        elif change == "unlink":
            manifest.unlink()
        elif change == "metadata":
            manifest.chmod(0o400)
        else:
            manifest.write_bytes(original)
            os.utime(manifest, ns=(st.st_atime_ns, st.st_mtime_ns))
        observed = inventory(tmp_path)

    real_hash, real_read, real_bytes, real_scan = recovery.fingerprint, recovery._read, recovery._read_backup, recovery.scan

    def fingerprint(stream):
        nonlocal hash_calls
        result = real_hash(stream)
        hash_calls += 1
        if (stage == "initial_hash" and hash_calls == 1) or (stage == "final_hash" and hash_calls == 3):
            mutate()
        return result

    def read(stream):
        result = real_read(stream)
        if stage == "manifest_parse":
            mutate()
        return result

    def read_bytes(stream):
        result = real_bytes(stream)
        if stage == "candidate_read":
            mutate()
        return result

    def scan(*args):
        result = real_scan(*args)
        if stage == "scan":
            mutate()
        return result

    monkeypatch.setattr(recovery, "fingerprint", fingerprint)
    monkeypatch.setattr(recovery, "_read", read)
    monkeypatch.setattr(recovery, "_read_backup", read_bytes)
    monkeypatch.setattr(recovery, "scan", scan)
    code, out, err = invoke(capsys, manifest, candidate, "--expect-manifest-sha256", pin,
                            "--check-pairing", "--diagnostic-summary", "--json")
    assert code == 2 and set(json.loads(out)) == {"file", "error"}
    assert "error: " in err and "Traceback" not in err
    assert observed is not None and observed == inventory(tmp_path)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("case", ["schema", "version", "policy", "candidate_hash", "source_alias", "manifest_alias"])
def test_exact_pin_keeps_strict_preflight_policies(tmp_path, capsys, monkeypatch, mode, case):
    source, manifest, candidate, _ = prepare_mode(tmp_path, mode)
    value = json.loads(manifest.read_text())
    if case == "schema":
        value["extra"] = True
    elif case == "version":
        value["version"] = True
    elif case == "policy":
        value["options"]["backup"] = False
    elif case == "candidate_hash":
        candidate.write_bytes(b"PRIVATE wrong candidate")
    elif case == "source_alias":
        candidate = tmp_path / "alias"
        candidate.hardlink_to(source)
    else:
        candidate = manifest
    manifest.write_text(json.dumps(value))
    pin = sha(manifest.read_bytes())
    before = inventory(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("strict preflight refusal must not scan")

    monkeypatch.setattr(recovery, "scan", forbidden)
    code, out, err = invoke(capsys, manifest, candidate, "--expect-manifest-sha256", pin,
                            "--check-pairing", "--diagnostic-summary", "--json")
    assert code == 2 and set(json.loads(out)) == {"file", "error"}
    assert "error: " in err and before == inventory(tmp_path)
