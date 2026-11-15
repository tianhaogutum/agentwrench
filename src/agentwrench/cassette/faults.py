"""Explicit, bounded, single-request transport fault scenarios (separate from cassettes)."""
from __future__ import annotations

from pathlib import Path

from .format import CassetteError, loads

MAX_SCENARIO_BYTES = 4096
MAX_TIMEOUT_MS = 60_000


def load_fault_scenario(path):
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_SCENARIO_BYTES + 1)
    except OSError as exc:
        raise CassetteError(f"cannot read fault scenario: {exc.strerror}") from exc
    if len(raw) > MAX_SCENARIO_BYTES:
        raise CassetteError("fault scenario exceeds 4096 bytes")
    return loads(raw)


def validate_fault_scenario(data, cassette):
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
        raise CassetteError("fault scenario version must be 1")
    kind = data.get("kind")
    if not isinstance(kind, str) or kind not in {"eof", "timeout"}:
        raise CassetteError("fault scenario kind must be eof or timeout")
    fields = {"version", "request_seq", "kind"}
    if kind == "timeout":
        fields.add("duration_ms")
        duration = data.get("duration_ms")
        if type(duration) is not int or not 1 <= duration <= MAX_TIMEOUT_MS:
            raise CassetteError("fault scenario duration_ms must be an integer from 1 to 60000")
    if data.keys() != fields:
        raise CassetteError("fault scenario has missing or unknown fields")
    seq = data["request_seq"]
    events = cassette["events"]
    if type(seq) is not int or not 0 <= seq < len(events):
        raise CassetteError("fault scenario request_seq must identify a recorded client request")
    event = events[seq]
    if event["direction"] != "client" or not {"method", "id"} <= event["message"].keys():
        raise CassetteError("fault scenario request_seq must identify a recorded client request")
    return dict(data)
