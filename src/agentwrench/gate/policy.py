"""Strict, bounded policy loading and deterministic evidence-carrying rules."""
from dataclasses import asdict, dataclass
from contextlib import nullcontext
from pathlib import Path
from typing import Any, BinaryIO

import regex
import yaml

from .model import InputError, Trajectory, integer, obj, string

MAX_POLICY_BYTES = 65536
RULE_KEYS = {"require_command", "forbid_command", "forbid_file_change", "allow_file_change_only",
             "max_tool_calls", "max_commands", "max_total_tokens", "require_final_message",
             "forbid_error_events", "order", "require_success"}


class StrictLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise InputError("YAML aliases are unsupported")
        event = self.peek_event()
        if getattr(event, "anchor", None) is not None:
            raise InputError("YAML anchors are unsupported")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise InputError("policy keys must be strings")
            if key in result:
                raise InputError(f"duplicate policy key: {key}")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


@dataclass
class Rule:
    id: str
    key: str
    value: Any


@dataclass
class Result:
    id: str
    passed: bool
    message: str
    evidence: list[int]


def keys(value: Any, allowed: set[str], required: set[str], label: str) -> dict:
    value = obj(value, label)
    if value.keys() - allowed:
        raise InputError(f"{label}: unknown key {sorted(value.keys() - allowed)[0]}")
    if required - value.keys():
        raise InputError(f"{label}: missing key {sorted(required - value.keys())[0]}")
    return value


def pattern(value: Any, label: str):
    value = string(value, label)
    if len(value) > 2048:
        raise InputError(f"{label}: regex exceeds 2048 characters")
    try:
        return regex.compile(value)
    except regex.error as exc:
        raise InputError(f"{label}: invalid regex: {exc}") from exc


def matches(compiled, value: str) -> bool:
    try:
        return compiled.search(value, timeout=0.05) is not None
    except TimeoutError as exc:
        raise InputError("regex exceeded 50 ms match budget") from exc


def glob(value: Any):
    """POSIX full-path glob. ** is a whole segment; no braces or escaping."""
    value = string(value, "glob")
    if len(value) > 2048 or any(c in value for c in "\x00\\{}"):
        raise InputError(f"invalid glob: {value!r}")
    out, i = "\\A", 0
    while i < len(value):
        c = value[i]
        if c == "*":
            if value[i:i + 2] == "**":
                if (i and value[i - 1] != "/") or (i + 2 < len(value) and value[i + 2] != "/"):
                    raise InputError("invalid glob: ** must be a whole path segment")
                if i + 2 < len(value):
                    out += "(?:[^/]+/)*"
                    i += 3
                else:
                    out += ".*"
                    i += 2
                continue
            out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        elif c == "[":
            end = value.find("]", i + 1)
            if end == -1 or end == i + 1:
                raise InputError("invalid glob: unterminated or empty character class")
            chars = value[i + 1:end]
            negate = chars.startswith("!")
            chars = chars[1:] if negate else chars
            if not chars or any(x in chars for x in "[/^"):
                raise InputError("invalid glob character class")
            out += "(?!/)[" + ("^" if negate else "") + chars + "]"
            i = end
        elif c == "]":
            raise InputError("invalid glob: unmatched ]")
        else:
            out += regex.escape(c)
        i += 1
    try:
        return regex.compile(out + "\\Z", regex.DOTALL)
    except regex.error as exc:
        raise InputError(f"invalid glob: {exc}") from exc


def selector(raw: Any) -> dict:
    if isinstance(raw, str):
        raw = {"kind": "command", "pattern": raw}
    raw = keys(raw, {"kind", "pattern", "glob", "name"}, {"kind"}, "order selector")
    if not isinstance(raw["kind"], str) or raw["kind"] not in {"message", "command", "file_change", "tool_call", "tool_result", "web_search", "usage", "error", "end"}:
        raise InputError(f"order: unknown event kind {raw['kind']}")
    compiled = dict(raw)
    if "pattern" in raw:
        if raw["kind"] not in {"command", "message"}:
            raise InputError("order.pattern requires kind command or message")
        compiled["pattern"] = pattern(raw["pattern"], "order.pattern")
    if "glob" in raw:
        if raw["kind"] != "file_change":
            raise InputError("order.glob requires kind file_change")
        compiled["glob"] = glob(raw["glob"])
    if "name" in raw:
        string(raw["name"], "order.name")
    return compiled


def load_policy(path: str | Path | BinaryIO) -> list[Rule]:
    return parse_policy(read_policy(path))


def read_policy(path: str | Path | BinaryIO) -> bytes:
    """Read a file or borrowed binary stream through EOF, at most cap + 1 bytes.

    Path arguments always name files. Borrowed streams are neither sought nor
    closed, and short reads do not indicate EOF.
    """
    data = bytearray()
    with (Path(path).open("rb") if isinstance(path, (str, Path)) else nullcontext(path)) as handle:
        while True:
            chunk = handle.read(MAX_POLICY_BYTES - len(data) + 1)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_POLICY_BYTES:
                raise InputError("policy exceeds 64 KiB")
    return bytes(data)


def parse_policy(data: bytes) -> list[Rule]:
    """Parse one bounded snapshot, also usable when fingerprinting a policy."""
    if len(data) > MAX_POLICY_BYTES:
        raise InputError("policy exceeds 64 KiB")
    try:
        raw = yaml.load(data.decode("utf-8"), Loader=StrictLoader)
    except (yaml.YAMLError, UnicodeError, RecursionError) as exc:
        raise InputError(f"invalid YAML policy: {exc}") from exc
    return compile_policy(raw)


def compile_policy(raw: Any) -> list[Rule]:
    raw = keys(raw, RULE_KEYS, set(), "policy")
    if not raw:
        raise InputError("policy must contain at least one rule")
    rules = []
    for key, value in raw.items():
        if key in {"require_command", "forbid_command", "forbid_file_change"}:
            entries = value if isinstance(value, list) else [value]
            if not entries:
                raise InputError(f"{key}: expected nonempty list")
            for i, entry in enumerate(entries):
                field = "glob" if key == "forbid_file_change" else "pattern"
                allowed = {field, "exit_code"} if key == "require_command" else {field}
                entry = keys(entry, allowed, {field}, key)
                compiled = dict(entry)
                compiled[field] = glob(entry[field]) if field == "glob" else pattern(entry[field], key)
                if "exit_code" in entry:
                    integer(entry["exit_code"], "exit_code", nonnegative=False)
                rules.append(Rule(f"{key}[{i}:{entry[field]}]", key, compiled))
        elif key == "require_final_message":
            value = keys(value, {"pattern"}, {"pattern"}, key)
            rules.append(Rule(key, key, pattern(value["pattern"], key)))
        elif key == "allow_file_change_only":
            if not isinstance(value, list):
                raise InputError(f"{key}: expected list of globs")
            rules.append(Rule(key, key, [glob(g) for g in value]))
        elif key.startswith("max_"):
            rules.append(Rule(key, key, integer(value, key)))
        elif key == "order":
            if not isinstance(value, list) or not value:
                raise InputError("order: expected nonempty list of selector pairs")
            for i, pair in enumerate(value):
                if not isinstance(pair, list) or len(pair) != 2:
                    raise InputError("order: each entry must have exactly two selectors")
                rules.append(Rule(f"order[{i}]", key, [selector(x) for x in pair]))
        else:
            if type(value) is not bool:
                raise InputError(f"{key}: expected boolean")
            if value:
                rules.append(Rule(key, key, value))
    if len(rules) > 200:
        raise InputError("policy exceeds 200 rules")
    return rules


def _selected(e: dict, s: dict) -> bool:
    if e["kind"] != s["kind"]:
        return False
    if "name" in s and e.get("name") != s["name"]:
        return False
    if "pattern" in s and not matches(s["pattern"], e.get("command", e.get("text", ""))):
        return False
    if "glob" in s and not any(matches(s["glob"], c["path"]) for c in e["changes"]):
        return False
    return True


def check(t: Trajectory, rules: list[Rule], allow_incomplete: bool = False) -> dict:
    results = []
    events = t.events
    commands = [e for e in events if e["kind"] == "command"]
    files = [e for e in events if e["kind"] == "file_change"]
    usage = [e for e in events if e["kind"] == "usage"]
    ends = [e for e in events if e["kind"] == "end"]
    if not allow_incomplete:
        results.append(Result("incomplete_stream", t.complete,
                              "terminal event observed and all items resolved" if t.complete else "missing terminal event or unresolved items",
                              [e["index"] for e in events if e.get("call_id") in t.pending_calls or "codex:" + e.get("item_id", "") in t.pending_calls or e["kind"] == "end"]))
    for rule in rules:
        key, value = rule.key, rule.value
        passed, evidence, message = True, [], ""
        if key in {"require_command", "forbid_command"}:
            matching = [e for e in commands if matches(value["pattern"], e["command"])]
            if key == "require_command":
                accepted = [e for e in matching if e.get("completed") and ("exit_code" not in value or e.get("exit_code") == value["exit_code"])]
                passed = bool(accepted)
                evidence = [e["index"] for e in accepted or matching]
                message = "matching completed command found" if passed else "no matching completed command with required exit_code (missing codes do not match)"
            else:
                passed = not matching
                evidence = [e["index"] for e in matching]
                message = "no forbidden command" if passed else "forbidden command observed"
        elif key in {"forbid_file_change", "allow_file_change_only"}:
            bad, good = [], []
            for event in files:
                for change in event["changes"]:
                    path = change["path"]
                    match = matches(value["glob"], path) if key == "forbid_file_change" else any(matches(g, path) for g in value)
                    # Relative allowlists cannot bless an escape, even with **.
                    unsafe = path.startswith("../") or path == ".." or path.startswith("/")
                    denied = match if key == "forbid_file_change" else (not match or unsafe)
                    (bad if denied else good).append((event["index"], path))
            passed = not bad
            evidence = sorted({i for i, _ in (bad or good)})
            message = "file paths allowed" if passed else "forbidden file change: " + ", ".join(p for _, p in bad)
        elif key in {"max_tool_calls", "max_commands", "max_total_tokens"}:
            if key == "max_commands":
                selected, count = commands, len(commands)
            elif key == "max_tool_calls":
                calls = {}
                for e in events:
                    if e["kind"] in {"command", "file_change", "tool_call", "web_search"} and "call_id" in e:
                        calls.setdefault(e["call_id"], e)
                selected, count = list(calls.values()), len(calls)
            else:
                selected = usage
                known = bool(usage) and len(usage) == len(ends) and all(e["total_tokens"] is not None for e in usage) and t.complete
                count = sum(e["total_tokens"] for e in usage) if known else None
            passed = count is not None and count <= value
            evidence = [e["index"] for e in selected]
            message = f"observed {count if count is not None else 'unknown'}; limit {value}"
        elif key == "require_final_message":
            conversation = [e for e in events if e["kind"] in {"message", "command", "tool_call", "tool_result", "file_change", "web_search"} and not e.get("reasoning")]
            last = conversation[-1] if conversation else None
            passed = bool(last and last["kind"] == "message" and last.get("role") == "assistant" and last.get("completed") and matches(value, last["text"]))
            evidence = [last["index"]] if last else []
            message = "final assistant message matched" if passed else "final assistant message missing or pattern did not match"
        elif key == "forbid_error_events":
            errors = [e for e in events if e["kind"] == "error"]
            passed, evidence = not errors, [e["index"] for e in errors]
            message = "no error events" if passed else "error events observed"
        elif key == "require_success":
            passed = t.complete and bool(ends) and all(e["success"] for e in ends)
            evidence = [e["index"] for e in ends]
            message = "all terminal events successful" if passed else "missing or unsuccessful terminal event"
        elif key == "order":
            left, right = [[e["index"] for e in events if _selected(e, s)] for s in value]
            passed = bool(left and right and max(left) < min(right))
            evidence = sorted(set(left + right))
            message = "all A events precede all B events" if passed else "order violated or selector has no events"
        results.append(Result(rule.id, bool(passed), message, evidence))
    failed = sum(not r.passed for r in results)
    return {"schema_version": 1, "source": t.source, "passed": failed == 0,
            "summary": {"passed": len(results) - failed, "failed": failed, "total": len(results)},
            "complete": t.complete, "pending_calls": t.pending_calls,
            "ignored_count": sum(t.ignored.values()), "ignored": t.ignored,
            "results": [asdict(r) for r in results]}
