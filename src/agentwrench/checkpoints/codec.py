"""Deliberately data-only use of the upstream serializer.

No pickle, imports, constructors, LangChain revivers, or MessagePack extensions.
This is narrower than JsonPlusSerializer's default allowlist.
"""
from datetime import datetime
import hashlib
import json

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

MAX_BLOB_BYTES = 16 * 1024 * 1024


def reject_extension(code, data):
    raise ValueError("MessagePack extension/object is opaque")


class DataSerializer(JsonPlusSerializer):
    def __init__(self):
        super().__init__(pickle_fallback=False, allowed_json_modules=(),
                         allowed_msgpack_modules=(), __unpack_ext_hook__=reject_extension)

    def _reviver(self, value):
        if value.get("lc") in (1, 2) and value.get("type") in ("constructor", "secret"):
            raise ValueError("JSON object revival is disabled")
        return value


SERDE = DataSerializer()


def decode(kind, blob):
    try:
        if not isinstance(blob, bytes) or len(blob) > MAX_BLOB_BYTES:
            return None, "invalid or oversized blob"
        result = SERDE.loads_typed((kind, blob))
        # Bound subsequent recursive formatting/hashing, including adversarial JSON.
        stack = [(result, 0)]
        nodes = 0
        while stack:
            item, depth = stack.pop()
            nodes += 1
            if depth > 64 or nodes > 1_000_000:
                return None, "nested value exceeds data-only safety limits"
            if isinstance(item, dict):
                stack.extend((v, depth + 1) for pair in item.items() for v in pair)
            elif isinstance(item, (list, tuple)):
                stack.extend((v, depth + 1) for v in item)
        return result, None
    except Exception:
        # Never echo decoder exceptions: they can contain private state values.
        return None, "unsupported or undecodable serialization"


def checkpoint(row):
    value, error = decode(row["type"], row["checkpoint"])
    if error:
        return None, error
    if (not isinstance(value, dict) or value.get("v") != 4
            or value.get("id") != row["checkpoint_id"]
            or not isinstance(value.get("ts"), str)
            or not isinstance(value.get("channel_values"), dict)
            or not all(isinstance(k, str) for k in value["channel_values"])):
        return None, "unsupported checkpoint payload (requires v4)"
    try:
        if datetime.fromisoformat(value["ts"]).tzinfo is None:
            return None, "checkpoint timestamp must include a timezone"
    except ValueError:
        return None, "invalid checkpoint timestamp"
    return value, None


def canonical(value):
    """Map order is immaterial; sequence order and scalar types remain significant."""
    if isinstance(value, dict):
        pairs = [(canonical(k), canonical(v)) for k, v in value.items()]
        return dict(sorted(pairs, key=lambda item: SERDE.dumps_typed(item[0])))
    if isinstance(value, (list, tuple)):
        return [canonical(item) for item in value]
    return value


def encoded(value):
    kind, data = SERDE.dumps_typed(canonical(value))
    return kind.encode() + b"\0" + data


def state_hash(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def preview(value, limit=160):
    # repr/json never evaluates user content. JSON escaping prevents terminal escapes.
    try:
        text = json.dumps(value, ensure_ascii=True, default=lambda v: repr(v))
    except (TypeError, ValueError):
        # MessagePack permits keys such as bytes; JSON's default hook only
        # handles values. Escape the data-only repr rather than discard keys.
        text = json.dumps(repr(value), ensure_ascii=True)
    return text if len(text) <= limit else text[:limit] + "..."
