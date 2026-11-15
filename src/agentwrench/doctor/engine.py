"""Streaming parser and shared pairing engine; retains identifiers, never payloads."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Iterable

STUB_TEXT = "[rollout-doctor] tool output missing: call was interrupted"
DROP_TEXT = "[rollout-doctor] removed invalid tool blocks"
PAIRS = {
    "function_call": "function_call_output",
    "custom_tool_call": "custom_tool_call_output",
    "local_shell_call": "function_call_output",
    "tool_search_call": "tool_search_output",
}
OUTPUTS = frozenset(PAIRS.values())
CODEX_MARKERS = frozenset({"session_meta", "response_item", "turn_context", "event_msg",
                            "token_usage_record", "world_state"})


class DoctorError(Exception):
    """An operational error or an input outside the supported format boundary."""


class FormatError(DoctorError):
    """Unsupported, mixed or mismatched session format (safe to categorize)."""


class UndetectedFormat(FormatError):
    """Auto-detection found no Codex or Claude Code session records (e.g. other JSONL logs)."""


def usable_id(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _bad_constant(_: str) -> None:
    raise ValueError("non-finite JSON number")


def decode(raw: bytes) -> dict:
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                       parse_constant=_bad_constant)
    if not isinstance(value, dict):
        raise TypeError("JSONL record must be an object")
    return value


@dataclass(frozen=True)
class Issue:
    code: str
    line: int
    detail: str
    call_id: str | None = None
    block: int | None = None

    def as_dict(self) -> dict:
        data = {"code": self.code, "line": self.line, "detail": self.detail}
        if self.call_id is not None:
            data["call_id"] = self.call_id
        if self.block is not None:
            data["block"] = self.block
        return data


@dataclass(frozen=True)
class Event:
    line: int
    block: int | None
    call_id: str
    kind: str
    output_kind: str = ""
    node: str | None = None


@dataclass(frozen=True)
class Node:
    line: int
    parent: str | None
    session: str | None
    sidechain: bool


@dataclass
class Report:
    file: str
    format: str
    issues: list[Issue] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)
    events: list[Event] = field(default_factory=list, repr=False)
    nodes: dict[str, Node] = field(default_factory=dict, repr=False)
    missing: list[Event] = field(default_factory=list, repr=False)
    digest: str = field(default="", repr=False)
    lines: int = 0

    def as_dict(self) -> dict:
        return {"file": self.file, "format": self.format,
                "issues": [i.as_dict() for i in self.issues], "changes": self.changes}

    def issue(self, code: str, line: int, detail: str,
              call_id: str | None = None, block: int | None = None) -> None:
        self.issues.append(Issue(code, line, detail, call_id, block))


def _format(record: dict) -> str | None:
    kind = record.get("type")
    if isinstance(kind, str) and kind in CODEX_MARKERS:
        return "codex"
    if (kind in ("assistant", "user") and "message" in record) or (
        "uuid" in record
    ):
        return "claude"
    return None


def _codex(report: Report, record: dict, line: int) -> None:
    if record.get("type") != "response_item":
        return
    payload = record.get("payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
        report.issue("RD003", line, "response_item requires an object payload with type")
        return
    kind = payload["type"]
    if kind not in PAIRS and kind not in OUTPUTS:
        return
    # Server-executed search is self-contained, not a client call/output obligation.
    if kind in ("tool_search_call", "tool_search_output"):
        if payload.get("execution") == "server":
            return
        if payload.get("execution") != "client":
            report.issue("RD003", line, "tool search execution must be client or server")
            return
    cid = payload.get("call_id")
    if kind == "local_shell_call" and cid is None:
        cid = payload.get("id")  # Legacy Codex local-shell identifier.
    if not usable_id(cid):
        code = "RD103" if kind in OUTPUTS else "RD107"
        report.issue(code, line, "tool output missing usable call_id" if kind in OUTPUTS
                     else "tool call missing usable call_id (legacy local shell also accepts id)")
        return
    report.events.append(Event(line, None, cid, kind, PAIRS.get(kind, "")))


def _claude(report: Report, record: dict, line: int) -> None:
    kind = record.get("type")
    uid, parent = record.get("uuid"), record.get("parentUuid")
    message = record.get("message")
    content: list = []
    if kind in ("assistant", "user"):
        if not isinstance(message, dict) or not isinstance(message.get("content"), (str, list)):
            report.issue("RD003", line, "user/assistant requires message.content string or array")
            return
        if isinstance(message["content"], list):
            content = message["content"]
            if any(not isinstance(block, dict) for block in content):
                report.issue("RD003", line, "message content array must contain objects")
                return
        if not usable_id(uid):
            report.issue("RD003", line, "Claude user/assistant record requires a usable uuid")
            return
    if uid is not None and not usable_id(uid):
        report.issue("RD003", line, "uuid must be a nonempty string")
        return
    if parent is not None and not usable_id(parent):
        report.issue("RD003", line, "parentUuid must be null or a nonempty string")
        return
    if record.get("sessionId") is not None and not usable_id(record["sessionId"]):
        report.issue("RD003", line, "sessionId must be null or a nonempty string")
        return
    if "isSidechain" in record and not isinstance(record["isSidechain"], bool):
        report.issue("RD003", line, "isSidechain must be a boolean")
        return
    if usable_id(uid):
        if uid in report.nodes:
            report.issue("RD207", line, "duplicate uuid: graph repair is ambiguous")
            return
        report.nodes[uid] = Node(line, parent, record.get("sessionId"),
                                 record.get("isSidechain", False))
    elif parent is not None:
        report.issue("RD003", line, "record with parentUuid requires uuid")
        return
    for index, block in enumerate(content):
        btype = block.get("type")
        if btype not in ("tool_use", "tool_result"):
            continue
        cid = block.get("id" if btype == "tool_use" else "tool_use_id")
        expected_role = "assistant" if btype == "tool_use" else "user"
        if not usable_id(cid) or kind != expected_role or message.get("role", kind) != kind:
            report.issue("RD206", line, "tool block has missing id or incorrect message role",
                         cid if usable_id(cid) else None, index)
            continue
        report.events.append(Event(line, index, cid, btype,
                                   "tool_result" if btype == "tool_use" else "", uid))


def _intervals(report: Report) -> dict[str, tuple[int, int]]:
    """Compute ancestry intervals iteratively; never recurse through user input."""
    children: dict[str | None, list[str]] = {}
    for uid, node in report.nodes.items():
        parent = report.nodes.get(node.parent)
        if node.parent is not None and parent is None:
            report.issue("RD204", node.line, "parentUuid does not identify a record in this file")
        if parent and parent.line >= node.line:
            report.issue("RD207", node.line, "parentUuid must point to an earlier record; cycle/forward edge unsupported")
            continue
        same_scope = parent and (parent.session, parent.sidechain) == (node.session, node.sidechain)
        children.setdefault(node.parent if same_scope else None, []).append(uid)
    intervals: dict[str, tuple[int, int]] = {}
    starts: dict[str, int] = {}
    clock = 0
    stack = [(uid, False) for uid in reversed(children.get(None, []))]
    while stack:
        uid, exiting = stack.pop()
        if exiting:
            intervals[uid] = (starts[uid], clock)
        else:
            clock += 1
            starts[uid] = clock
            stack.append((uid, True))
            stack.extend((child, False) for child in reversed(children.get(uid, [])))
    return intervals


def pair(report: Report) -> None:
    """Pair explicit client calls/results with format-specific chain eligibility."""
    claude = report.format == "claude"
    intervals = _intervals(report) if claude else {}
    if any(i.code == "RD207" for i in report.issues):
        return  # Graph is ambiguous. Never pretend it can be safely repaired.

    def related(call: Event, output: Event) -> bool:
        if not claude:
            return True
        a, b = report.nodes[call.node], report.nodes[output.node]
        if (a.session, a.sidechain) != (b.session, b.sidechain):
            return False
        start, end = intervals[call.node]
        other = intervals[output.node][0]
        return start <= other <= end

    calls: dict[str, list[Event]] = {}
    for event in report.events:
        if not event.output_kind:
            continue
        candidates = calls.setdefault(event.call_id, [])
        if any(related(call, event) for call in candidates):
            report.issue("RD205" if claude else "RD104", event.line,
                         "duplicate tool call identifier; first call is retained",
                         event.call_id, event.block)
        else:
            candidates.append(event)
    satisfied: set[Event] = set()
    for event in report.events:
        if event.output_kind:
            continue
        candidates = calls.get(event.call_id, [])
        eligible = [call for call in candidates if related(call, event)]
        if not eligible:
            report.issue("RD202" if claude else "RD102", event.line,
                         "tool output has no call in its chain", event.call_id, event.block)
            continue
        call = eligible[-1]
        if event.line <= call.line:
            report.issue("RD202" if claude else "RD105", event.line,
                         "tool output occurs before its call", event.call_id, event.block)
        elif call.output_kind != event.kind:
            report.issue("RD106", event.line, "output type does not match call type",
                         event.call_id, event.block)
        elif call in satisfied:
            report.issue("RD203" if claude else "RD104", event.line,
                         "duplicate tool output identifier; first valid output is retained",
                         event.call_id, event.block)
        else:
            satisfied.add(call)
    for candidates in calls.values():
        for call in candidates:
            if call not in satisfied:
                report.missing.append(call)
                report.issue("RD201" if claude else "RD101", call.line,
                             "tool call has no later output in its chain", call.call_id, call.block)


def add_record(report: Report, record: dict, line: int, format: str) -> None:
    """Accumulate only parsing metadata; pairing findings are snapshot-derived."""
    report.lines = line
    marker = _format(record)
    if marker:
        if (report.format not in ("auto", "empty", marker) or
                format != "auto" and format != marker):
            raise FormatError(f"{report.file}: mixed or mismatched session formats at line {line}")
        report.format = marker
    if report.format == "codex":
        _codex(report, record, line)
    elif report.format == "claude":
        _claude(report, record, line)


def sort_issues(report: Report) -> None:
    report.issues.sort(key=lambda issue: (issue.line, issue.block if issue.block is not None else -1,
                                           issue.code))


def scan(lines: Iterable[bytes], file: str, format: str = "auto") -> Report:
    if format not in ("auto", "codex", "claude"):
        raise DoctorError("format must be auto, codex or claude")
    report = Report(file, format)
    digest = hashlib.sha256()
    # Unknown records pass through; UUID-bearing Claude metadata identifies its own graph.
    for line, raw in enumerate(lines, 1):
        report.lines = line
        digest.update(raw)
        try:
            record = decode(raw)
        except TypeError:
            report.issue("RD003", line, "JSONL record must be an object")
            continue
        except (ValueError, UnicodeError, RecursionError):
            report.issue("RD001" if raw.endswith(b"\n") else "RD002", line,
                         "invalid UTF-8/JSON line" if raw.endswith(b"\n")
                         else "invalid final JSON line without newline (possibly truncated)")
            continue
        add_record(report, record, line, format)
    report.digest = digest.hexdigest()
    if report.format == "auto":
        if report.lines:
            message = f"{file}: cannot detect format; use --format codex or --format claude"
            # Only well-formed JSONL of another kind is "not a session"; garbage stays an error.
            raise (FormatError if report.issues else UndetectedFormat)(message)
        report.format = "empty"
    pair(report)
    sort_issues(report)
    return report
