"""Resumable audit contracts, using local synthetic session records."""
import json

import pytest

from .make_fixtures import cnode, item, result, use
from .test_cli import cli
from .test_engine import raw


def audit(path, checkpoint, *args):
    return cli("audit", path, "--checkpoint", checkpoint, "--json", *args)


def test_resume_completed_tail_without_duplicate_diagnostics(tmp_path):
    path, checkpoint = tmp_path / "session.jsonl", tmp_path / "cursor.json"
    # UTF-8 and CRLF ensure offsets count bytes, rather than decoded characters.
    first = '{"type":"response_item","payload":{"type":"function_call_output","output":"é"}}\r\n'.encode()
    completed = raw(item("function_call", "c", name="shell", arguments="{}"))
    path.write_bytes(first + completed[:32])
    original = path.read_bytes()
    strict = audit(path, checkpoint)
    assert strict.returncode == 2
    assert not checkpoint.exists() and path.read_bytes() == original

    run = audit(path, checkpoint, "--allow-incomplete-final")
    assert run.returncode == 1, run.stderr
    report = json.loads(run.stdout)
    assert [(i["code"], i["byte_offset"], i["end_byte_offset"]) for i in report["issues"]] == [("RD103", 0, len(first))]
    assert report["offset"] == len(first) and report["resumed_from"] == 0
    assert report["incomplete_final"] == {"line": 2, "byte_offset": len(first), "end_byte_offset": len(original)}
    assert audit(path, checkpoint, "--allow-incomplete-final").returncode == 1
    assert json.loads(audit(path, checkpoint, "--allow-incomplete-final").stdout)["issues"] == []

    path.write_bytes(first + completed)
    run = audit(path, checkpoint)
    report = json.loads(run.stdout)
    assert run.returncode == 1
    assert [i["code"] for i in report["issues"]] == ["RD101"]
    assert report["issues"][0]["byte_offset"] == len(first)
    assert report["resumed_from"] == len(first) and report["offset"] == path.stat().st_size
    with path.open("ab") as target:
        target.write(raw(item("function_call_output", "c", output="ok")))
    report = json.loads(audit(path, checkpoint).stdout)
    assert report["issues"] == [] and report["issue_count"] == 1  # RD103 remains, RD101 resolves.
    assert path.read_bytes() == first + completed + raw(item("function_call_output", "c", output="ok"))
    assert checkpoint.stat().st_mode & 0o777 == 0o600
    state_text = checkpoint.read_text()
    assert '"output"' not in state_text and '"arguments"' not in state_text


@pytest.mark.parametrize("bad", [b'{bad}\n', b'{"x":', b'[]\n', b'{"x":NaN}',
                                     b'{"x":1,"x":2}', b'{"x":"\\q', b'{"x":truX',
                                     b'\xff', b'{"type":"response_item","payload":null}'])
def test_malformed_input_fails_without_advancing_checkpoint(tmp_path, bad):
    path, checkpoint = tmp_path / "s.jsonl", tmp_path / "cursor.json"
    first = raw(item("message", role="user", content=[]))
    path.write_bytes(first)
    assert audit(path, checkpoint).returncode == 0
    before = checkpoint.read_bytes()
    # A truncated fragment becomes an interior record here and must also fail.
    path.write_bytes(first + bad + b'\n' + raw(item("message", role="user", content=[])))
    run = audit(path, checkpoint, "--allow-incomplete-final")
    assert run.returncode == 2, run.stdout
    assert "error" in json.loads(run.stdout)
    assert checkpoint.read_bytes() == before
    # Malformed complete final records cannot hide behind the flag either.
    if bad != b'{"x":':
        path.write_bytes(first + bad)
        assert audit(path, checkpoint, "--allow-incomplete-final").returncode == 2
        assert checkpoint.read_bytes() == before


@pytest.mark.parametrize("tail", [b'{', b'{"type":', b'{"x":"unfinished', b'{"x":"a\\',
                                      b'{"x":"\\u12', b'{"x":tru', b'{"x":-1.2e+',
                                      b'{"x":[true,', b'{"x":"\xc3'])
def test_only_incomplete_final_record_may_be_deferred(tmp_path, tail):
    path, checkpoint = tmp_path / "s.jsonl", tmp_path / "cursor.json"
    first = raw(item("message", role="user", content=[]))
    path.write_bytes(first + tail)
    run = audit(path, checkpoint, "--allow-incomplete-final")
    assert run.returncode == 0, run.stderr
    report = json.loads(run.stdout)
    assert report["offset"] == len(first) and report["incomplete_final"]["line"] == 2


def test_resume_rejects_changed_prefix_rotation_and_format(tmp_path):
    path, checkpoint = tmp_path / "s.jsonl", tmp_path / "cursor.json"
    original = raw(item("function_call", "a", name="shell", arguments="{}"))
    path.write_bytes(original)
    assert audit(path, checkpoint).returncode == 1
    before = checkpoint.read_bytes()
    path.write_bytes(original.replace(b'"a"', b'"b"'))
    assert audit(path, checkpoint).returncode == 2
    path.write_bytes(original[:10])
    assert audit(path, checkpoint).returncode == 2
    path.write_bytes(original)
    assert audit(path, checkpoint, "--format", "codex").returncode == 2
    replacement = tmp_path / "replacement"
    replacement.write_bytes(original)
    replacement.replace(path)
    assert audit(path, checkpoint).returncode == 2
    assert checkpoint.read_bytes() == before


def test_claude_pairing_across_resume_and_valid_unterminated_record(tmp_path):
    path, checkpoint = tmp_path / "s.jsonl", tmp_path / "cursor.json"
    call = cnode("assistant", "a", None, [use("c")])
    path.write_bytes(raw(call).rstrip(b"\n"))
    run = audit(path, checkpoint)
    assert run.returncode == 1 and json.loads(run.stdout)["issues"][0]["code"] == "RD201"
    offset = path.stat().st_size
    with path.open("ab") as target:
        target.write(b"\n" + raw(cnode("user", "b", "a", [result("c")])))
    run = audit(path, checkpoint)
    report = json.loads(run.stdout)
    assert run.returncode == 0 and report["issues"] == [] and report["issue_count"] == 0
    assert report["resumed_from"] == offset


def test_checkpoint_is_not_session_and_bad_state_is_refused(tmp_path):
    path, checkpoint = tmp_path / "s.jsonl", tmp_path / "cursor.json"
    data = raw(item("message", role="user", content=[]))
    path.write_bytes(data)
    assert audit(path, path).returncode == 2
    assert path.read_bytes() == data
    checkpoint.symlink_to(path)
    assert audit(path, checkpoint).returncode == 2
    checkpoint.unlink()
    checkpoint.write_text('{"version":1}')
    assert audit(path, checkpoint).returncode == 2
    assert path.read_bytes() == data


@pytest.mark.parametrize("tail", [b' ', b'{bad}', b'{"x":01', b'{"x":+1', b'{"x":Na',
                                      b'{"x":[1,]}', b'{"x":"a\x01', b'{"x":"\\uZZ',
                                      b'{"x":1,"x":', b'{}x', b'[]', b'{"x":}\n'])
def test_invalid_final_prefix_is_not_truncation(tmp_path, tail):
    path, checkpoint = tmp_path / "s.jsonl", tmp_path / "cursor.json"
    path.write_bytes(raw(item("message", role="user", content=[])) + tail)
    run = audit(path, checkpoint, "--allow-incomplete-final")
    assert run.returncode == 2
    assert not checkpoint.exists()


def test_every_byte_prefix_of_complex_object_is_resumable():
    from agentwrench.doctor.audit import incomplete_object
    record = '{"é":[true,false,null,-12.5e+10,{"x":"quote\\\"and\\u1234"}]}'.encode()
    for end in range(1, len(record)):
        assert incomplete_object(record[:end]), end
    assert not incomplete_object(record)


def test_initial_partial_empty_then_complete_and_new_issue_offsets(tmp_path):
    path, checkpoint = tmp_path / "s.jsonl", tmp_path / "cursor.json"
    path.write_bytes(b'{"type":')
    run = audit(path, checkpoint, "--allow-incomplete-final")
    assert run.returncode == 0 and json.loads(run.stdout)["offset"] == 0
    data = raw(item("function_call_output", "missing", output="hidden"))
    path.write_bytes(data)
    run = audit(path, checkpoint)
    issue = json.loads(run.stdout)["issues"][0]
    assert run.returncode == 1 and issue["code"] == "RD102"
    assert issue["line"] == 1 and issue["end_byte_offset"] == len(data)
    second = raw(item("function_call_output", "missing", output="another"))
    with path.open("ab") as target:
        target.write(second)
    run = audit(path, checkpoint)
    issues = json.loads(run.stdout)["issues"]
    assert len(issues) == 1 and issues[0]["line"] == 2 and issues[0]["byte_offset"] == len(data)


def test_mixed_formats_and_unknown_formats_leave_no_checkpoint(tmp_path):
    path, checkpoint = tmp_path / "s.jsonl", tmp_path / "cursor.json"
    path.write_bytes(raw(item("message", role="user", content=[]), cnode("user", "u")))
    assert audit(path, checkpoint).returncode == 2 and not checkpoint.exists()
    path.write_bytes(b'{"custom":true}\n')
    assert audit(path, checkpoint).returncode == 2
    assert audit(path, checkpoint, "--format", "codex").returncode == 0


def test_checkpoint_lock_and_publish_failure(tmp_path, monkeypatch):
    import fcntl
    import agentwrench.doctor.audit as audit_module
    from agentwrench.doctor.engine import DoctorError

    path, checkpoint = tmp_path / "s.jsonl", tmp_path / "cursor.json"
    path.write_bytes(raw(item("message", role="user", content=[])))
    assert audit(path, checkpoint).returncode == 0
    before = checkpoint.read_bytes()
    with checkpoint.with_name(checkpoint.name + ".lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(DoctorError, match="locked"):
            audit_module.audit(path, checkpoint)
    def fail(*_):
        raise OSError("synthetic publication failure")
    monkeypatch.setattr(audit_module.os, "replace", fail)
    with pytest.raises(OSError, match="synthetic publication"):
        audit_module.audit(path, checkpoint)
    assert checkpoint.read_bytes() == before
    assert not list(tmp_path.glob(".rollout-doctor-audit-*"))


def test_changed_source_during_read_keeps_checkpoint(tmp_path, monkeypatch):
    import agentwrench.doctor.audit as audit_module
    from agentwrench.doctor.engine import DoctorError

    path, checkpoint = tmp_path / "s.jsonl", tmp_path / "cursor.json"
    path.write_bytes(raw(item("message", role="user", content=[])))
    assert audit(path, checkpoint).returncode == 0
    before = checkpoint.read_bytes()
    original_pair = audit_module.pair
    def append_while_pairing(*args, **kwargs):
        report = original_pair(*args, **kwargs)
        with path.open("ab") as target:
            target.write(b'{}\n')
        return report
    monkeypatch.setattr(audit_module, "pair", append_while_pairing)
    with pytest.raises(DoctorError, match="changed while reading"):
        audit_module.audit(path, checkpoint)
    assert checkpoint.read_bytes() == before
