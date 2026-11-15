"""OpenAI-style message export for agentevals; never fabricates tool outcomes."""
import json
import hashlib
import re

from .model import InputError, Trajectory


def export_agentevals(t: Trajectory) -> list[dict]:
    if not t.complete:
        raise InputError("cannot export incomplete trajectory (terminal event or tool results missing)")
    messages = []
    seen = set()
    for e in t.events:
        kind = e["kind"]
        if kind == "message" and not e.get("reasoning") and e.get("completed"):
            messages.append({"role": e["role"], "content": e["text"]})
        elif kind in {"tool_call", "command", "file_change", "web_search"} and "name" in e:
            cid = e["call_id"]
            if cid in seen:
                continue
            seen.add(cid)
            if not isinstance(e.get("arguments"), dict):
                raise InputError(f"cannot export missing/non-object tool arguments: {cid}")
            name = e["name"]
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name):
                name = "tool_" + hashlib.sha256(name.encode("utf-8", errors="surrogatepass")).hexdigest()[:56]
            messages.append({"role": "assistant", "content": "", "tool_calls": [{
                "id": cid, "type": "function", "function": {
                    "name": name, "arguments": json.dumps(e["arguments"], ensure_ascii=False, sort_keys=True, allow_nan=False)
                }}]})
        elif kind == "tool_result":
            if e["content"] is None:
                raise InputError(f"cannot export missing tool outcome: {e['call_id']}")
            if e["call_id"] not in seen:
                raise InputError(f"cannot export orphan tool result: {e['call_id']}")
            content = e["content"] if isinstance(e["content"], str) else json.dumps(e["content"], ensure_ascii=False, sort_keys=True, allow_nan=False)
            messages.append({"role": "tool", "tool_call_id": e["call_id"], "content": content})
    # Parallel calls must be grouped before results for a valid conversation.
    grouped = []
    open_calls: set[str] = set()
    for message in messages:
        if message.get("tool_calls"):
            if grouped and grouped[-1].get("tool_calls"):
                grouped[-1]["tool_calls"].extend(message["tool_calls"])
            elif open_calls:
                raise InputError("cannot export interleaved parallel calls/messages as an OpenAI conversation")
            else:
                grouped.append(message)
            open_calls.update(c["id"] for c in message["tool_calls"])
        elif message["role"] == "tool":
            if message["tool_call_id"] not in open_calls:
                raise InputError("cannot export duplicate tool result")
            open_calls.remove(message["tool_call_id"])
            grouped.append(message)
        else:
            if open_calls:
                raise InputError("cannot export a message interleaved before pending tool results")
            grouped.append(message)
    if open_calls:
        raise InputError("cannot export tool calls without results")
    return grouped
