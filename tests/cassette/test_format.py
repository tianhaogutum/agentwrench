import copy
import json

import pytest

from agentwrench.cassette.format import (CassetteError, MAX_LINE, load_cassette, loads, parse_line,
                                 pointer_tokens, redact, save_cassette, validate_cassette)
from .conftest import cassette, request, response


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'NaN', b'1e999', b'"\\ud800"', b'\xff'])
def test_invalid_json(raw):
    with pytest.raises(CassetteError):
        loads(raw)


@pytest.mark.parametrize("message", [[], {"jsonrpc": "1.0"}, request(id=True), request(id=None),
    request(params=[]), {"jsonrpc": "2.0", "id": 1, "result": {}, "error": {}},
    {"jsonrpc": "2.0", "id": 1, "error": {"code": "bad", "message": "bad"}}])
def test_invalid_rpc(message):
    with pytest.raises(CassetteError):
        parse_line((json.dumps(message) + "\n").encode())


def test_framing_and_unicode():
    message = request(params={"text": "你好\nworld"})
    assert parse_line((json.dumps(message, ensure_ascii=False) + "\n").encode()) == message
    with pytest.raises(CassetteError, match="newline"):
        parse_line(b'{"jsonrpc":"2.0","method":"ping","id":1}')
    with pytest.raises(CassetteError, match="1 MiB"):
        parse_line(b" " * (MAX_LINE + 1))


def test_json_pointer_escaping_lists_and_non_mutation():
    message = request(params={"a/b": {"~key": ["secret", "public"]}, "missing": True})
    original = copy.deepcopy(message)
    got = redact(message, ["/params/a~1b/~0key/0", "/result/missing", "/params/nonexistent"])
    assert got["params"]["a/b"]["~key"] == ["<REDACTED>", "public"]
    assert message == original


@pytest.mark.parametrize("pointer", ["", "/", "/id", "/jsonrpc", "/method", "/params/a~2b", "/error", "/error/code", "/params"])
def test_unsafe_redaction(pointer):
    with pytest.raises(CassetteError):
        pointer_tokens(pointer)


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(version=2), lambda d: d.update(version=True), lambda d: d.update(incomplete="false"),
    lambda d: d["events"][0].update(seq=1), lambda d: d["events"][0].update(ms=-1),
    lambda d: d["events"][1].update(ms=-1), lambda d: d["events"][0].update(direction="up"),
    lambda d: d["events"][1]["message"].update(id=99),
])
def test_schema_rejects_corruption(mutate):
    data = cassette(("client", request()), ("server", response()))
    mutate(data)
    with pytest.raises(CassetteError):
        validate_cassette(data)


def test_pending_and_duplicate_ids():
    with pytest.raises(CassetteError, match="unanswered"):
        validate_cassette(cassette(("client", request())))
    assert validate_cassette(cassette(("client", request()), incomplete=True))["incomplete"]
    with pytest.raises(CassetteError, match="duplicate"):
        validate_cassette(cassette(("client", request()), ("client", request()), incomplete=True))


def test_save_private_and_atomic_symlink(tmp_path):
    data = cassette()
    destination = tmp_path / "c.json"
    outside = tmp_path / "untouched"
    outside.write_text("safe")
    destination.symlink_to(outside)
    save_cassette(destination, data)
    assert outside.read_text() == "safe"
    assert not destination.is_symlink()
    assert destination.stat().st_mode & 0o777 == 0o600
    assert load_cassette(destination) == data


@pytest.mark.parametrize("raw", [b"not-json", b'{"version":999}', b'[]'])
def test_malformed_cassette_cli_exit_two(cli, tmp_path, raw):
    path = tmp_path / "broken.json"
    path.write_bytes(raw)
    result = cli("inspect", path)
    assert result.returncode == 2
    assert result.stdout == b""
    assert b"Traceback" not in result.stderr


@pytest.mark.parametrize("direction", [[], {}, None, 5])
def test_invalid_direction_never_crashes_validator(direction):
    data = cassette(("client", request()), ("server", response()))
    data["events"][0]["direction"] = direction
    with pytest.raises(CassetteError):
        validate_cassette(data)


def test_bad_meta_rejected():
    with pytest.raises(CassetteError, match="_meta"):
        parse_line((json.dumps(request(params={"_meta": "bad"})) + "\n").encode())
