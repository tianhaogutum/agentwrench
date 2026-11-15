"""Small edit plans, byte-preserving rendering, and offline post-repair validation."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import json
from typing import BinaryIO, Iterator
import uuid

from .engine import DoctorError, DROP_TEXT, Report, STUB_TEXT, decode, scan


@dataclass
class Plan:
    report: Report
    drop_lines: set[int] = field(default_factory=set)
    drop_blocks: dict[int, set[int]] = field(default_factory=lambda: defaultdict(set))
    stubs: dict[int, list] = field(default_factory=lambda: defaultdict(list))
    reset_parents: set[int] = field(default_factory=set)
    reparent: dict[int, str] = field(default_factory=dict)
    stub_uuids: dict[int, str] = field(default_factory=dict)


def make_plan(report: Report, strategy: str = "stub", drop_invalid: bool = False) -> Plan:
    if strategy not in ("stub", "drop"):
        raise DoctorError("strategy must be stub or drop")
    if any(issue.code == "RD207" for issue in report.issues):
        raise DoctorError("ambiguous Claude UUID graph (RD207); manual graph repair required")
    if not drop_invalid and any(i.code in ("RD001", "RD003") for i in report.issues):
        raise DoctorError("invalid JSON/record in file; review it or explicitly use --drop-invalid")
    report.changes.clear()
    plan = Plan(report)

    def change(action: str, line: int, detail: str, call_id=None, block=None) -> None:
        data = {"action": action, "line": line, "detail": detail}
        if call_id is not None:
            data["call_id"] = call_id
        if block is not None:
            data["block"] = block
        report.changes.append(data)

    for issue in report.issues:
        if issue.code in ("RD101", "RD201"):
            continue
        if issue.code == "RD204":
            plan.reset_parents.add(issue.line)
            change("reset_parent", issue.line, "set dangling parentUuid to null")
        elif issue.block is not None:
            plan.drop_blocks[issue.line].add(issue.block)
            change("drop_block", issue.line, issue.detail, issue.call_id, issue.block)
        else:
            plan.drop_lines.add(issue.line)
            change("drop_line", issue.line, issue.detail, issue.call_id)
    for call in report.missing:
        if strategy == "drop":
            if call.block is None:
                plan.drop_lines.add(call.line)
                change("drop_line", call.line, "remove call without output", call.call_id)
            else:
                plan.drop_blocks[call.line].add(call.block)
                change("drop_block", call.line, "remove tool_use without result", call.call_id, call.block)
        else:
            if call.output_kind == "tool_search_output":
                raise DoctorError("missing client tool_search_output cannot carry an error-text stub; use --strategy drop")
            plan.stubs[call.line].append(call)
            change("insert_stub", call.line, "insert synthetic error output immediately after call", call.call_id)
    if report.format == "claude":
        children: dict[str, list] = defaultdict(list)
        for uid, node in report.nodes.items():
            children[node.parent].append((uid, node))
        for line, calls in plan.stubs.items():
            call = calls[0]
            node = report.nodes[call.node]
            stub_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "rollout-doctor:" + call.node + ":" +
                                     json.dumps([c.call_id for c in calls], ensure_ascii=True)))
            if stub_id in report.nodes:
                raise DoctorError("synthetic UUID would collide with existing record; manual repair required")
            plan.stub_uuids[line] = stub_id
            for _, child in children.get(call.node, []):
                if (child.session, child.sidechain) == (node.session, node.sidechain):
                    plan.reparent[child.line] = stub_id
                    change("reparent", child.line, "link child through inserted tool_result")
    report.changes.sort(key=lambda c: (c["line"], c.get("block", -1), c["action"]))
    return plan


def encode(record: dict, newline: bytes = b"\n") -> bytes:
    return json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + newline


def transform(plan: Plan, line: int, raw: bytes) -> list[bytes]:
    if line in plan.drop_lines:
        return []
    calls = plan.stubs.get(line, [])
    modify = line in plan.drop_blocks or line in plan.reset_parents or line in plan.reparent
    if not calls and not modify:
        return [raw]
    record = decode(raw)
    newline = b"\r\n" if raw.endswith(b"\r\n") else b"\n"
    if line in plan.drop_blocks:
        message = record["message"]
        message["content"] = [block for i, block in enumerate(message["content"])
                              if i not in plan.drop_blocks[line]]
        if not message["content"]:
            message["content"] = [{"type": "text", "text": DROP_TEXT}]
    if line in plan.reset_parents:
        record["parentUuid"] = None
    if line in plan.reparent:
        record["parentUuid"] = plan.reparent[line]
    # Preserve an unterminated but otherwise valid original final line, except where
    # inserting a record necessarily adds a separating newline.
    original = encode(record, newline if raw.endswith(b"\n") or calls else b"") if modify else raw
    result = [original]
    if calls and not original.endswith(b"\n"):
        result[0] += newline
    if calls and plan.report.format == "codex":
        call = calls[0]
        payload = {"type": call.output_kind, "call_id": call.call_id, "output": STUB_TEXT}
        stub = {"type": "response_item", "payload": payload}
        if "timestamp" in record:
            stub["timestamp"] = record["timestamp"]
        result.append(encode(stub, newline))
    elif calls:
        stub = {key: record[key] for key in ("sessionId", "isSidechain", "cwd", "version", "timestamp")
                if key in record}
        stub.update({"type": "user", "uuid": plan.stub_uuids[line], "parentUuid": record["uuid"],
                     "message": {"role": "user", "content": [
                         {"type": "tool_result", "tool_use_id": c.call_id,
                          "content": STUB_TEXT, "is_error": True} for c in calls]}})
        result.append(encode(stub, newline))
    return result


def render(source: BinaryIO, plan: Plan) -> Iterator[bytes]:
    source.seek(0)
    for line, raw in enumerate(source, 1):
        yield from transform(plan, line, raw)


def validate_plan(source: BinaryIO, plan: Plan) -> None:
    fixed = scan(render(source, plan), plan.report.file,
                 plan.report.format if plan.report.format != "empty" else "auto")
    if fixed.issues:
        codes = ", ".join(sorted({i.code for i in fixed.issues}))
        raise DoctorError(f"repair plan still has issues ({codes}); no output written")
