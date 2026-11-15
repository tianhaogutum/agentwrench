import copy
import json

import pytest

from agentwrench.cassette.format import CassetteError
from agentwrench.cassette.replay import Replayer
from .conftest import cassette, request, response


def test_canonical_fifo_ids_and_typed_ids():
    data = cassette(("client", request(params={"b": 2, "a": 1}, id=7)), ("server", response(7, {"n": 1})),
                    ("client", request(params={"a": 1, "b": 2}, id="7")), ("server", response("7", {"n": 2})))
    original = copy.deepcopy(data)
    player = Replayer(data, strict=True)
    assert player.handle(request(params={"a": 1, "b": 2}, id="live")) == [response("live", {"n": 1})]
    assert player.handle(request(params={"b": 2, "a": 1}, id=999)) == [response(999, {"n": 2})]
    assert player.finish() == 0
    assert data == original


def test_out_of_order_recorded_responses_and_id_reuse():
    data = cassette(("client", request("a", id=1)), ("client", request("b", id="1")),
                    ("server", response("1", {"method": "b"})), ("server", response(1, {"method": "a"})),
                    ("client", request("a", id=1)), ("server", response(1, {"second": True})))
    player = Replayer(data)
    assert player.handle(request("a", id="x")) == [response("x", {"method": "a"})]
    assert player.handle(request("a", id="y")) == [response("y", {"second": True})]
    assert player.handle(request("b", id=5)) == [response(5, {"method": "b"})]


def test_sequence_order_and_retry():
    data = cassette(("client", request("a")), ("server", response()),
                    ("client", request("b")), ("server", response()))
    player = Replayer(data, match="sequence", strict=True)
    assert "error" in player.handle(request("b"))[0]
    assert player.handle(request("a", id=10)) == [response(10)]
    assert player.handle(request("b", id=11)) == [response(11)]
    assert player.finish() == 1  # earlier mismatch remains visible


def test_strict_unmatched_and_unused_without_params_in_diagnostic():
    warnings = []
    player = Replayer(cassette(("client", request("expected")), ("server", response())), strict=True, warn=warnings.append)
    assert "error" in player.handle(request("unknown", {"password": "SECRET"}))[0]
    assert player.finish() == 1
    assert "unused interactions (1)" in warnings[-1]
    assert "SECRET" not in " ".join(warnings)


def test_initialize_identity_version_and_ping():
    init = request("initialize", {"protocolVersion": "2025-11-25", "clientInfo": {"name": "old"}})
    data = cassette(("client", init), ("server", response(result={"protocolVersion": "2025-11-25"})))
    warnings = []
    player = Replayer(data, strict=True, warn=warnings.append)
    assert "error" in player.handle(request("initialize", {"protocolVersion": "2024-11-05"}))[0]
    assert player.handle(request("initialize", {"protocolVersion": "2025-11-25", "clientInfo": {"name": "new"}}, id=42)) == [response(42, {"protocolVersion": "2025-11-25"})]
    assert player.handle(request("ping", id="p")) == [response("p")]
    player = Replayer(data, warn=warnings.append)
    assert "result" in player.handle(request("initialize", {"protocolVersion": "future"}))[0]
    assert "protocolVersion mismatch" in warnings[-1]


def test_recorded_ping_consumes_interaction():
    player = Replayer(cassette(("client", request("ping")), ("server", response())), strict=True)
    player.handle(request("ping", id=42))
    assert player.finish() == 0


def test_notifications_and_error_response():
    notification = {"jsonrpc": "2.0", "method": "notifications/message", "params": {"data": "hi"}}
    ready = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    err = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad args"}}
    player = Replayer(cassette(("client", ready), ("server", notification), ("client", request()),
                               ("server", notification), ("server", err)), strict=True)
    assert player.handle(ready) == [notification]
    assert player.handle(request(id=52)) == [notification, {**err, "id": 52}]
    assert player.finish() == 0


def test_notification_after_response_preserves_order():
    note = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
    player = Replayer(cassette(("client", request()), ("server", response()), ("server", note)))
    assert player.handle(request(id=9)) == [response(9), note]


def test_server_initiated_request_rejected_and_warned():
    warnings = []
    data = cassette(("server", request("roots/list", id="s")), ("client", response("s")))
    with pytest.raises(CassetteError, match="server-initiated"):
        Replayer(data, warn=warnings.append)
    assert "unsupported" in warnings[0]


def test_incomplete_rejected():
    with pytest.raises(CassetteError, match="incomplete"):
        Replayer(cassette(incomplete=True))


def test_redacted_request_is_matched_with_any_secret():
    player = Replayer(cassette(("client", request(params={"token": "<REDACTED>", "text": "hello"})),
                               ("server", response(result="<REDACTED>")), paths=["/params/token", "/result"]))
    assert player.handle(request(params={"text": "hello", "token": "new-secret"}, id=9)) == [response(9, "<REDACTED>")]


def test_cli_strict_report_and_diagnostics(cli, tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps(cassette(("client", request("a")), ("server", response()))))
    report = tmp_path / "exit.json"
    result = cli("replay", "--cassette", path, "--strict", "--report", report, messages=[request("wrong")])
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["code"] == -32001
    assert b"unmatched interactions" in result.stderr and b"unused interactions" in result.stderr
    assert json.loads(report.read_text()) == {"exit_code": 1, "unmatched": 1, "unused": 1}


def test_concurrent_progress_has_one_owner_and_is_not_duplicated():
    first = request("a", {"_meta": {"progressToken": "token-a"}}, id=1)
    second = request("b", {"_meta": {"progressToken": "token-b"}}, id=2)
    progress = {"jsonrpc": "2.0", "method": "notifications/progress",
                "params": {"progressToken": "token-a", "progress": 1, "total": 1}}
    log = {"jsonrpc": "2.0", "method": "notifications/message", "params": {"data": "untagged"}}
    player = Replayer(cassette(("client", first), ("client", second), ("server", progress),
                               ("server", log), ("server", response(2)), ("server", response(1))))
    assert player.handle({**second, "id": "live-b"}) == [log, response("live-b")]
    assert player.handle({**first, "id": "live-a"}) == [progress, response("live-a")]
    assert player.finish() == 0


def test_startup_and_unknown_notifications(cli, tmp_path):
    startup = {"jsonrpc": "2.0", "method": "notifications/message", "params": {"data": "startup"}}
    player = Replayer(cassette(("server", startup)), strict=True)
    assert player.startup == [startup]
    assert player.handle({"jsonrpc": "2.0", "method": "notifications/unknown"}) == []
    assert player.finish() == 1


def test_malformed_live_input_exits_two(cli, tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps(cassette()))
    result = cli("replay", "--cassette", path, raw=b"[]\n")
    assert result.returncode == 2
    assert b"Traceback" not in result.stderr


def test_replay_stdin_file_and_stdout_file(tmp_path):
    import subprocess
    import sys
    path = tmp_path / "c.json"
    path.write_text(json.dumps(cassette(("client", request("a")), ("server", response()))))
    incoming = tmp_path / "input.jsonl"
    incoming.write_text(json.dumps(request("a", id="file-id")) + "\n")
    output = tmp_path / "output.jsonl"
    with incoming.open("rb") as stdin, output.open("wb") as stdout:
        result = subprocess.run([sys.executable, "-m", "agentwrench", "cassette", "replay", "--cassette", str(path), "--strict"],
                                stdin=stdin, stdout=stdout, stderr=subprocess.PIPE, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text()) == response("file-id")


def test_diagnostics_escape_control_characters_and_bound_method_size():
    warnings = []
    player = Replayer(cassette(), strict=True, warn=warnings.append)
    result = player.handle(request("bad\n\x1b[31m" + "x" * 10000))
    assert len(json.dumps(result)) < 1000
    assert all("\n" not in warning and "\x1b" not in warning for warning in warnings)
    assert "\\n" in warnings[0]
