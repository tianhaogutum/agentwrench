"""Versioned retained-history byte snapshots and read-only growth gates."""
from collections import defaultdict
import json
from pathlib import Path

from .codec import checkpoint, decode, encoded
from .db import ScopeError, blob_size, key

FORMAT = "lgckpt-scope.growth/v1"
POLICY_FORMAT = "lgckpt-scope.budget-policy/v1"
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_GROUPS = 200_000
MAX_POLICY_BYTES = 1024 * 1024
MAX_OVERRIDES = 10_000
IDENTITY = ("thread_id", "checkpoint_ns")
COMPONENTS = {"threads": ("checkpoint_bytes", "write_bytes"),
              "channels": ("checkpoint_value_bytes", "write_bytes")}


def snapshot_report(cps, writes, *, thread=None):
    """Attribute all retained rows, including orphan writes, without storing values."""
    threads = defaultdict(lambda: {"checkpoint_bytes": 0, "write_bytes": 0})
    channels = defaultdict(lambda: {"checkpoint_value_bytes": 0, "write_bytes": 0})
    for row in cps:
        cp, error = checkpoint(row)
        if error:
            raise ScopeError("snapshot refused: opaque/unsupported checkpoint; use stats for partial inspection")
        group = key(row)[:2]
        threads[group]["checkpoint_bytes"] += blob_size(row, "checkpoint")
        for channel, value in cp["channel_values"].items():
            channels[(*group, channel)]["checkpoint_value_bytes"] += len(encoded(value).split(b"\0", 1)[1])
        if len(threads) + len(channels) > MAX_GROUPS:
            raise ScopeError("snapshot exceeds 200000 groups; select --thread or split offline")
    for row in writes:
        _, error = decode(row["type"], row["value"])
        if error:
            raise ScopeError("snapshot refused: opaque/unsupported write; use stats for partial inspection")
        group = key(row)[:2]
        size = blob_size(row, "value")
        threads[group]["write_bytes"] += size
        channels[(*group, row["channel"])]["write_bytes"] += size
        if len(threads) + len(channels) > MAX_GROUPS:
            raise ScopeError("snapshot exceeds 200000 groups; select --thread or split offline")
    result = {"format": FORMAT, "scope": {"thread": thread}}
    for name, groups in (("threads", threads), ("channels", channels)):
        fields = (*IDENTITY, "channel") if name == "channels" else IDENTITY
        result[name] = [{**dict(zip(fields, group)), **sizes, "bytes": sum(sizes.values())}
                        for group, sizes in sorted(groups.items())]
    # Ensure a produced snapshot can be consumed by the bounded JSON loader.
    if len(json.dumps(result, ensure_ascii=True).encode("ascii")) + 1 > MAX_SNAPSHOT_BYTES:
        raise ScopeError("snapshot exceeds 64 MiB; select --thread or split offline")
    return result


def validate_snapshot(value):
    """Reject ambiguous snapshots instead of letting missing/duplicate data pass."""
    def invalid():
        raise ScopeError("invalid growth snapshot: expected complete lgckpt-scope.growth/v1 byte accounting")

    if (not isinstance(value, dict) or set(value) != {"format", "scope", "threads", "channels"}
            or value["format"] != FORMAT or not isinstance(value["scope"], dict)
            or set(value["scope"]) != {"thread"}):
        invalid()
    selected = value["scope"]["thread"]
    if selected is not None and not isinstance(selected, str):
        invalid()
    indexed = {}
    for name, components in COMPONENTS.items():
        fields = (*IDENTITY, "channel") if name == "channels" else IDENTITY
        rows = value[name]
        if not isinstance(rows, list) or len(rows) > MAX_GROUPS:
            invalid()
        groups = indexed[name] = {}
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {*fields, *components, "bytes"}
                    or not all(isinstance(row[f], str) for f in fields)
                    or not all(type(row[f]) is int and row[f] >= 0 for f in (*components, "bytes"))
                    or row["bytes"] != sum(row[f] for f in components)
                    or (selected is not None and row["thread_id"] != selected)):
                invalid()
            group = tuple(row[f] for f in fields)
            if group in groups:
                invalid()
            groups[group] = row
    if sum(map(len, indexed.values())) > MAX_GROUPS:
        invalid()
    write_totals = defaultdict(int)
    for group, row in indexed["channels"].items():
        if group[:2] not in indexed["threads"]:
            invalid()
        write_totals[group[:2]] += row["write_bytes"]
    for group, row in indexed["threads"].items():
        if row["write_bytes"] != write_totals[group]:
            invalid()
    return indexed


def _load_json(path, *, limit, label):
    def unique_object(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError("duplicate JSON key")
            result[name] = value
        return result

    with Path(path).expanduser().open("rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ScopeError(f"{label} exceeds {limit // (1024 * 1024)} MiB input limit")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise ScopeError(f"invalid {label} JSON") from None
    return value


def load_snapshot(path):
    value = _load_json(path, limit=MAX_SNAPSHOT_BYTES, label="growth snapshot")
    validate_snapshot(value)
    return value


def validate_policy(value):
    """Require explicit defaults and unique, literal selectors for both kinds."""
    def invalid():
        raise ScopeError("invalid budget policy: expected lgckpt-scope.budget-policy/v1, "
                         "both nonnegative integer defaults and unique exact overrides "
                         "(at most 10000 combined)")

    default_fields = {"max_thread_growth_bytes", "max_channel_growth_bytes"}
    if (not isinstance(value, dict) or set(value) != {"format", "default", "threads", "channels"}
            or value["format"] != POLICY_FORMAT or not isinstance(value["default"], dict)
            or set(value["default"]) != default_fields
            or not all(type(n) is int and n >= 0 for n in value["default"].values())):
        invalid()
    indexed = {}
    for name in COMPONENTS:
        fields = (*IDENTITY, "channel") if name == "channels" else IDENTITY
        rows = value[name]
        if not isinstance(rows, list) or len(rows) > MAX_OVERRIDES:
            invalid()
        groups = indexed[name] = {}
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {*fields, "max_growth_bytes"}
                    or not all(isinstance(row[f], str) for f in fields)
                    or type(row["max_growth_bytes"]) is not int or row["max_growth_bytes"] < 0):
                invalid()
            group = tuple(row[f] for f in fields)
            if group in groups:
                invalid()
            groups[group] = row["max_growth_bytes"]
    if sum(map(len, indexed.values())) > MAX_OVERRIDES:
        invalid()
    return indexed


def load_policy(path):
    value = _load_json(path, limit=MAX_POLICY_BYTES, label="budget policy")
    validate_policy(value)
    return value


def budget_report(baseline, current, *, max_thread_growth_bytes=None, max_channel_growth_bytes=None,
                  policy=None, require_policy_match=False, show_policy_coverage=False, max_results=None,
                  show_group_presence=False, show_group_presence_summary=False, max_unused_selectors=None,
                  show_byte_components=False):
    if max_results is not None and (type(max_results) is not int or max_results < 0):
        raise ScopeError("--max-results must be a nonnegative integer")
    if max_unused_selectors is not None and (type(max_unused_selectors) is not int or max_unused_selectors < 0):
        raise ScopeError("--max-unused-selectors must be a nonnegative integer")
    if require_policy_match and policy is None:
        raise ScopeError("--require-policy-match requires --policy")
    if show_policy_coverage and policy is None:
        raise ScopeError("--show-policy-coverage requires --policy")
    if max_unused_selectors is not None and (not show_policy_coverage or policy is None):
        raise ScopeError("--max-unused-selectors requires --show-policy-coverage and --policy")
    limits = {"threads": max_thread_growth_bytes, "channels": max_channel_growth_bytes}
    overrides = None
    if policy is not None:
        if any(limit is not None for limit in limits.values()):
            raise ScopeError("--policy cannot be combined with uniform growth limit flags")
        overrides = validate_policy(policy)
        limits = {"threads": policy["default"]["max_thread_growth_bytes"],
                  "channels": policy["default"]["max_channel_growth_bytes"]}
    if all(limit is None for limit in limits.values()):
        raise ScopeError("budget requires --policy or --max-thread-growth-bytes and/or --max-channel-growth-bytes")
    if any(limit is not None and (type(limit) is not int or limit < 0) for limit in limits.values()):
        raise ScopeError("growth byte limits must be nonnegative integers")
    before, after = validate_snapshot(baseline), validate_snapshot(current)
    if baseline["scope"] != current["scope"]:
        raise ScopeError("incompatible growth snapshot scopes; capture both with the same --thread selection")
    if require_policy_match or show_policy_coverage:
        # Share exact, independent union matching between refusal and evidence.
        unused = {name: sorted(overrides[name].keys() - before[name].keys() - after[name].keys())
                  for name in COMPONENTS}
    if require_policy_match:
        for name in COMPONENTS:
            if unused[name]:
                fields = (*IDENTITY, "channel") if name == "channels" else IDENTITY
                selector = ", ".join(f"{field}={json.dumps(value, ensure_ascii=True)}"
                                     for field, value in zip(fields, unused[name][0]))
                kind = "channel" if name == "channels" else "thread"
                raise ScopeError(f"unused budget policy {kind} selector: {selector}")
    comparisons = []
    for name, default in limits.items():
        if default is None:
            continue
        fields = (*IDENTITY, "channel") if name == "channels" else IDENTITY
        for group in sorted(before[name].keys() | after[name].keys()):
            limit = overrides[name].get(group, default) if overrides is not None else default
            old = before[name].get(group, {}).get("bytes", 0)
            new = after[name].get(group, {}).get("bytes", 0)
            growth = new - old
            comparisons.append({"group": "channel" if name == "channels" else "thread",
                                **dict(zip(fields, group)), "baseline_bytes": old, "current_bytes": new,
                                "growth_bytes": growth, "limit_bytes": limit,
                                "exceeded_bytes": max(0, growth - limit)})
            if overrides is not None:
                comparisons[-1]["limit_source"] = "override" if group in overrides[name] else "default"
            if show_byte_components:
                comparisons[-1]["byte_components"] = {}
                for component in COMPONENTS[name]:
                    old_component = before[name].get(group, {}).get(component, 0)
                    new_component = after[name].get(group, {}).get(component, 0)
                    comparisons[-1]["byte_components"][component] = {
                        "baseline_bytes": old_component, "current_bytes": new_component,
                        "growth_bytes": new_component - old_component}
            if show_group_presence:
                comparisons[-1]["baseline_present"] = group in before[name]
                comparisons[-1]["current_present"] = group in after[name]
    violations = [row for row in comparisons if row["exceeded_bytes"] > 0]
    result = {"passed": not violations, "scope": baseline["scope"],
              "comparisons": comparisons, "violations": violations}
    if overrides is not None:
        result["policy_format"] = POLICY_FORMAT
    if show_group_presence_summary:
        result["group_presence_summary"] = {}
        for name, default in limits.items():
            if default is None:
                continue
            common = len(before[name].keys() & after[name].keys())
            baseline_count, current_count = len(before[name]), len(after[name])
            result["group_presence_summary"][name] = {
                "baseline": baseline_count, "current": current_count,
                "added": current_count - common, "removed": baseline_count - common,
                "common": common}
    if show_policy_coverage:
        result["policy_coverage"] = {}
        if max_unused_selectors is not None:
            result["max_unused_selectors"] = max_unused_selectors
        for name in COMPONENTS:
            fields = (*IDENTITY, "channel") if name == "channels" else IDENTITY
            total, unmatched = len(overrides[name]), len(unused[name])
            result["policy_coverage"][name] = {
                "total_overrides": total, "covered_overrides": total - unmatched,
                "unused_overrides": unmatched,
                "unused_selectors": [dict(zip(fields, group)) for group in unused[name][:max_unused_selectors]]}
            if max_unused_selectors is not None:
                shown = min(unmatched, max_unused_selectors)
                result["policy_coverage"][name]["unused_selectors_summary"] = {
                    "total": unmatched, "shown": shown, "truncated": shown < unmatched}
    if max_results is not None:
        result["max_results"] = max_results
        result["result_summary"] = {}
        for name in ("comparisons", "violations"):
            rows = result[name]
            shown = min(len(rows), max_results)
            result["result_summary"][name] = {
                "total": len(rows), "shown": shown, "truncated": shown < len(rows)}
            result[name] = rows[:max_results]
    return result
