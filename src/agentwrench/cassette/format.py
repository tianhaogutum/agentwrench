"""Versioned, bounded JSON format, JSON-RPC validation and structural redaction."""

from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import re
import tempfile

FORMAT_VERSION = 1
MAX_LINE = 1024 * 1024
MAX_CASSETTE = 64 * 1024 * 1024
MAX_EVENTS = 100_000
MAX_MESSAGE_DEPTH = 64
NESTING_DIAGNOSTIC = f"message container nesting exceeds {MAX_MESSAGE_DEPTH} levels"
MAX_INTEGER_DIGITS = 4096
INTEGER_DIAGNOSTIC = f"integer exceeds supported {MAX_INTEGER_DIGITS} decimal digits"
FINITE_DIAGNOSTIC = "non-finite JSON numbers are not supported"
_INTEGER_LIMIT = 10 ** MAX_INTEGER_DIGITS
_INTEGER_MIN = -_INTEGER_LIMIT
REDACTED = "<REDACTED>"


class CassetteError(ValueError):
    """Invalid input or unsupported cassette content."""

    def __init__(self, message, *, code="invalid_input", location=None):
        super().__init__(message)
        self.code = code
        self.location = location if location is not None else {"path": ""}


def _reject_constant(value):
    raise CassetteError(FINITE_DIAGNOSTIC)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CassetteError("duplicate JSON object key")
        result[key] = value
    return result


def _parse_integer(text):
    # JSON's decoder supplies an ASCII integer token, including any minus sign.
    # Check before decimal conversion, independently of Python's global limit.
    if len(text) - text.startswith("-") > MAX_INTEGER_DIGITS:
        raise CassetteError(INTEGER_DIAGNOSTIC, code="integer_too_large")
    return int(text)


def _check_integer(value):
    # Compare signed magnitudes without decimal formatting or an abs() copy of
    # an arbitrarily large negative integer. Booleans remain ordinary scalars.
    if isinstance(value, int) and (value >= _INTEGER_LIMIT or value <= _INTEGER_MIN):
        raise CassetteError(INTEGER_DIAGNOSTIC, code="integer_too_large")


def loads(data: str | bytes):
    try:
        value = json.loads(data, parse_int=_parse_integer, parse_constant=_reject_constant,
                           object_pairs_hook=_unique_object)
        # Reject overflowing floats (1e999) and lone Unicode surrogates too.
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return value
    except json.JSONDecodeError as exc:
        raise CassetteError("invalid or truncated JSON", code="invalid_json", location={
            "path": "", "line": exc.lineno, "column": exc.colno, "char_offset": exc.pos}) from exc
    except UnicodeDecodeError as exc:
        raise CassetteError("invalid JSON encoding", code="invalid_json",
                            location={"path": "", "byte_offset": exc.start}) from exc
    except RecursionError as exc:
        raise CassetteError(NESTING_DIAGNOSTIC, code="invalid_json") from exc
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, CassetteError) and exc.code == "integer_too_large":
            raise
        raise CassetteError("invalid JSON (UTF-8, finite numbers and unique keys required)",
                            code="invalid_json") from exc


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def id_key(value):
    if type(value) not in (int, str):
        raise CassetteError("MCP ids must be strings or integers (not null/bool/float)")
    _check_integer(value)
    return (type(value).__name__, value)


def check_message_limits(msg):
    """Check scalar numbers and dict/list depth before copying/serialization.

    Iterator frames keep auxiliary storage proportional to depth, not width.
    Even cyclic direct-Python input reaches the bound without recursive copying.
    """
    stack = [iter((msg,))]
    while stack:
        try:
            value = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        _check_integer(value)
        if isinstance(value, float) and not math.isfinite(value):
            raise CassetteError(FINITE_DIAGNOSTIC, code="nonfinite_number")
        if isinstance(value, (dict, list)):
            if len(stack) > MAX_MESSAGE_DEPTH:
                raise CassetteError(NESTING_DIAGNOSTIC)
            stack.append(iter(value.values() if isinstance(value, dict) else value))


def validate_message(msg):
    check_message_limits(msg)
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        raise CassetteError("expected one JSON-RPC 2.0 object; batches are unsupported")
    if "id" in msg:
        id_key(msg["id"])
    if "method" in msg:
        if not isinstance(msg["method"], str) or not msg["method"]:
            raise CassetteError("method must be a non-empty string")
        if "result" in msg or "error" in msg:
            raise CassetteError("request/notification cannot contain result or error")
        if "params" in msg and not isinstance(msg["params"], dict):
            raise CassetteError("MCP params must be an object")
        if "_meta" in msg.get("params", {}) and not isinstance(msg["params"]["_meta"], dict):
            raise CassetteError("MCP params._meta must be an object")
    else:
        if "id" not in msg or ("result" in msg) == ("error" in msg):
            raise CassetteError("response requires id and exactly one result/error")
        if "params" in msg:
            raise CassetteError("response cannot contain params")
        if "error" in msg:
            err = msg["error"]
            if (not isinstance(err, dict) or type(err.get("code")) is not int
                    or not isinstance(err.get("message"), str)):
                raise CassetteError("error requires integer code and string message")
    return msg


def parse_line(line: bytes):
    if len(line) > MAX_LINE:
        raise CassetteError("JSON-RPC line exceeds 1 MiB")
    if not line.endswith(b"\n"):
        raise CassetteError("stdio message must end with a newline")
    return validate_message(loads(line))


def pointer_tokens(pointer: str) -> list[str]:
    # Routing fields must survive redaction for safe response pairing.
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise CassetteError("redaction requires a non-root JSON Pointer")
    if re.search(r"~(?![01])", pointer):
        raise CassetteError("invalid JSON Pointer escape")
    parts = [s.replace("~1", "/").replace("~0", "~") for s in pointer[1:].split("/")]
    if parts[0] == "params" and (len(parts) == 1 or parts == ["params", "_meta"]):
        raise CassetteError("redact fields inside /params, not the params object")
    if parts[0] not in {"params", "result", "error"}:
        raise CassetteError("redaction is limited to /params, /result or /error data")
    if parts[0] == "error" and (len(parts) < 2 or parts[1] not in {"data", "message"}):
        raise CassetteError("only /error/data and /error/message may be redacted")
    return parts


def _array_index(token: str, length: int) -> int | None:
    if not length or not token:
        return None
    last = str(length - 1)
    # Bound the token before scanning or converting it. A real list index has
    # at most as many decimal digits as its last position, even if Python's
    # integer-string conversion limit is disabled.
    if len(token) > len(last):
        return None
    if not token.isascii() or not token.isdecimal() or (len(token) > 1 and token[0] == "0"):
        return None
    if len(token) == len(last) and token > last:
        return None
    return int(token)


def redact(msg, pointers: list[str]):
    check_message_limits(msg)
    result = copy.deepcopy(msg)
    for pointer in pointers:
        parts = pointer_tokens(pointer)
        parent = result
        for part in parts[:-1]:
            if isinstance(parent, dict) and part in parent:
                parent = parent[part]
            elif isinstance(parent, list):
                index = _array_index(part, len(parent))
                if index is None:
                    break
                parent = parent[index]
            else:
                break
        else:
            leaf = parts[-1]
            if isinstance(parent, dict) and leaf in parent:
                parent[leaf] = REDACTED
            elif isinstance(parent, list):
                index = _array_index(leaf, len(parent))
                if index is not None:
                    parent[index] = REDACTED
    return result


def validation_issue(code, message, path="", frame_index=None, *, related=None):
    location = {"path": path}
    if frame_index is not None:
        location["frame_index"] = frame_index
    issue = {"code": code, "message": message, "location": location}
    if related is not None:
        issue["related"] = related
    return issue


def cassette_diagnostics(data, *, require_complete=False, pairs=None):
    """Yield shared schema/integrity checks, stopping at the first schema error.

    Pairing errors do not stop the scan. Keep the first in-flight request on a
    duplicate ID; a later response consumes it, never silently overwrites it.
    Locations use array indices independently of the untrusted stored seq.
    If supplied, pairs receives matched request/response indices in both
    directions, using the same pending-ID lifetimes as the diagnostics.
    """
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != FORMAT_VERSION:
        yield validation_issue("unsupported_version", "unsupported or missing cassette version (expected 1)", "/version")
        return
    if type(data.get("incomplete")) is not bool:
        yield validation_issue("invalid_schema", "incomplete must be a boolean", "/incomplete")
        return
    if not isinstance(data.get("redact_paths"), list):
        yield validation_issue("invalid_schema", "redact_paths must be an array", "/redact_paths")
        return
    for index, pointer in enumerate(data["redact_paths"]):
        try:
            pointer_tokens(pointer)
        except CassetteError as exc:
            yield validation_issue("invalid_schema", str(exc), f"/redact_paths/{index}")
            return
    events = data.get("events")
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        yield validation_issue("invalid_schema", "events must be an array of at most 100000 items", "/events")
        return
    pending = {"client": {}, "server": {}}
    previous_ms = 0
    for seq, event in enumerate(events):
        path = f"/events/{seq}"
        if not isinstance(event, dict) or type(event.get("seq")) is not int or event["seq"] != seq:
            yield validation_issue("invalid_schema", "event seq must be contiguous starting at 0", path + "/seq", seq)
            return
        ms = event.get("ms")
        if type(ms) not in (int, float) or (isinstance(ms, float) and not math.isfinite(ms)) or ms < previous_ms:
            yield validation_issue("invalid_schema", "event ms must be finite, nonnegative and monotonic", path + "/ms", seq)
            return
        previous_ms = ms
        direction = event.get("direction")
        if not isinstance(direction, str) or direction not in pending:
            yield validation_issue("invalid_schema", "event direction must be client or server", path + "/direction", seq)
            return
        try:
            msg = validate_message(event.get("message"))
        except CassetteError as exc:
            yield validation_issue("invalid_schema", str(exc), path + "/message", seq)
            return
        if len(canonical(msg).encode("utf-8")) + 1 > MAX_LINE:
            yield validation_issue("invalid_schema", "event message exceeds 1 MiB", path + "/message", seq)
            return
        if "id" not in msg:
            continue
        key = id_key(msg["id"])
        if "method" in msg:
            if key in pending[direction]:
                original = pending[direction][key]
                yield validation_issue("duplicate_request_id", "duplicate in-flight request id",
                    path + "/message/id", seq,
                    related=[{"path": f"/events/{original}/message/id", "frame_index": original}])
                continue
            pending[direction][key] = seq
        else:
            opposite = "server" if direction == "client" else "client"
            if key not in pending[opposite]:
                yield validation_issue("unmatched_response", "response has no matching in-flight request",
                                       path + "/message/id", seq)
            else:
                original = pending[opposite].pop(key)
                if pairs is not None:
                    pairs[original] = seq
                    pairs[seq] = original
    if require_complete or not data["incomplete"]:
        for seq in sorted(seq for requests in pending.values() for seq in requests.values()):
            yield validation_issue("unanswered_request", "unanswered request at end of recording",
                                   f"/events/{seq}/message/id", seq)
    if require_complete and data["incomplete"]:
        yield validation_issue("incomplete_recording", "recording is marked incomplete", "/incomplete")


def validate_cassette(data):
    issue = next(cassette_diagnostics(data), None)
    if issue is not None:
        raise CassetteError(issue["message"], code=issue["code"], location=issue["location"])
    return data


def read_cassette(path: str | Path):
    """Read bounded JSON without schema checks, for validation and inspection."""
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_CASSETTE + 1)
        if len(raw) > MAX_CASSETTE:
            raise CassetteError("cassette exceeds 64 MiB", code="cassette_too_large")
        return loads(raw)
    except OSError as exc:
        raise CassetteError(f"cannot read cassette: {exc.strerror}", code="read_error") from exc


def load_cassette(path: str | Path):
    return validate_cassette(read_cassette(path))


def cassette_bytes(data):
    """The exact on-disk UTF-8 representation, including its final LF."""
    return (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def save_cassette(path: str | Path, data):
    """Atomic private file; never follows a final destination symlink."""
    path = Path(path)
    raw = cassette_bytes(data)
    if len(raw) > MAX_CASSETTE:
        raise CassetteError("cassette exceeds 64 MiB")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".mcp-cassette-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
