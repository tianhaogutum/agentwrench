"""JSONL readers and adapters for public Codex exec / Claude stream-json formats."""
import json
import hashlib
import math
import posixpath
import re
from contextlib import nullcontext
from pathlib import Path
from typing import Any, BinaryIO, Iterator

from .model import InputError, Trajectory, integer, obj, string
from .losses import record_losses

MAX_STREAM_BYTES = 64 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024
MAX_LINES = 100_000


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise InputError("nonfinite JSON number")
    return result


def mcp_name(server: str, tool: str) -> str:
    # Escape underscores before inserting a delimiter, so (a_, b) != (a, _b).
    name = server.replace("_", "_u") + "__" + tool.replace("_", "_u")
    if re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name):
        return name
    identity = json.dumps([server, tool], ensure_ascii=True).encode("ascii")
    return "mcp_" + hashlib.sha256(identity).hexdigest()[:56]


def _binary_lines(handle: BinaryIO) -> Iterator[bytes]:
    """Frame physical lines across short reads, stopping at most at cap + 1."""
    pending = bytearray()
    total = 0
    while True:
        chunk = handle.read(min(64 * 1024, MAX_STREAM_BYTES - total + 1,
                                MAX_LINE_BYTES - len(pending) + 1))
        if not chunk:
            if pending:
                yield bytes(pending)
            return
        total += len(chunk)
        if total > MAX_STREAM_BYTES:
            raise InputError("stream exceeds limit (64 MiB, 1 MiB/line, 100000 lines)")
        pending.extend(chunk)
        start = 0
        while (end := pending.find(b"\n", start)) >= 0:
            yield bytes(pending[start:end + 1])
            start = end + 1
        del pending[:start]
        if len(pending) > MAX_LINE_BYTES:
            raise InputError("stream exceeds limit (64 MiB, 1 MiB/line, 100000 lines)")


def read_records(path: str | Path | BinaryIO, *, original: list[bytes] | None = None) -> list[tuple[int, dict]]:
    records = []
    # Only handles opened here are owned here. Borrowed streams stay at EOF,
    # without seeking, closing or applying text newline/encoding conversion.
    with (Path(path).open("rb") if isinstance(path, (str, Path)) else nullcontext(path)) as handle:
        for line_no, raw in enumerate(_binary_lines(handle), 1):
            if line_no > MAX_LINES or len(raw) > MAX_LINE_BYTES:
                raise InputError("stream exceeds limit (64 MiB, 1 MiB/line, 100000 lines)")
            if original is not None:
                original.append(raw)
            if not raw.strip():
                continue
            try:
                record = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                                    parse_float=_finite_float,
                                    parse_constant=lambda x: (_ for _ in ()).throw(InputError(f"invalid JSON number: {x}")))
                record = obj(record, "event")
                string(record.get("type"), "event.type")
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise InputError(f"line {line_no}: {exc}") from exc
            records.append((line_no, record))
    if not records:
        raise InputError("empty stream")
    return records


def normalize(path: str | Path | BinaryIO, source: str = "auto", root: str | None = None,
              *, preserve_original: bool = False) -> Trajectory:
    original = [] if preserve_original else None
    records = read_records(path, original=original)
    if source == "auto":
        formats = set()
        for _, r in records:
            if r["type"] in {"thread.started", "turn.started", "turn.completed", "turn.failed",
                             "item.started", "item.updated", "item.completed"}:
                formats.add("codex-exec")
            if r["type"] in {"assistant", "user", "system", "result", "stream_event"}:
                formats.add("claude-stream")
        if len(formats) != 1:
            raise InputError("cannot auto-detect one stream format; specify --from for ambiguous streams")
        source = formats.pop()
    if source not in {"codex-exec", "claude-stream"}:
        raise InputError(f"unsupported source: {source}")
    adapter = CodexAdapter(root) if source == "codex-exec" else ClaudeAdapter(root)
    for line, record in records:
        try:
            adapter.accept(line, record)
            adapter.trajectory.losses.extend(record_losses(source, line, record))
            adapter.trajectory.export_losses.extend(record_losses(source, line, record, "agentevals"))
        except (InputError, RecursionError) as exc:
            raise InputError(f"line {line}: {exc}") from exc
    t = adapter.trajectory
    if original is not None:
        t.original_jsonl = b"".join(original).decode("utf-8")
    t.pending_calls = sorted(adapter.pending)
    t.complete = adapter.terminal and not t.pending_calls
    return t


class Adapter:
    def __init__(self, source: str, root: str | None):
        self.trajectory = Trajectory(source)
        self.root = posixpath.normpath(root) if root else None
        if self.root and not self.root.startswith("/"):
            raise InputError("--root must be an absolute POSIX path")
        self.pending: set[str] = set()
        self.terminal = False

    def path(self, path: Any) -> str:
        path = string(path, "file path")
        if "\x00" in path or "\\" in path:
            raise InputError("file paths must be POSIX paths without NUL or backslashes")
        path = posixpath.normpath(path)
        if path.startswith("/") and self.root:
            path = posixpath.relpath(path, self.root)
        return path

    def usage(self, line: int, raw: Any, *, claude: bool = False) -> None:
        t = self.trajectory
        if raw is None:
            t.emit("usage", line, total_tokens=None)
            return
        u = obj(raw, "usage")
        # Codex cached/reasoning tokens are subsets. Claude cache reads/writes are separate.
        keys = ["input_tokens", "output_tokens"]
        if claude:
            keys += ["cache_read_input_tokens", "cache_creation_input_tokens"]
        values = {k: integer(u[k], f"usage.{k}") for k in keys if k in u}
        total = sum(values.values()) if all(k in values for k in keys[:2]) else None
        if not claude:
            for key in ("cached_input_tokens", "cache_write_input_tokens", "reasoning_output_tokens"):
                if key in u:
                    values[key] = integer(u[key], f"usage.{key}")
        t.emit("usage", line, **values, total_tokens=total)


class CodexAdapter(Adapter):
    def __init__(self, root: str | None):
        super().__init__("codex-exec", root)
        self.items: dict[str, dict] = {}
        self.finished: set[str] = set()

    def accept(self, line: int, r: dict) -> None:
        t, typ = self.trajectory, r["type"]
        if typ in {"thread.started", "turn.started"}:
            self.terminal = False
            return
        if typ in {"turn.completed", "turn.failed"}:
            success = typ == "turn.completed"
            if success:
                self.usage(line, r.get("usage"))
            else:
                error = obj(r.get("error"), "turn.failed.error")
                t.emit("error", line, message=string(error.get("message"), "error.message"))
            t.emit("end", line, success=success)
            self.terminal = True
            return
        if typ == "error":
            t.emit("error", line, message=string(r.get("message"), "error.message"))
            # Top-level error is fatal in the public schema, but is not a turn terminator.
            self.terminal = False
            return
        if typ not in {"item.started", "item.updated", "item.completed"}:
            t.ignore(typ)
            return
        item = obj(r.get("item"), "item")
        item_id = string(item.get("id"), "item.id")
        kind = string(item.get("type"), "item.type")
        known = {"agent_message", "reasoning", "command_execution", "file_change",
                 "mcp_tool_call", "web_search", "todo_list", "error"}
        if kind not in known:
            t.ignore(f"item.{kind}")
            return
        if kind == "todo_list":
            return  # Known metadata, not an action.
        self.terminal = False
        if item_id in self.finished:
            raise InputError(f"item {item_id}: event after completion / duplicate completion")
        completed = typ == "item.completed"
        call_id = "codex:" + item_id
        e = self.items.get(item_id)
        if e is not None and e["item_type"] != kind:
            raise InputError(f"item {item_id}: type changed")
        if e is None:
            mapped = {"agent_message": "message", "reasoning": "message", "command_execution": "command",
                      "file_change": "file_change", "mcp_tool_call": "tool_call", "web_search": "web_search", "error": "error"}
            e = t.emit(mapped[kind], line, item_id=item_id, item_type=kind)
            self.items[item_id] = e
        e["completed"] = completed
        if completed:
            e["completion_line"] = line
            self.finished.add(item_id)
            self.pending.discard(call_id)
        else:
            self.pending.add(call_id)
        if kind in {"agent_message", "reasoning"}:
            if not isinstance(item.get("text"), str):
                raise InputError("item.text: expected string")
            e.update(role="assistant", text=item["text"], reasoning=kind == "reasoning")
        elif kind == "command_execution":
            code = item.get("exit_code")
            if code is not None:
                integer(code, "exit_code", nonnegative=False)
            output = item.get("aggregated_output", "")
            if not isinstance(output, str):
                raise InputError("aggregated_output: expected string")
            if "command" in e and e["command"] != item.get("command"):
                raise InputError(f"item {item_id}: command changed during lifecycle")
            self._status(item, completed, {"in_progress", "completed", "failed", "declined"})
            e.update(command=string(item.get("command"), "command"), exit_code=code, output=output,
                     status=item.get("status"), call_id=call_id, name="command_execution",
                     arguments={"command": item["command"]})
            if completed:
                self._result(line, e, {"exit_code": code, "output": output}, code not in (None, 0) or item.get("status") in {"failed", "declined"})
        elif kind == "file_change":
            changes = item.get("changes")
            if not isinstance(changes, list) or not changes:
                raise InputError("file_change.changes: expected nonempty list")
            mapped_changes = []
            for change in changes:
                change = obj(change, "change")
                if not isinstance(change.get("kind"), str) or change["kind"] not in {"add", "update", "delete"}:
                    raise InputError(f"unsupported file change kind: {change.get('kind')}")
                mapped_changes.append({"path": self.path(change.get("path")), "kind": change["kind"]})
            self._status(item, completed, {"in_progress", "completed", "failed"})
            # Retain every observed path, even if a later lifecycle snapshot omits it.
            previous = e.get("changes", [])
            e.update(changes=previous + [c for c in mapped_changes if c not in previous],
                     status=item.get("status"), call_id=call_id, name="file_change",
                     arguments={"changes": previous + [c for c in mapped_changes if c not in previous]})
            if completed:
                self._result(line, e, {"status": item.get("status"), "changes": e["changes"]}, item.get("status") == "failed")
        elif kind == "mcp_tool_call":
            self._status(item, completed, {"in_progress", "completed", "failed"})
            server, tool = string(item.get("server"), "server"), string(item.get("tool"), "tool")
            name = mcp_name(server, tool)
            arguments = item.get("arguments")
            if arguments is None:
                arguments = e.get("arguments")
            if "name" in e and (e["name"] != name or (e["arguments"] is not None and e["arguments"] != arguments)):
                raise InputError(f"item {item_id}: MCP invocation changed during lifecycle")
            e.update(call_id=call_id, name=name, server=server, tool=tool,
                     arguments=arguments, status=item.get("status"))
            if completed:
                failed = bool(item.get("error")) or item.get("status") == "failed"
                self._result(line, e, item.get("error") if failed else item.get("result"), failed)
        elif kind == "web_search":
            e.update(call_id=call_id, name="web_search", query=string(item.get("query"), "query"),
                     arguments={"query": item["query"]})
            if completed:
                self._result(line, e, {"results": item.get("results"), "action": item.get("action")}, False)
        else:
            e["message"] = string(item.get("message"), "error.message")

    @staticmethod
    def _status(item: dict, completed: bool, allowed: set[str]) -> None:
        status = item.get("status")
        if not isinstance(status, str) or status not in allowed or (completed and status == "in_progress"):
            raise InputError(f"invalid or nonterminal item status: {status!r}")

    def _result(self, line: int, call: dict, content: Any, failed: bool) -> None:
        self.trajectory.emit("tool_result", line, call_id=call["call_id"], content=content, is_error=failed)
        if failed:
            self.trajectory.emit("error", line, call_id=call["call_id"], message=f"{call['name']} failed")


class ClaudeAdapter(Adapter):
    def __init__(self, root: str | None):
        super().__init__("claude-stream", root)
        self.calls: dict[str, dict] = {}
        self.commands: dict[str, dict] = {}
        self.message_ids: set[str] = set()

    def accept(self, line: int, r: dict) -> None:
        t, typ = self.trajectory, r["type"]
        if r.get("parent_tool_use_id") is not None or r.get("agent_id") is not None:
            raise InputError("nested/subagent streams are unsupported; use a top-level trajectory")
        if typ == "system":
            if r.get("subtype") == "init":
                self.terminal = False
                if not self.root and r.get("cwd"):
                    self.root = string(r["cwd"], "cwd")
                    if not self.root.startswith("/"):
                        raise InputError("system.cwd must be absolute")
            else:
                t.ignore("system." + str(r.get("subtype", "unknown")))
            return
        if typ == "stream_event":
            # Full assistant messages carry the content; never count token deltas twice.
            t.ignore("stream_event")
            return
        if typ == "result":
            subtype = string(r.get("subtype"), "result.subtype")
            if type(r.get("is_error")) is not bool:
                raise InputError("result.is_error: expected boolean")
            success = subtype == "success" and not r["is_error"]
            self.usage(line, r.get("usage"), claude=True)
            if not success:
                t.emit("error", line, message="Claude result: " + subtype)
            # result.result repeats the assistant final answer; use only when no such message exists.
            if success and isinstance(r.get("result"), str) and r["result"]:
                previous = next((e for e in reversed(t.events) if e["kind"] == "message" and e["role"] == "assistant"), None)
                if previous is None or previous["text"] != r["result"]:
                    t.emit("message", line, role="assistant", text=r["result"], reasoning=False, completed=True)
            turns = r.get("num_turns")
            if turns is not None:
                integer(turns, "num_turns")
            t.emit("end", line, success=success, subtype=subtype, num_turns=turns)
            self.terminal = True
            return
        if typ not in {"assistant", "user"}:
            t.ignore(typ)
            return
        self.terminal = False
        message = obj(r.get("message"), "message")
        if r.get("isReplay"):
            raise InputError("replayed messages are unsupported")
        if message.get("id") is not None:
            string(message["id"], "message.id")
        # A complete assistant content block is one JSONL record; several records
        # legitimately share the API message.id. Only the outer UUID is unique.
        mid = r.get("uuid")
        if mid:
            string(mid, "message id")
            if mid in self.message_ids:
                raise InputError(f"duplicate/replayed message {mid} is unsupported")
            self.message_ids.add(mid)
        content = message.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list):
            raise InputError("message.content: expected string or list")
        for block in content:
            b = obj(block, "content block")
            bt = string(b.get("type"), "content block.type")
            if bt == "text":
                text = b.get("text")
                if not isinstance(text, str):
                    raise InputError("content.text: expected string")
                t.emit("message", line, role=typ, text=text, reasoning=False, completed=True)
            elif bt == "tool_use" and typ == "assistant":
                self._call(line, b)
            elif bt == "tool_result" and typ == "user":
                self._result(line, b, r.get("tool_use_result"))
            elif bt not in {"thinking", "redacted_thinking"}:
                t.ignore("content." + bt)

    def _call(self, line: int, b: dict) -> None:
        cid, name = string(b.get("id"), "tool_use.id"), string(b.get("name"), "tool_use.name")
        if cid in self.calls:
            raise InputError(f"duplicate tool_use id: {cid}")
        args = obj(b.get("input"), "tool_use.input")
        t = self.trajectory
        call = t.emit("tool_call", line, call_id=cid, name=name, arguments=args)
        self.calls[cid] = call
        self.pending.add(cid)
        if name == "Bash":
            self.commands[cid] = t.emit("command", line, call_id=cid, command=string(args.get("command"), "Bash.command"),
                                        exit_code=None, completed=False)
        elif name in {"Write", "Edit", "MultiEdit", "NotebookEdit"}:
            path = args.get("notebook_path") if name == "NotebookEdit" else args.get("file_path")
            t.emit("file_change", line, call_id=cid, changes=[{"path": self.path(path), "kind": "write" if name == "Write" else "update"}],
                   observed="attempt")
        elif name == "WebSearch":
            t.emit("web_search", line, call_id=cid, query=string(args.get("query"), "WebSearch.query"))

    def _result(self, line: int, b: dict, structured: Any) -> None:
        cid = string(b.get("tool_use_id"), "tool_result.tool_use_id")
        if cid not in self.calls or cid not in self.pending:
            raise InputError(f"orphan or duplicate tool_result: {cid}")
        failed = b.get("is_error", False)
        if type(failed) is not bool:
            raise InputError("tool_result.is_error: expected boolean")
        content = b.get("content", "")
        self.trajectory.emit("tool_result", line, call_id=cid, content=content, is_error=failed)
        detached = isinstance(structured, dict) and (structured.get("backgroundTaskId") or structured.get("detachedToolCall"))
        if not detached:
            self.pending.remove(cid)
        if cid in self.commands:
            command = self.commands[cid]
            command.update(completed=not bool(detached), completion_line=line, output=content, is_error=failed)
            # BashOutput has no documented exit code. Do not infer zero from tool success.
            if isinstance(structured, dict) and "exit_code" in structured:
                command["exit_code"] = integer(structured["exit_code"], "tool_use_result.exit_code", nonnegative=False)
        if failed:
            self.trajectory.emit("error", line, call_id=cid, message=f"{self.calls[cid]['name']} failed")
