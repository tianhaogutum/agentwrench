"""Section 3: tool-call pairing in the most recent session files, via :mod:`agentwrench.doctor`.

Files are streamed line by line under a shared, non-blocking lock and never
modified. Only issue codes and line numbers are retained, never session text.
"""
from __future__ import annotations

import time
from collections import Counter
from pathlib import Path

from ..doctor.engine import DoctorError, decode, scan
from ..doctor.files import open_session
from .common import CLIENT_LABELS, Finding, Locations, quote, show_path

# A file written in the last few minutes whose only problem is unanswered calls,
# with no new user prompt after them, is most likely a session running right now.
LIVE_SECONDS = 600
LIVE_CODES = frozenset({"RD101", "RD201", "RD002"})
MAX_FILE_BYTES = 512 * 1024 * 1024
INVALID_CODES = frozenset({"RD001", "RD003"})


def recent_files(client: str, loc: Locations, limit: int) -> tuple[Path, list[Path]]:
    if client == "claude-code":
        root = loc.claude_dir / "projects"
        pattern, recursive = "*.jsonl", False
    else:
        root = loc.codex_home / "sessions"
        pattern, recursive = "rollout-*.jsonl", True
    if limit <= 0 or not root.is_dir():
        return root, []
    found: list[tuple[float, Path]] = []
    if recursive:
        candidates = root.rglob(pattern)
    else:  # Claude Code keeps one directory per project; subagent logs live deeper.
        candidates = (p for d in root.iterdir() if d.is_dir() and not d.is_symlink() for p in d.glob(pattern))
    for path in candidates:
        try:
            info = path.lstat()
        except OSError:
            continue
        if path.is_file() and not path.is_symlink():
            found.append((info.st_mtime, path))
    found.sort(key=lambda item: (-item[0], str(item[1])))
    return root, [path for _, path in found[:limit]]


def _is_prompt(record: dict, client: str) -> bool:
    """A new user turn (not a tool result): the conversation moved past any pending call."""
    if client == "codex":
        payload = record.get("payload")
        return (record.get("type") == "response_item" and isinstance(payload, dict)
                and payload.get("type") == "message" and payload.get("role") == "user")
    message = record.get("message")
    if record.get("type") != "user" or not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, str):
        return True
    return isinstance(content, list) and any(isinstance(b, dict) and b.get("type") != "tool_result"
                                             for b in content)


def _moved_on(path: Path, client: str, after_line: int) -> bool:
    with open_session(path) as source:
        for line, raw in enumerate(source, 1):
            if line <= after_line:
                continue
            try:
                record = decode(raw)
            except (TypeError, ValueError, UnicodeError, RecursionError):
                continue
            if _is_prompt(record, client):
                return True
    return False


def check_file(path: Path, client: str, now: float) -> dict:
    row: dict = {"path": str(path), "status": "clean", "issues": {}}
    try:
        info = path.lstat()
        if info.st_size > MAX_FILE_BYTES:
            row.update(status="skipped", error=f"larger than {MAX_FILE_BYTES // (1024 * 1024)} MiB")
            return row
        with open_session(path) as source:
            report = scan(source, str(path), "claude" if client == "claude-code" else "codex")
    except (OSError, DoctorError, ValueError, RecursionError) as exc:
        row.update(status="error", error=str(exc) if isinstance(exc, DoctorError) else type(exc).__name__)
        return row
    if not report.issues:
        return row
    row["issues"] = dict(sorted(Counter(issue.code for issue in report.issues).items()))
    row["first_issue_line"] = min(issue.line for issue in report.issues)
    live = (now - info.st_mtime < LIVE_SECONDS
            and all(issue.code in LIVE_CODES for issue in report.issues))
    if live:
        try:
            live = not _moved_on(path, client, min(issue.line for issue in report.issues))
        except (OSError, DoctorError):
            live = False
    row["status"] = "live" if live else "broken"
    return row


def repair_command(row: dict) -> list[str] | None:
    if "RD207" in row["issues"]:
        return None  # ambiguous Claude UUID graph: doctor refuses automatic repair
    words = ["agentwrench", "doctor", "repair", quote(show_path(row["path"]))]
    if INVALID_CODES & set(row["issues"]):
        words.append("--drop-invalid")
    return [" ".join([*words, "--dry-run"]), " ".join([*words, "--in-place", "--backup"])]


def scan_sessions(loc: Locations, limit: int, findings: list[Finding]) -> dict:
    result: dict = {}
    now = time.time()
    for client in CLIENT_LABELS:
        started = time.monotonic()
        root, files = recent_files(client, loc, limit)
        rows = [check_file(path, client, now) for path in files]
        counts = Counter(row["status"] for row in rows)
        broken = [row for row in rows if row["status"] == "broken"]
        example = next((cmd for cmd in map(repair_command, broken) if cmd), None)
        result[client] = {
            "root": str(root), "checked": len(rows), "clean": counts["clean"], "broken": counts["broken"],
            "live": counts["live"], "errors": counts["error"] + counts["skipped"],
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "files": [row for row in rows if row["status"] != "clean"],
            "example_command": example,
        }
        if broken:
            message = (f"{len(broken)} of {len(rows)} recent {CLIENT_LABELS[client]} {'session' if len(rows) == 1 else 'sessions'} "
                       f"{'has' if len(broken) == 1 else 'have'} unpaired or invalid tool-call records; "
                       "resuming fails until repaired (stop the agent first)")
            findings.append(Finding("error", "sessions", client, message, example or []))
    return result

