"""Index exchanges by typed id; deterministic, one-use FIFO matching."""

from __future__ import annotations

from collections import Counter, defaultdict, deque
import copy
import json
from dataclasses import dataclass, field
from itertools import islice

from .format import CassetteError, canonical, id_key, redact, validate_cassette, validate_message
from .faults import validate_fault_scenario


MAX_DIAGNOSTIC_SAMPLES = 16
MAX_SUMMARY_BYTES = 4096


def _summary(label, total, details):
    """Keep whole escaped details, reserving space for exact omission counts."""
    prefix = f"{label} interactions ({total}): "
    shown = []
    for detail in islice(details, MAX_DIAGNOSTIC_SAMPLES):
        omitted = total - len(shown) - 1
        suffix = f"; ... {omitted} omitted" if omitted else ""
        if len((prefix + "; ".join([*shown, detail]) + suffix).encode("utf-8")) > MAX_SUMMARY_BYTES:
            break
        shown.append(detail)
    omitted = total - len(shown)
    suffix = f"; ... {omitted} omitted" if omitted else ""
    return prefix + "; ".join(shown) + suffix


def progress_token(msg):
    """Read only request metadata; None means absent, never a valid token."""
    meta = msg.get("params", {}).get("_meta", {})
    if "id" not in msg or "method" not in msg or "progressToken" not in meta:
        return None
    token = meta["progressToken"]
    if type(token) not in (str, int):
        raise CassetteError("params._meta.progressToken must be a string or integer")
    return token


def binds_progress(msg):
    # Initialize retains its special matching; Tasks remain opaque transcripts.
    return ("id" in msg and msg["method"] != "initialize"
            and not msg["method"].startswith("tasks/") and "task" not in msg.get("params", {}))


@dataclass
class Interaction:
    seq: int
    request: dict
    response: dict | None = None
    output: list[dict] = field(default_factory=list)
    used: bool = False


class Replayer:
    def __init__(self, cassette, *, strict=False, match="params", warn=lambda message: None,
                 fault_scenario=None):
        validate_cassette(cassette)
        if cassette["incomplete"]:
            raise CassetteError("incomplete cassette cannot be replayed; record a complete session")
        if match not in {"params", "sequence"}:
            raise CassetteError("match must be params or sequence")
        self.strict, self.match, self.warn = strict, match, warn
        self.fault_scenario = (validate_fault_scenario(fault_scenario, cassette)
                               if fault_scenario is not None else None)
        self.fault_triggered = False
        self.paths = cassette["redact_paths"]
        self.interactions: list[Interaction] = []
        self.queues = defaultdict(deque)
        self.startup = []
        self.unmatched = []
        self.unmatched_count = 0
        pending = {}
        progress_pending = {}
        last_trigger = None
        for event in cassette["events"]:
            msg = event["message"]
            if event["direction"] == "client" and "method" in msg:
                item = Interaction(event["seq"], msg)
                self.interactions.append(item)
                self.queues[self.key(msg)].append(item)
                last_trigger = item
                if "id" in msg:
                    pending[id_key(msg["id"])] = item
                    token = progress_token(msg)
                    if token is not None:
                        token_key = id_key(token)
                        if token_key in progress_pending:
                            raise CassetteError(f"ambiguous progressToken: duplicate active token at seq={item.seq}")
                        progress_pending[token_key] = item
            elif event["direction"] == "server" and "method" not in msg:
                item = pending.pop(id_key(msg["id"]))
                item.response = msg
                item.output.append(msg)
                token = progress_token(item.request)
                if token is not None:
                    del progress_pending[id_key(token)]
            elif event["direction"] == "server" and "id" in msg:
                warn("server-initiated requests are recorded but unsupported for replay")
                raise CassetteError("cannot replay server-initiated requests (sampling/elicitation/roots)")
            elif event["direction"] == "server":
                if msg["method"] == "notifications/progress":
                    token = msg.get("params", {}).get("progressToken")
                    if type(token) not in (str, int):
                        raise CassetteError(f"notifications/progress requires a string or integer progressToken at seq={event['seq']}")
                    owner = progress_pending.get(id_key(token))
                    if owner is None:
                        raise CassetteError(f"orphan notifications/progress: no active request for progressToken at seq={event['seq']}")
                else:
                    owner = next(reversed(pending.values())) if pending else last_trigger
                (owner.output if owner else self.startup).append(msg)

    def key(self, msg):
        token = progress_token(msg)
        message = redact(msg, self.paths)
        if binds_progress(msg) and token is not None:
            # Preserve presence and all other metadata, even an otherwise empty
            # _meta. The stored token value is only a per-interaction binding.
            message["params"]["_meta"]["progressToken"] = None
        # Identity/capability differences are expected across SDK clients.
        params = None if message["method"] == "initialize" else message.get("params", {})
        return ("id" in message, message["method"], canonical(params))

    def error(self, msg, reason, code=-32001):
        method = json.dumps(msg["method"][:160], ensure_ascii=True)
        diagnostic = f"{reason}: method={method} (params omitted)"
        self.unmatched_count += 1
        if len(self.unmatched) < MAX_DIAGNOSTIC_SAMPLES:
            self.unmatched.append(diagnostic)
        self.warn(diagnostic)
        if "id" not in msg:
            return []
        return [{"jsonrpc": "2.0", "id": msg["id"], "error": {"code": code, "message": diagnostic}}]

    def handle(self, msg):
        if self.fault_triggered:
            raise CassetteError("fault scenario has terminated replay")
        validate_message(msg)
        if "method" not in msg:
            raise CassetteError("client responses are unsupported during replay")
        key = self.key(msg)
        if self.match == "sequence":
            item = next((i for i in self.interactions if not i.used), None)
            if item is not None and self.key(item.request) != key:
                item = None
        else:
            queue = self.queues.get(key)
            item = queue[0] if queue else None
        if item is None:
            if (msg["method"] == "ping" and "id" in msg and progress_token(msg) is None
                    and not any(not i.used and i.request["method"] == "ping"
                                and progress_token(i.request) is not None for i in self.interactions)):
                return [{"jsonrpc": "2.0", "id": msg["id"], "result": {}}]
            reason = "unmatched request/notification"
            if binds_progress(msg):
                reason += "; params and progressToken presence must match"
            return self.error(msg, reason)
        if msg["method"] == "initialize":
            incoming = msg.get("params", {}).get("protocolVersion")
            recorded = item.request.get("params", {}).get("protocolVersion")
            result = (item.response or {}).get("result", {})
            negotiated = result.get("protocolVersion", recorded) if isinstance(result, dict) else recorded
            if incoming != recorded or incoming != negotiated:
                if self.strict:
                    return self.error(msg, "protocolVersion mismatch", code=-32002)
                self.warn("protocolVersion mismatch: returning the recorded initialize response")
        item.used = True
        self.queues[key].popleft()
        if self.fault_scenario is not None and item.seq == self.fault_scenario["request_seq"]:
            self.fault_triggered = True
            self.warn(f"transport fault triggered: kind={self.fault_scenario['kind']} request_seq={item.seq}")
            return []
        output = copy.deepcopy(item.output)
        for message in output:
            if "method" not in message:
                message["id"] = msg["id"]
            elif message["method"] == "notifications/progress" and binds_progress(msg):
                message["params"]["progressToken"] = progress_token(msg)
        return output

    @property
    def unused(self):
        return list(self._unused_details())

    def _unused_details(self):
        return (f"seq={i.seq} method={json.dumps(i.request['method'][:160], ensure_ascii=True)}"
                for i in self.interactions if not i.used)

    @property
    def unused_count(self):
        return sum(not i.used for i in self.interactions)

    def finish(self):
        unused = self.unused_count
        missing_fault = self.fault_scenario is not None and not self.fault_triggered
        if missing_fault:
            self.warn("fault scenario was not triggered")
        if self.unmatched_count:
            self.warn(_summary("unmatched", self.unmatched_count, self.unmatched))
        if unused:
            self.warn(_summary("unused", unused, self._unused_details()))
        return 1 if missing_fault or (self.strict and (self.unmatched_count or unused)) else 0


def inspect_cassette(cassette):
    methods, tools = Counter(), Counter()
    server_requests = 0
    for event in cassette["events"]:
        msg = event["message"]
        if event["direction"] == "client" and "method" in msg:
            methods[msg["method"]] += 1
            if msg["method"] == "tools/call":
                name = msg.get("params", {}).get("name", "<missing>")
                tools[name if isinstance(name, str) else "<invalid>"] += 1
        if event["direction"] == "server" and "method" in msg and "id" in msg:
            server_requests += 1
    return {"version": cassette["version"], "incomplete": cassette["incomplete"],
            "events": len(cassette["events"]), "methods": dict(sorted(methods.items())),
            "tools": dict(sorted(tools.items())), "server_requests": server_requests}
