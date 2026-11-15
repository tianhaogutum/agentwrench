"""Pure reporting over a stable SQLite snapshot."""
from collections import defaultdict
from datetime import datetime

from .codec import checkpoint, decode, encoded, preview
from .db import ScopeError, blob_size, key


def sizes(cps, writes):
    by_key = defaultdict(int)
    for row in cps:
        by_key[key(row)] += blob_size(row, "checkpoint")
    for row in writes:
        by_key[key(row)] += blob_size(row, "value")
    return by_key


def threads_report(cps, writes):
    groups = {}
    for row in [*cps, *writes]:
        groups.setdefault(row["thread_id"], {"thread_id": row["thread_id"], "checkpoint_ns": set(),
                                             "checkpoints": 0, "writes": 0, "bytes": 0,
                                             "metadata_bytes": 0, "opaque_checkpoints": 0,
                                             "first": None, "last": None})
        groups[row["thread_id"]]["checkpoint_ns"].add(row["checkpoint_ns"])
    for row in cps:
        group = groups[row["thread_id"]]
        group["checkpoints"] += 1
        group["bytes"] += blob_size(row, "checkpoint")
        group["metadata_bytes"] += blob_size(row, "metadata")
        cp, error = checkpoint(row)
        if error:
            group["opaque_checkpoints"] += 1
        else:
            ts = cp["ts"]
            group["first"] = min(group["first"], ts, key=datetime.fromisoformat) if group["first"] else ts
            group["last"] = max(group["last"], ts, key=datetime.fromisoformat) if group["last"] else ts
    for row in writes:
        group = groups[row["thread_id"]]
        group["writes"] += 1
        group["bytes"] += blob_size(row, "value")
    for group in groups.values():
        group["checkpoint_ns"] = sorted(group["checkpoint_ns"])
    return sorted(groups.values(), key=lambda g: g["thread_id"])


def stats_report(cps, writes, top=10):
    channels = defaultdict(lambda: {"checkpoint_value_bytes": 0, "write_bytes": 0, "observations": 0})
    opaque_cp, opaque_writes, opaque_bytes = 0, 0, 0
    totals = sizes(cps, writes)
    growth, largest, previous = [], [], {}
    for row in cps:
        cp, error = checkpoint(row)
        if error:
            opaque_cp += 1
            opaque_bytes += blob_size(row, "checkpoint")
        else:
            for channel, value in cp["channel_values"].items():
                # serialized value size excludes the serializer type tag and map key.
                channels[channel]["checkpoint_value_bytes"] += len(encoded(value).split(b"\0", 1)[1])
                channels[channel]["observations"] += 1
        identity = {k: row[k] for k in ("thread_id", "checkpoint_ns", "checkpoint_id")}
        size = totals[key(row)]
        group = key(row)[:2]
        growth.append({**identity, "bytes": size,
                       "growth_bytes": None if group not in previous else size - previous[group]})
        previous[group] = size
        largest.append({**identity, "bytes": size})
    for row in writes:
        _, error = decode(row["type"], row["value"])
        if error:
            opaque_writes += 1
            opaque_bytes += blob_size(row, "value")
        channels[row["channel"]]["write_bytes"] += blob_size(row, "value")
    ranked = [{"channel": name, **data, "bytes": data["checkpoint_value_bytes"] + data["write_bytes"]}
              for name, data in channels.items()]
    return {"checkpoints": len(cps), "writes": len(writes), "bytes": sum(totals.values()),
            "metadata_bytes": sum(blob_size(r, "metadata") for r in cps),
            "opaque_checkpoints": opaque_cp, "opaque_writes": opaque_writes, "opaque_bytes": opaque_bytes,
            "channels": sorted(ranked, key=lambda r: (-r["bytes"], r["channel"]))[:top],
            "growth_per_step": growth,
            "largest_checkpoints": sorted(largest, key=lambda r: (-r["bytes"], r["thread_id"], r["checkpoint_ns"], r["checkpoint_id"]))[:top]}


def diff_report(cps, *, namespace="", from_id=None, to_id=None):
    rows = [r for r in cps if r["checkpoint_ns"] == namespace]
    if from_id is None:
        if len(rows) < 2:
            raise ScopeError("diff needs at least two checkpoints in the selected namespace", 1)
        before, after = rows[-2:]
    else:
        by_id = {r["checkpoint_id"]: r for r in rows}
        if from_id not in by_id or to_id not in by_id:
            raise ScopeError("checkpoint id not found in selected thread/namespace", 1)
        before, after = by_id[from_id], by_id[to_id]
    old, old_error = checkpoint(before)
    new, new_error = checkpoint(after)
    if old_error or new_error:
        return {"from": before["checkpoint_id"], "to": after["checkpoint_id"],
                "opaque": True, "error": "cannot diff opaque checkpoint", "changes": []}
    old, new = old["channel_values"], new["channel_values"]
    changes = []
    for channel in sorted(old.keys() | new.keys()):
        if channel not in old:
            change = {"channel": channel, "kind": "added", "after": preview(new[channel])}
            if isinstance(new[channel], list):
                change["summary"] = f"+{len(new[channel])} items"
        elif channel not in new:
            change = {"channel": channel, "kind": "removed", "before": preview(old[channel])}
            if isinstance(old[channel], list):
                change["summary"] = f"-{len(old[channel])} items"
        elif encoded(old[channel]) != encoded(new[channel]):
            change = {"channel": channel, "kind": "changed", "before": preview(old[channel]), "after": preview(new[channel])}
            a, b = old[channel], new[channel]
            if isinstance(a, list) and isinstance(b, list):
                delta = len(b) - len(a)
                change["summary"] = f"{delta:+d} items"
                change["appended"] = len(b) >= len(a) and encoded(a) == encoded(b[:len(a)])
        else:
            continue
        changes.append(change)
    return {"from": before["checkpoint_id"], "to": after["checkpoint_id"], "opaque": False, "changes": changes}
