"""Incremental audit must decode append bytes only and match full replay."""
from io import BytesIO
from pathlib import Path
import hashlib
import json

import pytest

import agentwrench.doctor.audit as audit_module
import agentwrench.doctor.engine as engine
from .make_fixtures import cnode, item, result, use
from .test_engine import raw


@pytest.mark.parametrize("fmt,first,second", [
    ("codex", item("function_call", "c", arguments="SECRET-ARGUMENT"),
     item("function_call_output", "c", output="SECRET-OUTPUT")),
    ("claude", cnode("assistant", "a", blocks=[use("c")]),
     cnode("user", "b", "a", [result("c")])),
])
def test_resume_decodes_only_appended_records(tmp_path, monkeypatch, fmt, first, second):
    path, checkpoint = tmp_path / "s.jsonl", tmp_path / "cursor.json"
    path.write_bytes(raw(first))
    assert audit_module.audit(path, checkpoint)["issue_count"] == 1
    # Watch both decode entry points: decoding the checkpoint is allowed, old
    # source records are not. This also catches a double decode of appended JSON.
    original = engine.decode
    decoded = []
    def watch(data):
        if b'"version"' not in data:
            decoded.append(data)
        return original(data)
    monkeypatch.setattr(engine, "decode", watch)
    monkeypatch.setattr(audit_module, "decode", watch)
    with path.open("ab") as stream:
        stream.write(raw(second))
    report = audit_module.audit(path, checkpoint)
    assert decoded == [raw(second)]
    assert report["issue_count"] == 0 and report["issues"] == []
    state = json.loads(checkpoint.read_text())
    assert state["version"] == 2
    assert state["digest"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert "SECRET-" not in checkpoint.read_text()
    decoded.clear()
    audit_module.audit(path, checkpoint)
    assert decoded == []


SEQUENCES = [
    [item("function_call_output", "c", output="early"),
     item("custom_tool_call", "c", input="secret"),
     item("function_call_output", "c"),
     item("custom_tool_call_output", "c"),
     item("custom_tool_call_output", "c"), item("function_call", "c"),
     item("function_call_output"), item("function_call")],
    [cnode("assistant", "root", blocks=[use("c")]),
     cnode("user", "sibling", blocks=[result("c")]),
     cnode("user", "side", "root", [result("c")], True),
     cnode("user", "session", "root", [result("c")], session="other"),
     cnode("user", "ok", "root", [result("c")]),
     cnode("user", "again", "ok", [result("c")]),
     cnode("assistant", "dup", "root", [use("c")]),
     cnode("assistant", "independent", blocks=[use("c")]),
     cnode("user", "independent-result", "independent", [result("c")])],
    [cnode("assistant", "child", "later", [use("c")]),
     cnode("user", "result", "child", [result("c")]),
     cnode("user", "later"),  # RD204 resolves into RD207; pairing is suppressed.
     cnode("user", "later")],
    [cnode("assistant", "a", blocks=[{"type": "tool_use"}]),
     cnode("user", "b", "a", [use("wrong-role")])],
]


@pytest.mark.parametrize("records", SEQUENCES)
@pytest.mark.parametrize("fmt", ["auto", "explicit"])
def test_snapshot_matches_full_replay_at_every_append(tmp_path, records, fmt):
    path, checkpoint = tmp_path / "s.jsonl", tmp_path / "cursor.json"
    format = ("codex" if records[0]["type"] == "response_item" else "claude") if fmt == "explicit" else "auto"
    data, emitted = b"", set()
    for record in records:
        chunk = raw(record).replace(b"\n", b"\r\n")
        with path.open("ab") as stream:
            stream.write(chunk)
        data += chunk
        full = engine.scan(BytesIO(data), str(path), format)
        ranges, offset = {}, 0
        for line, row in enumerate(data.splitlines(keepends=True), 1):
            ranges[line] = (offset, offset + len(row))
            offset += len(row)
        expected = []
        for issue in full.issues:
            value = issue.as_dict()
            value["byte_offset"], value["end_byte_offset"] = ranges[issue.line]
            key = json.dumps(value, sort_keys=True)
            if key not in emitted:
                expected.append(value)
                emitted.add(key)
        report = audit_module.audit(path, checkpoint, format)
        assert report["format"] == full.format
        assert report["issue_count"] == len(full.issues)
        assert report["issues"] == expected
        assert report["offset"] == len(data)
        assert json.loads(checkpoint.read_text())["digest"] == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("fmt,call,output", [
    ("codex", item("function_call", "c"), item("function_call_output", "c")),
    ("claude", cnode("assistant", "a", blocks=[use("c")]),
     cnode("user", "b", "a", [result("c")]))])
def test_partial_append_completion_keeps_cursor_and_replays_no_prefix(tmp_path, monkeypatch, fmt, call, output):
    path, checkpoint = tmp_path / "s.jsonl", tmp_path / "cursor.json"
    first, final = raw(call), raw(output)
    path.write_bytes(first + final[:20])
    initial = audit_module.audit(path, checkpoint, fmt, True)
    assert initial["offset"] == len(first) and initial["incomplete_final"]["line"] == 2
    original = audit_module.decode
    def no_prefix(data):
        assert data != first, "accepted prefix was decoded again"
        return original(data)
    monkeypatch.setattr(audit_module, "decode", no_prefix)
    assert audit_module.audit(path, checkpoint, fmt, True)["issues"] == []
    with path.open("ab") as stream:
        stream.write(final[20:])
    completed = audit_module.audit(path, checkpoint, fmt)
    assert completed["issue_count"] == 0 and completed["offset"] == len(first + final)
    assert completed["incomplete_final"] is None


@pytest.mark.parametrize("separator", [b"\n", b"\r\n", b" \t\r\n"])
def test_unterminated_record_separator_matches_replay(tmp_path, separator):
    path, checkpoint = tmp_path / "s.jsonl", tmp_path / "cursor.json"
    first = raw(item("function_call", "c")).rstrip(b"\n")
    path.write_bytes(first)
    audit_module.audit(path, checkpoint)
    with path.open("ab") as stream:
        stream.write(separator)
    report = audit_module.audit(path, checkpoint)
    # Diagnostic byte range changes when a separator is appended, as in full replay.
    assert report["issue_count"] == 1
    assert report["issues"][0]["end_byte_offset"] == len(first + separator)
    assert json.loads(checkpoint.read_text())["pairing"]["lines"] == 1
    with path.open("ab") as stream:
        stream.write(raw(item("function_call_output", "c")))
    assert audit_module.audit(path, checkpoint)["issue_count"] == 0


def test_concatenation_after_unterminated_object_refused_without_write(tmp_path):
    path, checkpoint = tmp_path / "s.jsonl", tmp_path / "cursor.json"
    path.write_bytes(raw(item("function_call", "c")).rstrip(b"\n"))
    audit_module.audit(path, checkpoint)
    before = checkpoint.read_bytes()
    with path.open("ab") as stream:
        stream.write(raw(item("function_call_output", "c")))
    source = path.read_bytes()
    with pytest.raises(engine.DoctorError, match="newline separator"):
        audit_module.audit(path, checkpoint)
    assert checkpoint.read_bytes() == before and path.read_bytes() == source


def corrupt(value, case):
    pairing = value["pairing"]
    if case == "v1":
        value["version"] = 1
        del value["pairing"]
    elif case == "future":
        value["version"] = 3
    elif case == "missing":
        del pairing["events"]
    elif case == "node-ref":
        pairing["events"][0]["node"] = "absent"
    elif case == "line":
        pairing["events"][0]["line"] = True
    elif case == "kind":
        pairing["events"][0]["kind"] = []
    elif case == "output-kind":
        pairing["events"][0]["output_kind"] = "function_call_output"
    elif case == "location":
        pairing["locations"][0][2] = value["offset"] + 1
    elif case == "lost-location":
        pairing["locations"] = []
    elif case == "duplicate-location":
        pairing["locations"] *= 2
    elif case == "derived-issue":
        pairing["issues"] = [{"code": "RD201", "line": 1, "detail": "bad", "call_id": "c", "block": 0}]
    elif case == "unknown-field":
        pairing["payload"] = "not permitted"
    elif case == "format":
        pairing["format"] = "empty"
    elif case == "terminated":
        pairing["terminated"] = 1
    elif case == "lines":
        pairing["lines"] = 0
    elif case == "node-line":
        pairing["nodes"]["a"]["line"] = 2
    elif case == "node-scope":
        pairing["nodes"]["a"]["sidechain"] = "false"


@pytest.mark.parametrize("case", ["v1", "future", "missing", "node-ref", "line", "kind",
                                  "output-kind", "location", "lost-location", "duplicate-location",
                                  "derived-issue", "unknown-field", "format", "terminated", "lines",
                                  "node-line", "node-scope"])
def test_invalid_or_old_checkpoint_refused_without_writes(tmp_path, case):
    path, checkpoint = tmp_path / "s.jsonl", tmp_path / "cursor.json"
    path.write_bytes(raw(cnode("assistant", "a", blocks=[use("c")])))
    audit_module.audit(path, checkpoint)
    state = json.loads(checkpoint.read_bytes())
    corrupt(state, case)
    checkpoint.write_text(json.dumps(state))
    before = checkpoint.read_bytes()
    source = path.read_bytes()
    with pytest.raises(engine.DoctorError, match="invalid audit checkpoint; use a new checkpoint path"):
        audit_module.audit(path, checkpoint)
    assert checkpoint.read_bytes() == before and path.read_bytes() == source


def test_unrelated_payloads_and_record_locations_are_not_cached(tmp_path):
    path, checkpoint = tmp_path / "s.jsonl", tmp_path / "cursor.json"
    path.write_bytes(raw(item("function_call", "c", arguments="PRIVATE-ARGUMENTS")) +
                     raw(*[item("message", content="PRIVATE-MESSAGE" * 100) for _ in range(100)]))
    audit_module.audit(path, checkpoint)
    saved = checkpoint.read_text()
    state = json.loads(saved)
    assert "PRIVATE-" not in saved
    assert state["pairing"]["lines"] == 101
    assert len(state["pairing"]["locations"]) == 1
    assert len(state["pairing"]["events"]) == 1
    assert state["pairing"]["nodes"] == {}


@pytest.mark.parametrize("path", sorted((Path(__file__).parent / "fixtures").glob("*/RD*.jsonl")),
                         ids=lambda p: p.parent.name + "/" + p.stem)
def test_issue_fixtures_match_full_replay_across_each_record(tmp_path, path):
    data = path.read_bytes()
    full = engine.scan(BytesIO(data), str(path))
    session, checkpoint = tmp_path / "s.jsonl", tmp_path / "cursor.json"
    prefix = b""
    for chunk in data.splitlines(keepends=True):
        with session.open("ab") as stream:
            stream.write(chunk)
        prefix += chunk
        full = engine.scan(BytesIO(prefix), str(session), full.format)
        if any(i.code in ("RD001", "RD002", "RD003") for i in full.issues):
            before = checkpoint.read_bytes() if checkpoint.exists() else None
            with pytest.raises(engine.DoctorError, match="invalid"):
                audit_module.audit(session, checkpoint, full.format)
            assert (checkpoint.read_bytes() if checkpoint.exists() else None) == before
            break
        current = audit_module.audit(session, checkpoint, full.format)
        assert current["issue_count"] == len(full.issues)


@pytest.mark.parametrize("first,second", [
    (item("message"), cnode("user", "u")),
    (cnode("user", "u"), item("message"))])
def test_cross_run_mixed_format_refusal_preserves_checkpoint(tmp_path, first, second):
    path, checkpoint = tmp_path / "s.jsonl", tmp_path / "cursor.json"
    path.write_bytes(raw(first))
    audit_module.audit(path, checkpoint)
    before = checkpoint.read_bytes()
    with path.open("ab") as stream:
        stream.write(raw(second))
    with pytest.raises(engine.DoctorError, match="mixed or mismatched"):
        audit_module.audit(path, checkpoint)
    assert checkpoint.read_bytes() == before


def test_append_only_whitespace_to_unterminated_object(tmp_path):
    path, checkpoint = tmp_path / "s.jsonl", tmp_path / "cursor.json"
    path.write_bytes(raw(item("function_call", "c")).rstrip(b"\n"))
    audit_module.audit(path, checkpoint)
    with path.open("ab") as stream:
        stream.write(b" \t")
    report = audit_module.audit(path, checkpoint)
    assert report["issue_count"] == 1 and report["offset"] == path.stat().st_size
    assert not json.loads(checkpoint.read_text())["pairing"]["terminated"]
    with path.open("ab") as stream:
        stream.write(b"\n" + raw(item("function_call_output", "c")))
    assert audit_module.audit(path, checkpoint)["issue_count"] == 0
