"""Progress binding contracts for the existing deterministic transcript replay."""
import copy

import pytest

from agentwrench.cassette.replay import Replayer
from .conftest import cassette, request, response


def call(token, *, id=1):
    return request(params={"name": "work", "arguments": {"text": "hello"},
                           "_meta": {"progressToken": token, "trace": "keep"}}, id=id)


def progress(token, value=1, **extra):
    return {"jsonrpc": "2.0", "method": "notifications/progress",
            "params": {"progressToken": token, "progress": value, **extra}}


@pytest.mark.parametrize("match", ["params", "sequence"])
@pytest.mark.parametrize("recorded,live", [("recorded-token", 42), (7, "live-token")])
def test_progress_binding_changes_only_token_and_response_id(match, recorded, live):
    notes = [progress(recorded, 0.5, total=2.5, message="starting"),
             progress(recorded, 2.5, total=2.5, message="done")]
    result = {"content": [{"type": "text", "text": "hello"}], "isError": False}
    data = cassette(("client", call(recorded)), *(("server", n) for n in notes),
                    ("server", response(result=result)))
    original = copy.deepcopy(data)
    incoming = call(live, id="live-id")
    incoming_before = copy.deepcopy(incoming)
    player = Replayer(data, strict=True, match=match)
    expected = [progress(live, 0.5, total=2.5, message="starting"),
                progress(live, 2.5, total=2.5, message="done"), response("live-id", result)]
    assert player.handle(incoming) == expected
    assert player.finish() == 0
    assert incoming == incoming_before and data == original


@pytest.mark.parametrize("match", ["params", "sequence"])
@pytest.mark.parametrize("change", ["arguments", "metadata", "missing-live-token", "missing-recorded-token"])
def test_real_differences_still_fail_and_do_not_consume(match, change):
    recorded, incoming = call("old"), call(9, id=99)
    if change == "arguments":
        incoming["params"]["arguments"]["text"] = "different"
    elif change == "metadata":
        incoming["params"]["_meta"]["trace"] = "different"
    elif change == "missing-live-token":
        del incoming["params"]["_meta"]["progressToken"]
    else:
        del recorded["params"]["_meta"]["progressToken"]
    player = Replayer(cassette(("client", recorded), ("server", response())), strict=True, match=match)
    error = player.handle(incoming)[0]["error"]
    assert error["code"] == -32001
    assert "progressToken presence must match" in error["message"]
    assert len(player.unused) == 1
    assert player.handle({**recorded, "id": 99}) == [response(99)]
    assert player.finish() == 1  # mismatch remains visible after successful retry


@pytest.mark.parametrize("params", [None, {}, {"_meta": {}}, {"_meta": {"trace": "keep"}}])
def test_tokenless_requests_keep_original_matching(params):
    msg = request(params=params)
    player = Replayer(cassette(("client", msg), ("server", response())), strict=True)
    assert player.handle({**msg, "id": "new"}) == [response("new")]
    assert player.finish() == 0


def test_empty_meta_is_not_removed_and_client_notifications_are_not_normalized():
    original = request(params={"_meta": {}})
    player = Replayer(cassette(("client", original), ("server", response())))
    assert "error" in player.handle(request())[0]
    note = {"jsonrpc": "2.0", "method": "notifications/custom", "params": {"_meta": {"progressToken": "old"}}}
    player = Replayer(cassette(("client", note)), strict=True)
    changed = copy.deepcopy(note)
    changed["params"]["_meta"]["progressToken"] = 8
    assert player.handle(changed) == []
    assert len(player.unused) == 1 and player.finish() == 1


@pytest.mark.parametrize("location", ["startup", "unknown-token", "after-response", "typed-mismatch", "tokenless-request"])
def test_orphan_progress_rejected(location):
    from agentwrench.cassette.format import CassetteError
    req = call("7")
    events = [("client", req), ("server", progress("7")), ("server", response())]
    if location == "startup":
        events = [("server", progress("7"))]
    elif location == "unknown-token":
        events[1] = ("server", progress("wrong"))
    elif location == "after-response":
        events[1], events[2] = events[2], events[1]
    elif location == "typed-mismatch":
        events[1] = ("server", progress(7))
    else:
        del req["params"]["_meta"]["progressToken"]
    with pytest.raises(CassetteError, match="orphan notifications/progress.*seq="):
        Replayer(cassette(*events))


@pytest.mark.parametrize("bad", [None, True, False, 1.0, [], {}])
@pytest.mark.parametrize("where", ["recorded", "live", "notification"])
def test_invalid_token_types_are_rejected(bad, where):
    from agentwrench.cassette.format import CassetteError
    data = cassette(("client", call("old")), ("server", progress("old")), ("server", response()))
    if where == "recorded":
        data["events"][0]["message"]["params"]["_meta"]["progressToken"] = bad
    elif where == "notification":
        data["events"][1]["message"]["params"]["progressToken"] = bad
    with pytest.raises(CassetteError, match="progressToken.*string or integer|string or integer progressToken"):
        if where == "live":
            Replayer(data).handle(call(bad))
        else:
            Replayer(data)


def test_missing_notification_token_and_ambiguous_active_tokens():
    from agentwrench.cassette.format import CassetteError
    note = progress("old")
    del note["params"]["progressToken"]
    with pytest.raises(CassetteError, match="requires.*progressToken"):
        Replayer(cassette(("client", call("old")), ("server", note), ("server", response())))
    with pytest.raises(CassetteError, match="ambiguous progressToken.*seq=1"):
        Replayer(cassette(("client", call("old")), ("client", call("old", id=2)),
                          ("server", response()), ("server", response(2))))


def test_completed_token_reuse_is_fifo_and_binding_is_local():
    data = cassette(("client", call("old")), ("server", progress("old", message="first")),
                    ("server", response(result={"n": 1})), ("client", call("old")),
                    ("server", progress("old", message="second")), ("server", response(result={"n": 2})))
    player = Replayer(data, strict=True)
    assert player.handle(call(0, id=8)) == [progress(0, message="first"), response(8, {"n": 1})]
    assert player.handle(call("new", id=9)) == [progress("new", message="second"), response(9, {"n": 2})]
    assert player.finish() == 0


def test_typed_ownership_preserves_existing_overlapping_recordings():
    data = cassette(("client", call(7)), ("client", call("7", id=2)),
                    ("server", progress("7", message="string")), ("server", progress(7, message="integer")),
                    ("server", response(2)), ("server", response()))
    player = Replayer(data, strict=True)
    assert player.handle(call("first", id=8)) == [progress("first", message="integer"), response(8)]
    assert player.handle(call(22, id=9)) == [progress(22, message="string"), response(9)]
    assert player.finish() == 0  # per-interaction order only; no concurrency scheduling


def test_only_progress_token_is_rewritten_even_for_error_results():
    log = {"jsonrpc": "2.0", "method": "notifications/message", "params": {"progressToken": "old", "data": "keep"}}
    error = {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "old", "data": {"progressToken": "old"}}}
    player = Replayer(cassette(("client", call("old")), ("server", log),
                               ("server", progress("old")), ("server", error)))
    assert player.handle(call(4, id=9)) == [log, progress(4), {**error, "id": 9}]


@pytest.mark.parametrize("method,task", [("tools/call", {"ttl": 500}), ("tasks/get", None)])
def test_tasks_keep_exact_tokens_and_no_post_response_lifetime(method, task):
    from agentwrench.cassette.format import CassetteError
    recorded, incoming = call("old"), call(9)
    for msg in (recorded, incoming):
        msg["method"] = method
        if task is not None:
            msg["params"]["task"] = task
    player = Replayer(cassette(("client", recorded), ("server", response())), strict=True)
    assert "error" in player.handle(incoming)[0]
    assert player.handle(recorded) == [response()]
    with pytest.raises(CassetteError, match="orphan notifications/progress"):
        Replayer(cassette(("client", recorded), ("server", response()), ("server", progress("old"))))


@pytest.mark.parametrize("recorded_token,live_token", [("old", None), (None, "new")])
def test_ping_cannot_hide_progress_presence_mismatch(recorded_token, live_token):
    def ping(token):
        return request("ping", {"_meta": {"progressToken": token}} if token is not None else {})
    player = Replayer(cassette(("client", ping(recorded_token)), ("server", response())), strict=True)
    assert "error" in player.handle(ping(live_token))[0]
    assert len(player.unused) == 1 and player.finish() == 1


def test_progress_binding_retains_redaction_and_fault_selection():
    from agentwrench.cassette.format import REDACTED
    recorded, incoming = call("old"), call(7)
    recorded["params"]["arguments"]["text"] = REDACTED
    data = cassette(("client", recorded), ("server", progress("old")), ("server", response()),
                    paths=["/params/arguments/text"])
    player = Replayer(data, strict=True)
    assert player.handle(incoming) == [progress(7), response()]
    assert player.finish() == 0
    player = Replayer(data, strict=True, fault_scenario={"version": 1, "request_seq": 0, "kind": "eof"})
    assert player.handle(incoming) == []
    assert player.fault_triggered and player.finish() == 0


@pytest.mark.parametrize("match", ["params", "sequence"])
@pytest.mark.parametrize("recorded_token,live_token", [("recorded", 42), (7, "live")])
def test_stdio_record_then_replay_with_new_token(cli, tmp_path, match, recorded_token, live_token):
    import json
    from pathlib import Path
    import sys
    server = Path(__file__).parent / "fixtures" / "progress_server.py"
    path, report = tmp_path / "progress.json", tmp_path / "report.json"
    recorded = cli("record", "--cassette", path, "--report", report, "--", sys.executable, server,
                   messages=[call(recorded_token)])
    assert recorded.returncode == 0, recorded.stderr
    assert json.loads(report.read_text()) == {"exit_code": 0, "unmatched": 0, "unused": 0}
    original = path.read_bytes()
    expected = [json.loads(line) for line in recorded.stdout.splitlines()]
    assert expected[:2] == [progress(recorded_token, 0.5, total=2.5, message="starting"),
                           progress(recorded_token, 2.5, total=2.5, message="done")]
    assert expected[-1] == response(result={"content": [{"type": "text", "text": "hello"}], "isError": False})
    for note in expected[:2]:
        note["params"]["progressToken"] = live_token
    expected[-1]["id"] = "new-id"
    replayed = cli("replay", "--cassette", path, "--match", match, "--strict", "--report", report,
                   messages=[call(live_token, id="new-id")])
    assert replayed.returncode == 0, replayed.stderr
    assert [json.loads(line) for line in replayed.stdout.splitlines()] == expected
    assert json.loads(report.read_text()) == {"exit_code": 0, "unmatched": 0, "unused": 0}
    assert path.read_bytes() == original


@pytest.mark.parametrize("kind", ["orphan", "ambiguous", "invalid-live"])
def test_cli_progress_failures_are_clear_and_exit_two(cli, tmp_path, kind):
    import json
    data = cassette(("client", call("old")), ("server", response()))
    incoming = call(2)
    expected = b"progressToken"
    if kind == "orphan":
        data = cassette(("server", progress("old")))
        expected = b"orphan notifications/progress"
    elif kind == "ambiguous":
        data = cassette(("client", call("old")), ("client", call("old", id=2)),
                        ("server", response()), ("server", response(2)))
        expected = b"ambiguous progressToken"
    else:
        incoming = call(True)
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(data))
    result = cli("replay", "--cassette", path, messages=[incoming])
    assert result.returncode == 2
    assert result.stdout == b"" and expected in result.stderr and b"Traceback" not in result.stderr
