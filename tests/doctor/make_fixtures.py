"""Recreate the small synthetic, freely redistributable JSONL examples."""
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parent / "fixtures"


def item(kind, cid=None, **fields):
    payload = {"type": kind, **fields}
    if cid is not None:
        payload["call_id"] = cid
    return {"timestamp": "2026-01-01T00:00:00Z", "type": "response_item", "payload": payload}


def cnode(kind, uid, parent=None, blocks=None, side=False, session="synthetic"):
    return {"type": kind, "uuid": uid, "parentUuid": parent, "sessionId": session,
            "isSidechain": side, "message": {"role": kind, "content": blocks or []}}


def use(cid="u1"):
    return {"type": "tool_use", "id": cid, "name": "Read", "input": {"file_path": "example.txt"}}


def result(cid="u1"):
    return {"type": "tool_result", "tool_use_id": cid, "content": "synthetic output"}


def write(fmt, name, records, tail=b""):
    path = ROOT / fmt / (name + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(json.dumps(row, separators=(",", ":")).encode() + b"\n" for row in records) + tail)


def main():
    call = item("function_call", "c1", name="shell", arguments='{"command":"pwd"}')
    out = item("function_call_output", "c1", output="/synthetic")
    write("codex", "clean", [{"type": "session_meta", "payload": {"id": "synthetic"}}, call, out,
                               item("web_search_call", id="ws1", status="completed"),
                               {"type": "event_msg", "payload": {"type": "task_complete"}}])
    write("codex", "orphan", [item("function_call_output", output="bad missing id"), call])
    write("codex", "RD001", [call], b'{"broken":\n' + json.dumps(out).encode() + b"\n")
    write("codex", "RD002", [call, out], b'{"type":"response_item","payload":')
    write("codex", "RD101", [call])
    write("codex", "RD102", [out])
    write("codex", "RD103", [item("function_call_output", output="no id")])
    write("codex", "RD104", [call, call, out, out])
    write("codex", "RD105", [out, call])
    write("codex", "RD106", [call, item("custom_tool_call_output", "c1", output="wrong family")])
    write("codex", "RD107", [item("function_call", name="shell", arguments="{}")])
    write("codex", "RD003", [{"type": "response_item", "payload": []}])
    write("codex", "custom", [item("custom_tool_call", "patch1", name="apply_patch", input="synthetic patch")])
    a = cnode("assistant", "a", blocks=[{"type": "text", "text": "Reading"}, use()])
    r = cnode("user", "r", "a", [result()])
    write("claude", "clean", [a, r, cnode("assistant", "done", "r", [{"type": "text", "text": "Done"}])])
    write("claude", "orphan_tool_use", [a])
    write("claude", "RD001", [a], b'{"broken":\n' + json.dumps(r).encode() + b"\n")
    write("claude", "RD002", [a, r], b'{"type":"user","message":')
    write("claude", "RD201", [a])
    write("claude", "RD202", [cnode("user", "r", blocks=[result()])])
    write("claude", "RD203", [a, r, cnode("user", "r2", "r", [result()])])
    write("claude", "RD204", [cnode("user", "r", "lost", [{"type": "text", "text": "hello"}])])
    write("claude", "RD205", [a, cnode("assistant", "a2", "a", [use()]), r])
    write("claude", "RD206", [cnode("user", "r", blocks=[{"type": "tool_result", "content": "missing id"}])])
    write("claude", "RD207", [a, a])
    # Two sidechain branches use the same ID, but neither may satisfy its sibling.
    write("claude", "sidechains", [cnode("user", "root", blocks=[{"type": "text", "text": "start"}]),
          cnode("assistant", "sa", "root", [use()], True),
          cnode("assistant", "sb", "root", [use()], True),
          cnode("user", "rb", "sb", [result()], True)])


if __name__ == "__main__":
    main()
