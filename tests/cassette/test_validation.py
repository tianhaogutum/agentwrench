"""Versioned CLI diagnostics against synthetic v1 recording frames."""
import json
import sys

import pytest

from .conftest import cassette, request, response


def validate(cli, tmp_path, data):
    path = tmp_path / "session.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    result = cli("validate", path)
    assert result.stderr == b""
    return result.returncode, json.loads(result.stdout)


def test_valid_recording_report_is_versioned(cli, tmp_path):
    code, report = validate(cli, tmp_path, cassette(
        ("client", request(id=1)), ("server", response(1))))
    assert code == 0
    assert report == {"report_version": 1, "cassette_version": 1,
                      "valid": True, "event_count": 2, "diagnostics": []}


def test_mismatched_typed_ids_locate_both_frames(cli, tmp_path):
    code, report = validate(cli, tmp_path, cassette(
        ("client", request(id=1)), ("server", response("1"))))
    assert code == 1
    assert report["valid"] is False
    assert [(d["code"], d["location"]) for d in report["diagnostics"]] == [
        ("unmatched_response", {"path": "/events/1/message/id", "frame_index": 1}),
        ("unanswered_request", {"path": "/events/0/message/id", "frame_index": 0}),
    ]


def test_incomplete_recording_locates_unanswered_frame(cli, tmp_path):
    code, report = validate(cli, tmp_path, cassette(
        ("client", request()), incomplete=True))
    assert code == 1
    assert [(d["code"], d["location"]) for d in report["diagnostics"]] == [
        ("unanswered_request", {"path": "/events/0/message/id", "frame_index": 0}),
        ("incomplete_recording", {"path": "/incomplete"}),
    ]


def test_truncated_json_has_exact_parser_location(cli, tmp_path):
    path = tmp_path / "truncated.json"
    raw = '{"version": 1, "events": [\n'
    path.write_text(raw)
    result = cli("validate", path)
    assert result.returncode == 2
    assert result.stderr == b""
    report = json.loads(result.stdout)
    assert report["valid"] is False
    assert report["event_count"] is None
    assert report["diagnostics"][0]["code"] == "invalid_json"
    assert report["diagnostics"][0]["location"] == {
        "path": "", "line": 2, "column": 1, "char_offset": len(raw)}


def test_concurrent_bidirectional_ids_and_reuse_are_valid(cli, tmp_path):
    # Same ID in opposite directions, typed IDs, out-of-order replies, an error
    # response, and reuse after completion must not be misdiagnosed.
    code, report = validate(cli, tmp_path, cassette(
        ("client", request(id=7)), ("client", request(id="7")),
        ("server", request("roots/list", id=7)), ("client", response(7)),
        ("server", response("7")),
        ("server", {"jsonrpc": "2.0", "id": 7,
                    "error": {"code": -32603, "message": "synthetic error"}}),
        ("client", request(id=7)), ("server", response(7)),
        ("server", {"jsonrpc": "2.0", "method": "notifications/message"})))
    assert code == 0
    assert report["diagnostics"] == []
    assert report["event_count"] == 9


def test_duplicate_id_locates_original_without_overwriting_it(cli, tmp_path):
    code, report = validate(cli, tmp_path, cassette(
        ("server", request(id="same")), ("server", request(id="same")),
        ("client", response("same")), ("client", response("same"))))
    assert code == 1
    first, second = report["diagnostics"]
    assert first["code"] == "duplicate_request_id"
    assert first["location"] == {"path": "/events/1/message/id", "frame_index": 1}
    assert first["related"] == [{"path": "/events/0/message/id", "frame_index": 0}]
    assert second["code"] == "unmatched_response"
    assert second["location"] == {"path": "/events/3/message/id", "frame_index": 3}


def test_response_in_wrong_direction_cannot_complete_request(cli, tmp_path):
    code, report = validate(cli, tmp_path, cassette(
        ("server", request(id=9)), ("server", response(9))))
    assert code == 1
    assert [d["code"] for d in report["diagnostics"]] == ["unmatched_response", "unanswered_request"]


@pytest.mark.parametrize("field,value", [("seq", 999), ("ms", -1),
                                         ("direction", []), ("message", {})])
def test_bad_frame_reports_actual_array_index(cli, tmp_path, field, value):
    data = cassette(("client", request()), ("server", response()))
    data["events"][1][field] = value
    code, report = validate(cli, tmp_path, data)
    assert code == 1
    assert report["diagnostics"][0]["code"] == "invalid_schema"
    assert report["diagnostics"][0]["location"] == {
        "path": f"/events/1/{field}", "frame_index": 1}


@pytest.mark.parametrize("field,value,path,issue_code", [
    ("version", 2, "/version", "unsupported_version"),
    ("version", True, "/version", "unsupported_version"),
    ("incomplete", "false", "/incomplete", "invalid_schema"),
    ("redact_paths", ["/id"], "/redact_paths/0", "invalid_schema"),
    ("events", None, "/events", "invalid_schema"),
])
def test_schema_failures_have_versioned_reports(cli, tmp_path, field, value, path, issue_code):
    data = cassette()
    data[field] = value
    code, report = validate(cli, tmp_path, data)
    assert code == 1
    assert report["report_version"] == 1
    assert report["diagnostics"][0]["code"] == issue_code
    assert report["diagnostics"][0]["location"] == {"path": path}


def test_incomplete_flag_fails_even_with_no_pending_requests(cli, tmp_path):
    code, report = validate(cli, tmp_path, cassette(incomplete=True))
    assert code == 1
    assert [d["code"] for d in report["diagnostics"]] == ["incomplete_recording"]


@pytest.mark.parametrize("raw", [b'{"version":1,"version":1}', b'NaN', b'1e999', b'"\\ud800"'])
def test_non_interoperable_json_returns_report(cli, tmp_path, raw):
    path = tmp_path / "bad.json"
    path.write_bytes(raw)
    result = cli("validate", path)
    assert result.returncode == 2
    assert result.stderr == b""
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == "invalid_json"


def test_syntax_location_uses_characters_not_utf8_bytes(cli, tmp_path):
    raw = '{"text":"你好",\n"events":['
    path = tmp_path / "unicode.json"
    path.write_text(raw, encoding="utf-8")
    result = cli("validate", path)
    assert result.returncode == 2
    location = json.loads(result.stdout)["diagnostics"][0]["location"]
    assert location == {"path": "", "line": 2, "column": 11, "char_offset": len(raw)}


def test_invalid_encoding_has_byte_location(cli, tmp_path):
    path = tmp_path / "utf8.json"
    path.write_bytes(b'{"x":"\xff"}')
    result = cli("validate", path)
    assert result.returncode == 2
    assert json.loads(result.stdout)["diagnostics"][0]["location"] == {"path": "", "byte_offset": 6}


def test_read_error_has_machine_readable_report(cli, tmp_path):
    result = cli("validate", tmp_path / "absent.json")
    assert result.returncode == 2
    assert result.stderr == b""
    report = json.loads(result.stdout)
    assert report["event_count"] is None
    assert report["diagnostics"][0]["code"] == "read_error"


def test_validation_retains_file_message_and_event_limits(tmp_path, monkeypatch):
    import agentwrench.cassette.format as fmt
    from agentwrench.cassette.validation import validation_report

    path = tmp_path / "limits.json"
    path.write_text(json.dumps(cassette(("client", request()), ("server", response()))))
    with monkeypatch.context() as patch:
        patch.setattr(fmt, "MAX_CASSETTE", 32)
        report, code = validation_report(path)
        assert code == 2
        assert report["diagnostics"][0]["code"] == "cassette_too_large"
    with monkeypatch.context() as patch:
        patch.setattr(fmt, "MAX_EVENTS", 1)
        report, code = validation_report(path)
        assert code == 1
        assert report["diagnostics"][0]["location"] == {"path": "/events"}
    with monkeypatch.context() as patch:
        patch.setattr(fmt, "MAX_LINE", 16)
        report, code = validation_report(path)
        assert code == 1
        assert report["diagnostics"][0]["location"] == {"path": "/events/0/message", "frame_index": 0}


def test_real_failed_recording_validates_with_request_location(cli, tmp_path):
    path = tmp_path / "crashed.json"
    recorded = cli("record", "--cassette", path, "--", sys.executable, "-c",
                   "import sys; sys.stdin.readline(); sys.exit(3)", messages=[request()])
    assert recorded.returncode == 1
    assert json.loads(path.read_text())["incomplete"] is True
    result = cli("validate", path)
    assert result.returncode == 1
    issues = json.loads(result.stdout)["diagnostics"]
    assert [(d["code"], d["location"]) for d in issues] == [
        ("unanswered_request", {"path": "/events/0/message/id", "frame_index": 0}),
        ("incomplete_recording", {"path": "/incomplete"}),
    ]
