"""Explicit refusal summaries expose only stable categories and diagnostic counts."""
import json

import pytest

from agentwrench.doctor.cli import main
from .test_output_verification import prepare_mode
from .test_backup_verification import inventory
from .test_recovery import sha

ERROR = "candidate pairing check failed; no output published"
MODES = ["v1", "v2copy", "v2source"]


def dirty(fmt):
    if fmt == "codex":
        records = [{"type": "response_item", "payload": {"type": "function_call", "call_id": f"PRIVATE-{i}",
                    "arguments": "PRIVATE payload"}} for i in range(2)]
        records += [{"type": "response_item", "payload": {"type": "function_call_output", "output": "PRIVATE"}}] * 2
        counts = {"RD101": 2, "RD103": 2}
    else:
        records = [{"type": "assistant", "uuid": "PRIVATE-uuid", "message": {"content": [
            {"type": "tool_use", "id": f"PRIVATE-{i}", "input": "PRIVATE payload"} for i in range(2)
        ] + [{"type": "tool_use", "input": "PRIVATE"}] * 2}}]
        counts = {"RD201": 2, "RD206": 2}
    return b"".join((json.dumps(r) + "\r\n").encode() for r in records), counts


def bind(manifest, candidate, data, *, saved_format=None):
    value = json.loads(manifest.read_text())
    value["output"]["sha256"] = sha(data)
    if saved_format:
        value["report"]["format"] = saved_format
    value["report"]["issues"] = ["PRIVATE historical issue"]
    value["report"]["changes"] = ["PRIVATE historical change"]
    manifest.write_text(json.dumps(value))
    candidate.write_bytes(data)


def invoke(capsys, manifest, candidate, *args):
    code = main(["verify-output", str(manifest), "--candidate", str(candidate), *args])
    captured = capsys.readouterr()
    assert "PRIVATE" not in captured.out + captured.err
    return code, captured.out, captured.err


@pytest.mark.parametrize("fmt", ["codex", "claude"])
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("kind", ["pairing", "malformed", "format"])
def test_summary_refusal_contract(tmp_path, capsys, fmt, mode, kind):
    source, manifest, candidate, _ = prepare_mode(tmp_path, mode, fmt)
    data, counts = dirty(fmt)
    reason = "pairing_findings"
    if kind == "malformed":
        data += b'PRIVATE invalid JSON\n["PRIVATE"]\n{"PRIVATE":"unfinished'
        counts = {"RD001": 1, "RD002": 1, "RD003": 1, **counts}
        reason = "malformed_records"
    elif kind == "format":
        data += b'{"uuid":"PRIVATE-other"}\n' if fmt == "codex" else b'{"type":"session_meta"}\n'
        counts, reason = {}, "format_refusal"
    bind(manifest, candidate, data)
    if mode != "v2source":
        source.unlink()
    before = inventory(tmp_path)
    code, out, err = invoke(capsys, manifest, candidate, "--check-pairing", "--diagnostic-summary", "--json")
    assert code == 2
    assert json.loads(out) == {"file": str(manifest), "error": ERROR,
                              "diagnostic_summary": {"reason": reason, "issue_counts": counts}}
    assert err == f"error: {ERROR}\n"
    code, out, err = invoke(capsys, manifest, candidate, "--check-pairing", "--diagnostic-summary")
    pairs = ", ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "none"
    assert code == 2 and out == ""
    assert err == f"error: {ERROR}\npairing summary: {reason}; issue counts: {pairs}\n"
    assert before == inventory(tmp_path)


@pytest.mark.parametrize("fmt", ["codex", "claude"])
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("kind", ["clean", "empty", "markerless", "saved_empty"])
def test_summary_does_not_change_success_receipts(tmp_path, capsys, fmt, mode, kind):
    _, manifest, candidate, data = prepare_mode(tmp_path, mode, fmt)
    if kind in ("empty", "saved_empty"):
        data = b""
    elif kind == "markerless":
        data = b'{"PRIVATE":"unrelated metadata"}'
    bind(manifest, candidate, data, saved_format="empty" if kind == "saved_empty" else None)
    before = inventory(tmp_path)
    for options in (["--json"], []):
        baseline = invoke(capsys, manifest, candidate, "--check-pairing", *options)
        assert baseline[0] == 0
        assert invoke(capsys, manifest, candidate, "--check-pairing", "--diagnostic-summary", *options) == baseline
    assert inventory(tmp_path) == before


@pytest.mark.parametrize("data", [b'{"PRIVATE":1}\n', b'{"type":"session_meta"}\n', b'{"uuid":"PRIVATE"}\n'])
def test_summary_saved_empty_format_refusal(tmp_path, capsys, data):
    _, manifest, candidate, _ = prepare_mode(tmp_path, "v1")
    bind(manifest, candidate, data, saved_format="empty")
    code, out, _ = invoke(capsys, manifest, candidate, "--check-pairing", "--diagnostic-summary", "--json")
    assert code == 2
    assert json.loads(out)["diagnostic_summary"] == {"reason": "format_refusal", "issue_counts": {}}


@pytest.mark.parametrize("fmt", ["codex", "claude"])
@pytest.mark.parametrize("kind", ["invalid_utf8", "duplicate", "nonfinite", "structure", "mismatched"])
def test_summary_parser_and_format_boundaries(tmp_path, capsys, fmt, kind):
    _, manifest, candidate, _ = prepare_mode(tmp_path, "v1", fmt)
    data = {"invalid_utf8": b'\xffPRIVATE\n', "duplicate": b'{"PRIVATE":1,"PRIVATE":2}\n',
            "nonfinite": b'{"PRIVATE":NaN}\n',
            "structure": b'{"type":"response_item","payload":"PRIVATE"}\n' if fmt == "codex" else
                         b'{"type":"assistant","message":{"content":"PRIVATE"}}\n',
            "mismatched": b'{"uuid":"PRIVATE"}\n' if fmt == "codex" else b'{"type":"session_meta"}\n'}[kind]
    bind(manifest, candidate, data)
    code, out, _ = invoke(capsys, manifest, candidate, "--check-pairing", "--diagnostic-summary", "--json")
    assert code == 2
    expected = ({"reason": "format_refusal", "issue_counts": {}} if kind == "mismatched" else
                {"reason": "malformed_records", "issue_counts": {"RD003" if kind == "structure" else "RD001": 1}})
    assert json.loads(out)["diagnostic_summary"] == expected


def test_summary_requires_pairing_and_other_failures_stay_generic(tmp_path, capsys, monkeypatch):
    from agentwrench.doctor import recovery

    _, manifest, candidate, _ = prepare_mode(tmp_path, "v1")
    before = inventory(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("hash-only/invalid request must not scan")
    monkeypatch.setattr(recovery, "scan", forbidden)
    code, out, err = invoke(capsys, manifest, candidate, "--diagnostic-summary", "--json")
    error = "--diagnostic-summary requires --check-pairing"
    assert code == 2 and json.loads(out) == {"file": str(manifest), "error": error}
    assert err == f"error: {error}\n" and inventory(tmp_path) == before
    candidate.write_bytes(b"PRIVATE arbitrary bytes\x00\xff")
    code, out, _ = invoke(capsys, manifest, candidate, "--check-pairing", "--diagnostic-summary", "--json")
    assert code == 2 and set(json.loads(out)) == {"file", "error"}
    assert "SHA-256" in json.loads(out)["error"]
    bind(manifest, candidate, candidate.read_bytes())
    assert invoke(capsys, manifest, candidate, "--json")[0] == 0
    value = json.loads(manifest.read_text())
    value["options"]["PRIVATE-extra"] = True
    manifest.write_text(json.dumps(value))
    code, out, _ = invoke(capsys, manifest, candidate, "--check-pairing", "--diagnostic-summary", "--json")
    assert code == 2 and set(json.loads(out)) == {"file", "error"}


@pytest.mark.parametrize("fmt", ["codex", "claude"])
@pytest.mark.parametrize("mode", MODES)
def test_summary_single_scan_locks_no_original_open_or_writes(tmp_path, monkeypatch, capsys, fmt, mode):
    import fcntl
    import os
    import tempfile
    from agentwrench.doctor import recovery, manifest as manifest_module, repair

    source, manifest, candidate, _ = prepare_mode(tmp_path, mode, fmt)
    data, counts = dirty(fmt)
    bind(manifest, candidate, data)
    if mode != "v2source":
        source.unlink()
    before = inventory(tmp_path)
    real_open, real_scan = os.open, recovery.scan
    opened, scanned = [], []

    def readonly(path, flags, *args, **kwargs):
        assert flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND) == 0
        assert path in (manifest, candidate)
        opened.append(path)
        return real_open(path, flags, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("summary attempted write, repair planning/apply or historical replay")

    def locked_scan(stream, file, format):
        assert file == str(candidate) and format == fmt and stream.tell() == 0
        for path in (manifest, candidate):
            with path.open("rb") as independent:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(independent, fcntl.LOCK_EX | fcntl.LOCK_NB)
        scanned.append(file)
        return real_scan(stream, file, format)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", readonly)
        for name in ("link", "replace", "unlink", "mkdir"):
            patch.setattr(os, name, forbidden)
        patch.setattr(tempfile, "mkstemp", forbidden)
        patch.setattr(tempfile, "mkdtemp", forbidden)
        patch.setattr(manifest_module, "scan", forbidden)
        patch.setattr(manifest_module, "apply", forbidden)
        patch.setattr(repair, "make_plan", forbidden)
        patch.setattr(recovery, "scan", locked_scan)
        code, out, _ = invoke(capsys, manifest, candidate, "--check-pairing", "--diagnostic-summary", "--json")
    assert code == 2 and json.loads(out)["diagnostic_summary"]["issue_counts"] == counts
    assert opened == [manifest, candidate] and scanned == [str(candidate)]
    assert inventory(tmp_path) == before


@pytest.mark.parametrize("endpoint", ["manifest", "candidate"])
@pytest.mark.parametrize("outcome", ["findings", "format_error"])
@pytest.mark.parametrize("change", ["rewrite", "replace", "metadata", "restore_mtime", "alias"])
def test_summary_never_masks_mutation_refusals(tmp_path, monkeypatch, capsys, endpoint, outcome, change):
    import os
    from agentwrench.doctor import recovery
    from agentwrench.doctor.engine import FormatError

    source, manifest, candidate, _ = prepare_mode(tmp_path, "v1")
    data, _ = dirty("codex")
    if outcome == "format_error":
        data += b'{"uuid":"PRIVATE-other"}\n'
    bind(manifest, candidate, data)
    path = manifest if endpoint == "manifest" else candidate
    old = path.stat()
    real_scan = recovery.scan
    observed = None

    def mutate():
        nonlocal observed
        if change == "rewrite":
            with path.open("r+b") as stream:
                stream.write(b"!")
        elif change == "replace":
            replacement = tmp_path / "replacement"
            replacement.write_bytes(path.read_bytes())
            os.replace(replacement, path)
        elif change == "metadata":
            path.chmod(0o400)
        elif change == "restore_mtime":
            path.write_bytes(path.read_bytes())
            os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
        else:
            source.unlink()
            source.hardlink_to(path)
        observed = inventory(tmp_path)

    def scan(*args):
        try:
            report = real_scan(*args)
        except FormatError:
            mutate()
            raise
        mutate()
        return report

    monkeypatch.setattr(recovery, "scan", scan)
    code, out, _ = invoke(capsys, manifest, candidate, "--check-pairing", "--diagnostic-summary", "--json")
    assert code == 2 and set(json.loads(out)) == {"file", "error"}
    assert "changed" in json.loads(out)["error"] or "separate" in json.loads(out)["error"]
    assert observed == inventory(tmp_path)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("case", ["bool_version", "unsupported_saved_format", "extra_policy", "manifest_candidate_alias",
                                 "source_hardlink", "candidate_symlink", "manifest_symlink", "candidate_lock", "manifest_lock"])
def test_summary_preflight_refusals_have_no_summary(tmp_path, capsys, monkeypatch, mode, case):
    import fcntl
    from contextlib import ExitStack
    from agentwrench.doctor import recovery

    source, manifest, candidate, _ = prepare_mode(tmp_path, mode)
    value = json.loads(manifest.read_text())
    def forbidden(*args, **kwargs):
        pytest.fail("unsafe inputs must refuse before scanning")
    monkeypatch.setattr(recovery, "scan", forbidden)
    with ExitStack() as stack:
        if case in ("bool_version", "unsupported_saved_format", "extra_policy"):
            if case == "bool_version":
                value["version"] = True
            elif case == "unsupported_saved_format":
                value["report"]["format"] = "PRIVATE-unsupported"
            else:
                value["options"]["PRIVATE-extra"] = True
            manifest.write_text(json.dumps(value))
        elif case == "manifest_candidate_alias":
            candidate = manifest
        elif case == "source_hardlink":
            alias = tmp_path / "source-alias"
            alias.hardlink_to(source)
            candidate = alias
        elif case.endswith("symlink"):
            endpoint = candidate if case == "candidate_symlink" else manifest
            moved = tmp_path / "moved"
            endpoint.rename(moved)
            endpoint.symlink_to(moved)
        else:
            endpoint = candidate if case == "candidate_lock" else manifest
            stream = stack.enter_context(endpoint.open("rb"))
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = inventory(tmp_path)
        code, out, _ = invoke(capsys, manifest, candidate, "--check-pairing", "--diagnostic-summary", "--json")
        assert code == 2 and set(json.loads(out)) == {"file", "error"}
        assert inventory(tmp_path) == before
