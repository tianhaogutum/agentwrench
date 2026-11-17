"""Run records (``relayproof-evidence/1``): one real command run, bound to a scope fingerprint.

Port of the original relayproof ``evidence.js``. ``run_evidence`` executes only
the explicit argv, without a shell, in its own process group; the record keeps
the true exit status and redacted, bounded output. Validation recomputes the
result from the termination fields and never trusts an imported verdict.
"""

from __future__ import annotations

import errno
import math
import os
import re
import selectors
import signal
import subprocess
import time
from datetime import UTC, datetime
from typing import Any

from ..cache.jscompat import utf16_length
from .pack_safety import (
    clipped,
    digest,
    is_finite_number,
    is_hash,
    is_integer,
    json_text,
    read_json,
    redact_command,
    require,
    write_new,
)
from .pack_scope import POLICY, snapshot

EVIDENCE_VERSION = "relayproof-evidence/1"
SOURCE = "relayproof-local-execution"
MAX_LOG_BYTES = 128 * 1024
FIELDS = ("schema", "policy", "source", "label", "command", "commandRedacted", "startedAt", "finishedAt",
          "durationMs", "before", "after", "exitCode", "signal", "timedOut", "spawnError", "stdout", "stderr",
          "stdoutTruncated", "stderrTruncated", "id")


def result_status(record: dict) -> str:
    if record["timedOut"]:
        return "timeout"
    if record["spawnError"] is not None:
        return "error"
    if record["signal"] is not None:
        return "signaled"
    return "passed" if record["exitCode"] == 0 else "failed"


def freshness(record: dict, current: dict) -> str:
    if record["before"] != record["after"]:
        return "mutated_during_run"
    return "matches_captured_scope" if record["after"] == current["fingerprint"] else "stale"


# ECMAScript Date Time String Format, the form Date.parse must accept.
_DATE = re.compile(r"([+-]\d{6}|\d{4})(?:-(\d{2})(?:-(\d{2}))?)?"
                   r"(?:T(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?(Z|[+-]\d{2}:\d{2})?)?")


def parses_as_date(value: Any) -> bool:
    """``Number.isFinite(Date.parse(value))`` for ISO 8601 strings (V8's legacy formats are not accepted)."""
    if not isinstance(value, str):
        return False
    match = _DATE.fullmatch(value)
    if not match:
        return False
    year = int(match.group(1))
    month, day = int(match.group(2) or 1), int(match.group(3) or 1)
    hour, minute, second = int(match.group(4) or 0), int(match.group(5) or 0), int(match.group(6) or 0)
    if not 1 <= month <= 12 or minute > 59 or second > 59 or hour > 24 or (hour == 24 and (minute or second)):
        return False
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    days = (31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)[month - 1]
    zone = match.group(8)
    if zone and zone != "Z" and (int(zone[1:3]) > 23 or int(zone[4:6]) > 59):
        return False
    return 1 <= day <= days and abs(year) <= 275760


def validate_evidence(record: Any) -> dict:
    require(isinstance(record, dict) and record.get("schema") == EVIDENCE_VERSION and record.get("policy") == POLICY,
            "Unsupported evidence format")
    require(len(record) == len(FIELDS) and all(key in FIELDS for key in record), "Unknown or missing evidence fields")
    require(is_hash(record["id"]) and is_hash(record["before"]) and is_hash(record["after"]), "Invalid evidence hashes")
    command = record["command"]
    require(isinstance(command, list) and command and all(isinstance(part, str) for part in command),
            "Invalid evidence command")
    code = record["exitCode"]
    require(code is None or (is_integer(code) and 0 <= code <= 255), "Invalid evidence exit code")
    require(isinstance(record["timedOut"], bool) and (record["signal"] is None or isinstance(record["signal"], str))
            and (record["spawnError"] is None or isinstance(record["spawnError"], str)), "Invalid evidence termination")
    require(code is not None or record["timedOut"] or record["signal"] is not None or record["spawnError"] is not None,
            "Missing execution result")
    require(isinstance(record["stdout"], str) and isinstance(record["stderr"], str)
            and isinstance(record["stdoutTruncated"], bool) and isinstance(record["stderrTruncated"], bool),
            "Invalid evidence logs")
    require(parses_as_date(record["startedAt"]) and parses_as_date(record["finishedAt"])
            and is_finite_number(record["durationMs"]) and record["durationMs"] >= 0
            and isinstance(record["label"], str), "Invalid evidence metadata")
    require(record["source"] == SOURCE and record["commandRedacted"] is True, "Invalid evidence source")
    payload = {key: value for key, value in record.items() if key != "id"}
    require(digest(payload) == record["id"], "Evidence integrity mismatch (not an authenticity signature)")
    return record


def load_evidence(filename: str) -> dict:
    return validate_evidence(read_json(filename, 1024 * 1024))


def sanitized_evidence(record: dict) -> dict:
    original = {key: value for key, value in validate_evidence(record).items() if key != "id"}
    payload = {
        **original,
        "label": clipped(original["label"], 1000), "command": redact_command(original["command"]),
        "stdout": clipped(original["stdout"], 64000), "stderr": clipped(original["stderr"], 64000),
        "stdoutTruncated": original["stdoutTruncated"] or utf16_length(original["stdout"]) > 64000,
        "stderrTruncated": original["stderrTruncated"] or utf16_length(original["stderr"]) > 64000,
    }
    return {**payload, "id": digest(payload)}


def iso_now() -> str:
    """``new Date().toISOString()``."""
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"SIG{number}"


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass  # Already exited.


def _execute(command: list[str], repo: str, timeout_ms: int) -> dict[str, Any]:
    """Spawn without a shell in a new process group; keep the first 128 KiB of each stream."""
    logs = {"stdout": bytearray(), "stderr": bytearray()}
    counts = {"stdout": 0, "stderr": 0}
    outcome: dict[str, Any] = {"exitCode": None, "signal": None, "timedOut": False, "spawnError": None,
                               "logs": logs, "counts": counts}
    start = time.perf_counter()
    try:
        child = subprocess.Popen(command, cwd=repo, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, start_new_session=True)
    except OSError as exc:
        outcome["spawnError"] = errno.errorcode.get(exc.errno or 0, "SPAWN_ERROR")
        return outcome
    except ValueError:
        outcome["spawnError"] = "SPAWN_ERROR"
        return outcome
    deadline = start + timeout_ms / 1000
    streams = {child.stdout: "stdout", child.stderr: "stderr"}
    try:
        with selectors.DefaultSelector() as selector:
            for stream, name in streams.items():
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map():
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    outcome["timedOut"] = True
                    break
                for key, _ in selector.select(remaining):
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    room = max(0, MAX_LOG_BYTES - counts[key.data])
                    if room:
                        logs[key.data] += data[:room]
                    counts[key.data] += len(data)
        if not outcome["timedOut"]:
            try:
                child.wait(timeout=max(0.0, deadline - time.perf_counter()))
            except subprocess.TimeoutExpired:
                outcome["timedOut"] = True
        if outcome["timedOut"]:
            _kill_group(child.pid)
    except BaseException:
        _kill_group(child.pid)  # interrupted: do not leave the command running
        raise
    finally:
        # A detached descendant can retain inherited pipes after the direct child exits.
        # Stop reading at the deadline rather than waiting indefinitely for them to close.
        for stream in streams:
            stream.close()
        returncode = child.wait()
    if returncode < 0:
        outcome["signal"] = _signal_name(-returncode)
    else:
        outcome["exitCode"] = returncode
    return outcome


def run_evidence(repo: str, command: list[str], output: str, *, timeout_ms: int = 30000,
                 label: str = "test command") -> dict:
    require(len(command) > 0 and all(isinstance(arg, str) and "\0" not in arg for arg in command),
            "Provide an executable after --")
    require(is_integer(timeout_ms) and 0 < timeout_ms <= 3600000, "Timeout must be 1..3600000 milliseconds")
    before = snapshot(repo)
    started_at = iso_now()
    start = time.perf_counter()
    result = _execute(command, repo, int(timeout_ms))
    finished_at = iso_now()
    duration_ms = math.floor((time.perf_counter() - start) * 1000 + 0.5)  # Math.round
    after = snapshot(repo)
    decoded = {name: bytes(data).decode("utf-8", "replace") for name, data in result["logs"].items()}

    def log(name: str) -> str:
        content = decoded[name]
        # Do not expose an incomplete credential at a truncated final line.
        if result["counts"][name] > MAX_LOG_BYTES:
            content = content[:max(0, content.rfind("\n"))]
        return clipped(content, 64000)

    spawn_error = result["spawnError"]
    payload = {
        "schema": EVIDENCE_VERSION, "policy": POLICY, "source": SOURCE,
        "label": clipped(label, 1000), "command": redact_command(command), "commandRedacted": True,
        "startedAt": started_at, "finishedAt": finished_at, "durationMs": duration_ms,
        "before": before["fingerprint"], "after": after["fingerprint"],
        "exitCode": None if spawn_error else result["exitCode"], "signal": result["signal"],
        "timedOut": result["timedOut"], "spawnError": spawn_error,
        "stdout": log("stdout"), "stderr": log("stderr"),
        "stdoutTruncated": result["counts"]["stdout"] > MAX_LOG_BYTES or utf16_length(decoded["stdout"]) > 64000,
        "stderrTruncated": result["counts"]["stderr"] > MAX_LOG_BYTES or utf16_length(decoded["stderr"]) > 64000,
    }
    record = {**payload, "id": digest(payload)}
    write_new(output, json_text(record))
    return record
