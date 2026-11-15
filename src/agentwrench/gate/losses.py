"""Account for source data outside the adapters' normalized/message projections.

Opaque tool inputs/results stay opaque: we preserve them, never interpret paths,
fetch attachments, decode media, or claim to validate a tool's private schema.
Pointers are RFC 6901 paths within the object at the physical source line.
"""
from typing import Any


def record_losses(source: str, line: int, record: dict, target: str = "normalize") -> list[dict]:
    losses = []
    exporting = target == "agentevals"

    def note(pointer: str, reason: str, disposition: str = "omitted") -> None:
        losses.append({"source_line": line, "pointer": pointer,
                       "reason": reason, "disposition": disposition})

    def fields(value: Any, supported: str, pointer: str = "") -> None:
        if isinstance(value, dict):
            for key in sorted(value.keys() - set(supported.split())):
                escaped = key.replace("~", "~0").replace("/", "~1")
                note(pointer + "/" + escaped, "unsupported_field")

    def metadata(value: dict, names: str, pointer: str = "") -> None:
        for key in names.split():
            if key in value:
                note(pointer + "/" + key, "metadata_not_normalized")

    def usage(value: Any, pointer: str) -> None:
        if exporting:
            note(pointer, "not_in_message_export")
        else:
            keys = "input_tokens output_tokens " + (
                "cache_read_input_tokens cache_creation_input_tokens" if source == "claude-stream"
                else "cached_input_tokens cache_write_input_tokens reasoning_output_tokens")
            fields(value, keys, pointer)

    def tool_result(value: Any, pointer: str) -> None:
        if exporting and value is not None and not isinstance(value, str):
            note(pointer, "structured_tool_result", "json_string")

    typ = record["type"]
    if source == "codex-exec":
        if typ in {"thread.started", "turn.started"}:
            fields(record, "type thread_id" if typ == "thread.started" else "type")
            metadata(record, "thread_id")
        elif typ in {"turn.completed", "turn.failed", "error"}:
            fields(record, {"turn.completed": "type usage", "turn.failed": "type error",
                            "error": "type message"}[typ])
            if "usage" in record:
                usage(record["usage"], "/usage")
            if "error" in record:
                fields(record["error"], "message", "/error")
            if exporting and typ != "turn.completed":
                note("", "not_in_message_export")
        elif typ in {"item.started", "item.updated", "item.completed"}:
            fields(record, "type item")
            item = record["item"]
            kind = item["type"]
            supported = {
                "agent_message": "text", "reasoning": "text",
                "command_execution": "command aggregated_output exit_code status",
                "file_change": "changes status",
                "mcp_tool_call": "server tool arguments result error status",
                "web_search": "query action results", "error": "message",
            }
            if kind == "todo_list":
                note("/item", "metadata_not_normalized")
            elif kind not in supported:
                note("/item", "unsupported_item")
            else:
                fields(item, "id type " + supported[kind], "/item")
                if kind == "file_change":
                    for i, change in enumerate(item["changes"]):
                        fields(change, "path kind", f"/item/changes/{i}")
                elif kind == "mcp_tool_call":
                    for key in ("result", "error"):
                        if key in item:
                            tool_result(item[key], "/item/" + key)
                if exporting and kind in {"reasoning", "error"}:
                    note("/item", "not_in_message_export")
                elif exporting and kind == "agent_message" and typ != "item.completed":
                    note("/item", "lifecycle_snapshot")
        else:
            note("", "unsupported_event")
        return losses

    if typ == "system":
        note("", "metadata_not_normalized" if record.get("subtype") == "init" else "unsupported_event")
    elif typ == "stream_event":
        note("", "partial_stream_event")
    elif typ == "result":
        fields(record, "type subtype is_error result usage num_turns")
        if "usage" in record:
            usage(record["usage"], "/usage")
        if exporting:
            for key in ("subtype", "is_error", "num_turns"):
                if key in record:
                    note("/" + key, "not_in_message_export")
    elif typ in {"assistant", "user"}:
        fields(record, "type message uuid isReplay parent_tool_use_id agent_id tool_use_result")
        metadata(record, "uuid isReplay parent_tool_use_id agent_id")
        if "tool_use_result" in record:
            # Only Bash exit/background flags are used; keep the whole value in originals.
            note("/tool_use_result", "partially_normalized")
        message = record["message"]
        fields(message, "id role content", "/message")
        metadata(message, "id", "/message")
        content = message.get("content")
        if isinstance(content, list):
            for i, block in enumerate(content):
                pointer = f"/message/content/{i}"
                bt = block["type"]
                if bt == "text":
                    fields(block, "type text", pointer)
                elif bt == "tool_use" and typ == "assistant":
                    fields(block, "type id name input", pointer)
                elif bt == "tool_result" and typ == "user":
                    fields(block, "type tool_use_id content is_error", pointer)
                    tool_result(block.get("content"), pointer + "/content")
                    if exporting and block.get("is_error"):
                        note(pointer + "/is_error", "not_in_message_export")
                else:
                    note(pointer, "unsupported_content")
    else:
        note("", "unsupported_event")
    return losses
