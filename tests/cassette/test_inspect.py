"""Read-only frame inspection: exact identities, typed lifetimes and failures."""
import copy
import json

import pytest

from .conftest import cassette, request, response


def frame(data, index):
    return {"frame_index": index, "path": f"/events/{index}", "event": data["events"][index]}


def inspect(cli, tmp_path, data, index):
    path = tmp_path / "session.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    original = path.read_bytes()
    result = cli("inspect", path, "--frame", index, raw=b"not replay input\n")
    assert path.read_bytes() == original
    assert result.stderr == b""
    return result.returncode, json.loads(result.stdout)


def test_exact_frame_report_contract(cli, tmp_path):
    data = cassette(("client", request("ping", id=7)), ("server", response(7)))
    code, report = inspect(cli, tmp_path, data, 0)
    assert code == 0
    assert report == {"report_version": 1, "cassette_version": 1, "valid": True,
                      "event_count": 2, "diagnostics": [],
                      "frame": frame(data, 0), "peer": frame(data, 1)}


@pytest.mark.parametrize("index,peer", [(0, 5), (1, 4), (2, 3), (3, 2), (4, 1), (5, 0),
                                      (6, 9), (7, 8), (8, 7), (9, 6), (10, 11), (11, 10),
                                      (12, None)])
def test_peer_is_exact_typed_directional_lifetime(cli, tmp_path, index, peer):
    data = cassette(
        ("client", request(params={"tag": "first integer"}, id=7)),
        ("client", request(params={"tag": "first string"}, id="7")),
        ("server", request("roots/list", id=7)), ("client", response(7, {"roots": []})),
        ("server", response("7", {"tag": "first string response"})),
        ("server", {"jsonrpc": "2.0", "id": 7,
                    "error": {"code": -32603, "message": "first integer error"}}),
        ("client", request(params={"tag": "reused integer"}, id=7)),
        ("client", request(params={"tag": "reused string"}, id="7")),
        ("server", response("7", {"tag": "second string response"})),
        ("server", response(7, {"tag": "second integer response"})),
        ("server", request("roots/list", id=7)), ("client", response(7, {"roots": ["second"]})),
        ("server", {"jsonrpc": "2.0", "method": "notifications/message"}))
    # Preserve original event fields and already-redacted payloads verbatim.
    data["events"][index]["extra"] = {"stored": "你好 <REDACTED>"}
    code, report = inspect(cli, tmp_path, data, index)
    assert code == 0
    assert report["diagnostics"] == []
    assert report["frame"] == frame(data, index)
    assert report["peer"] == (frame(data, peer) if peer is not None else None)


@pytest.mark.parametrize("index,peer", [(0, 2), (1, None), (2, 0), (3, None), (4, 5), (5, 4)])
def test_diagnostic_pairing_keeps_original_duplicate_and_allows_later_reuse(cli, tmp_path, index, peer):
    data = cassette(("client", request(id="x")), ("client", request(id="x")),
                    ("server", response("x", {"which": "original"})), ("server", response("x")),
                    ("client", request(id="x")), ("server", response("x", {"which": "reuse"})))
    code, report = inspect(cli, tmp_path, data, index)
    assert code == 1
    assert report["valid"] is False
    assert report["frame"] == frame(data, index)
    assert report["peer"] == (frame(data, peer) if peer is not None else None)
    assert [d["code"] for d in report["diagnostics"]] == ["duplicate_request_id", "unmatched_response"]
    assert report["diagnostics"][0]["related"] == [{"path": "/events/0/message/id", "frame_index": 0}]


@pytest.mark.parametrize("index", [0, 1])
def test_unmatched_typed_response_is_not_a_peer(cli, tmp_path, index):
    data = cassette(("client", request(id=1)), ("server", response("1")), incomplete=True)
    code, report = inspect(cli, tmp_path, data, index)
    assert code == 1
    assert report["frame"] == frame(data, index)
    assert report["peer"] is None
    assert [d["code"] for d in report["diagnostics"]] == [
        "unmatched_response", "unanswered_request", "incomplete_recording"]


@pytest.mark.parametrize("index", ["-1", "2", "99999999999999999999999999", "1.0", "abc", ""])
def test_invalid_frame_index_is_structured(cli, tmp_path, index):
    data = cassette(("client", request()), ("server", response()))
    code, report = inspect(cli, tmp_path, data, index)
    assert code == 2
    assert report == {"report_version": 1, "cassette_version": 1, "valid": False,
                      "event_count": 2, "frame": None, "peer": None, "diagnostics": [{
                          "code": "invalid_frame_index",
                          "message": "frame index must be a nonnegative decimal integer less than event_count",
                          "location": {"path": "/events"}}]}


def test_empty_cassette_has_no_frame_zero(cli, tmp_path):
    code, report = inspect(cli, tmp_path, cassette(), 0)
    assert code == 2
    assert report["diagnostics"][0]["code"] == "invalid_frame_index"


@pytest.mark.parametrize("selector,code", [("9" * 5000, 2), ("0" * 5000 + "1", 0)])
def test_large_selector_is_bounded_before_integer_conversion(cli, tmp_path, selector, code):
    data = cassette(("client", request()), ("server", response()))
    actual, report = inspect(cli, tmp_path, data, selector)
    assert actual == code
    if code == 0:
        assert report["frame"] == frame(data, 1)
        assert report["peer"] == frame(data, 0)
    else:
        assert report["diagnostics"][0]["code"] == "invalid_frame_index"


def test_incomplete_recording_retains_completed_peer_and_notification_has_none(cli, tmp_path):
    data = cassette(("client", request()), ("server", response()),
                    ("client", {"jsonrpc": "2.0", "method": "notifications/cancelled",
                                "params": {"requestId": 1}}), incomplete=True)
    code, report = inspect(cli, tmp_path, data, 0)
    assert code == 1
    assert report["frame"] == frame(data, 0) and report["peer"] == frame(data, 1)
    assert [d["code"] for d in report["diagnostics"]] == ["incomplete_recording"]
    code, report = inspect(cli, tmp_path, data, 2)
    assert code == 1 and report["frame"] == frame(data, 2) and report["peer"] is None


def test_same_direction_response_is_not_a_peer(cli, tmp_path):
    data = cassette(("server", request("roots/list", id=9)), ("server", response(9)))
    code, report = inspect(cli, tmp_path, data, 1)
    assert code == 1 and report["frame"] == frame(data, 1) and report["peer"] is None
    assert [d["code"] for d in report["diagnostics"]] == ["unmatched_response", "unanswered_request"]


@pytest.mark.parametrize("limit,value,code,issue", [
    ("MAX_CASSETTE", 32, 2, "cassette_too_large"),
    ("MAX_EVENTS", 1, 1, "invalid_schema"), ("MAX_LINE", 16, 1, "invalid_schema")])
def test_frame_inspection_retains_input_bounds(tmp_path, monkeypatch, limit, value, code, issue):
    import agentwrench.cassette.format as fmt
    from agentwrench.cassette.validation import validation_report

    path = tmp_path / "limits.json"
    path.write_text(json.dumps(cassette(("client", request()), ("server", response()))))
    monkeypatch.setattr(fmt, limit, value)
    report, actual = validation_report(path, frame="0")
    assert actual == code
    assert report["frame"] is None and report["peer"] is None
    assert report["diagnostics"][0]["code"] == issue


@pytest.mark.parametrize("raw,code,issue", [
    (None, 2, "read_error"), (b'{"events":[', 2, "invalid_json"),
    (b'{"version":2}', 1, "unsupported_version"),
    (json.dumps(cassette(("client", request()))).replace('"seq": 0', '"seq": 9').encode(),
     1, "invalid_schema"),
])
def test_unreadable_or_schema_invalid_input_never_returns_untrusted_frames(cli, tmp_path, raw, code, issue):
    path = tmp_path / "broken.json"
    if raw is not None:
        path.write_bytes(raw)
    result = cli("inspect", path, "--frame", 0)
    assert result.returncode == code
    assert result.stderr == b""
    report = json.loads(result.stdout)
    assert report["frame"] is None and report["peer"] is None
    assert report["diagnostics"][0]["code"] == issue


@pytest.mark.parametrize("index", ["0", "-1", "abc"])
def test_frame_inspection_never_starts_replay_or_processes(tmp_path, monkeypatch, capsys, index):
    import asyncio
    import subprocess
    from agentwrench.cassette import cli

    marker = tmp_path / "must-not-exist"
    command = f"touch {marker}"
    data = cassette(("client", request(command, {"command": command})), ("server", response()))
    data["metadata"] = {"command": command, "fault_scenario": {"kind": "eof"}}
    path = tmp_path / "untrusted.json"
    path.write_text(json.dumps(data))

    def forbidden(*args, **kwargs):
        pytest.fail("inspection entered an execution/replay path")

    for name in ("record", "Replayer", "replay_stdio", "stdin_reader", "load_cassette"):
        monkeypatch.setattr(cli, name, forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    before = copy.deepcopy(data)
    assert cli.main(["inspect", str(path), "--frame", index]) == (0 if index == "0" else 2)
    output = capsys.readouterr()
    assert output.err == ""
    assert json.loads(output.out)["report_version"] == 1
    assert json.loads(path.read_text()) == before
    assert not marker.exists()


def test_counts_only_inspect_contract_is_unchanged(cli, tmp_path):
    data = cassette(("client", request("ping")), ("server", response()))
    path = tmp_path / "counts.json"
    path.write_text(json.dumps(data))
    result = cli("inspect", path)
    assert result.returncode == 0 and result.stderr == b""
    assert json.loads(result.stdout) == {"version": 1, "incomplete": False, "events": 2,
                                        "methods": {"ping": 1}, "tools": {}, "server_requests": 0}
