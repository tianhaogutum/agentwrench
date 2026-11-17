"""Check an existing-file unified patch against exact allowed TypeScript symbols."""
from __future__ import annotations

import hashlib

from .baseline import directory_baseline, git_baseline  # noqa: F401  (re-exported API)
from .errors import FenceError, error_message, require_that
from .jsdiff import diff_arrays
from .patch import apply_exactly, is_typescript, parse_candidate, safe_relative, utf8_length
from .syntax import (list_symbols, parse_source, resolve_symbol, same_outside_text,  # noqa: F401
                     source_range, syntax_events, syntax_fingerprint, trivia_events)

SCHEMA_VERSION = 1
MAX_ALLOW_ENTRIES = 1000
MAX_TOTAL_SOURCE_BYTES = 16 * 1024 * 1024
DIAGNOSTIC_LIMIT = 100
DIFF_TIMEOUT_SECONDS = 1.0
DIFF_MAX_EDIT_LENGTH = 10_000


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def parse_selector(value) -> dict:
    require_that(isinstance(value, str), "invalid_allow",
                 "Allow entries must be strings of the form path.ts::qualified.symbol")
    separator = value.find("::")
    require_that(separator > 0 and separator + 2 < len(value), "invalid_allow",
                 f"Invalid allow entry: {_json(value)}")
    file = safe_relative(value[:separator])
    require_that(is_typescript(file), "invalid_allow", f"Only TypeScript symbols can be allowed: {file}")
    return {"file": file, "selector": value[separator + 2:], "value": value}


def _json(value) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def _violation(event, parsed, file: str, side: str, reason: str) -> dict:
    return {"file": file, "side": side,
            "reason": "outside_comment_boundary" if event.category == "comment_boundary" else reason,
            "kind": event.kind, "range": source_range(parsed, event.start, event.end),
            "category": event.category, "text": event.text}


def _describe_changes(before, after, old_parsed, new_parsed, file: str, reason: str, limit: int = DIAGNOSTIC_LIMIT):
    differences = diff_arrays([event.key for event in before], [event.key for event in after],
                              timeout=DIFF_TIMEOUT_SECONDS, max_edit_length=DIFF_MAX_EDIT_LENGTH)
    if differences is None:
        index = 0
        while index < len(before) and index < len(after) and before[index].key == after[index].key:
            index += 1
        changes = []
        for events, parsed, side in ((before, old_parsed, "baseline"), (after, new_parsed, "candidate")):
            if index < len(events):
                changes.append(_violation(events[index], parsed, file, side, reason))
        return {"changes": changes, "total": len(changes), "truncated": True, "countIsLowerBound": True,
                "diagnosticLimit": "Detailed diff exceeded its time/edit limit; first incompatible events are shown"}
    result = []
    total = 0
    seen = set()
    old_position = new_position = 0
    for part in differences:
        if not part.added and not part.removed:
            old_position += part.count
            new_position += part.count
            continue
        if part.added:
            events, side, parsed = after[new_position:new_position + part.count], "candidate", new_parsed
            new_position += part.count
        else:
            events, side, parsed = before[old_position:old_position + part.count], "baseline", old_parsed
            old_position += part.count
        for event in events:
            key = (side, event.kind, event.start, event.end, event.category)
            if key in seen:
                continue
            seen.add(key)
            total += 1
            if len(result) < limit:
                result.append(_violation(event, parsed, file, side, reason))
    return {"changes": result, "total": total, "truncated": total > len(result)}


# Identical outside CSTs align every child boundary. Compare the whitespace at
# those boundaries directly instead of running a quadratic diff on a reformat.
def _describe_whitespace(before, after, old_selected, new_selected, file: str, limit: int = DIAGNOSTIC_LIMIT):
    old_gaps = trivia_events(before, old_selected)
    new_gaps = trivia_events(after, new_selected)
    changes = []
    total = 0

    def add(event, parsed, side):
        nonlocal total
        total += 1
        if len(changes) < limit:
            changes.append(_violation(event, parsed, file, side, "outside_whitespace"))

    i = j = 0
    while i < len(old_gaps) or j < len(new_gaps):
        left = old_gaps[i] if i < len(old_gaps) else None
        right = new_gaps[j] if j < len(new_gaps) else None
        if left is not None and right is not None and left.gap_index == right.gap_index:
            if left.key != right.key:
                add(left, before, "baseline")
                add(right, after, "candidate")
            i += 1
            j += 1
        elif left is not None and (right is None or left.gap_index < right.gap_index):
            add(left, before, "baseline")
            i += 1
        else:
            add(right, after, "candidate")
            j += 1
    return {"changes": changes, "total": total, "truncated": total > len(changes)}


def _identity(read_base):
    identity = getattr(read_base, "identity", None)
    return {"kind": "reader"} if identity is None else identity


def _unique(values) -> list:
    """Order-preserving de-duplication (JavaScript ``new Set``) that tolerates unhashable entries."""
    result = []
    seen = set()
    for value in values:
        try:
            marker = (type(value), value)
            if marker in seen:
                continue
            seen.add(marker)
        except TypeError:
            if any(value is other for other in result):
                continue
        result.append(value)
    return result


def check_patch(*, patch, allow=(), read_base, ignore_formatting: bool = False) -> dict:
    """Check an existing-file unified patch without writing it anywhere.

    ``read_base(file)`` is a synchronous, trusted baseline reader returning UTF-8
    text; an optional ``identity`` attribute is recorded as evidence. A result
    with ``ok: False`` is a scope rejection. Invalid evidence raises FenceError.
    """
    require_that(isinstance(allow, (list, tuple)), "invalid_allow", "allow must be an array")
    require_that(len(allow) <= MAX_ALLOW_ENTRIES, "invalid_allow", "At most 1000 allow entries are supported")
    require_that(callable(read_base), "invalid_baseline", "A baseline reader is required")
    require_that(isinstance(ignore_formatting, bool), "invalid_options", "ignoreFormatting must be boolean")
    files = parse_candidate(patch)
    selectors = [parse_selector(value) for value in _unique(allow)]
    selectors_by_file: dict[str, list[str]] = {}
    for item in selectors:
        selectors_by_file.setdefault(item["file"], []).append(item["selector"])
    patches_by_file = {file.path: file for file in files}
    total_source_bytes = 0

    def read(file: str):
        nonlocal total_source_bytes
        try:
            source = read_base(file)
        except FenceError:
            raise
        except Exception as error:  # a custom reader's failure is invalid evidence
            raise FenceError("baseline_read", f"Cannot read baseline {file}: {error_message(error)}") from None
        require_that(isinstance(source, str), "invalid_source", f"Baseline reader must return text for {file}")
        total_source_bytes += utf8_length(source)
        require_that(total_source_bytes <= MAX_TOTAL_SOURCE_BYTES, "source_limit", "Total baseline input exceeds 16 MiB")
        return parse_source(source, file)

    report = {
        "schemaVersion": SCHEMA_VERSION,
        "ok": True,
        "evidence": {"patchSha256": _sha256(patch),
                     "baseline": _identity(read_base)},
        "policy": "ignore-whitespace" if ignore_formatting else "exact-outside-text",
        "allowed": [item["value"] for item in selectors],
        "files": [],
        "accepted": [],
        "violations": [],
        "ignoredFormatting": [],
    }
    # Process one file at a time, including unchanged permission files, so that
    # only one baseline/candidate pair of trees is alive at once.
    file_names = dict.fromkeys([*patches_by_file, *selectors_by_file])
    for file_name in file_names:
        file = patches_by_file.get(file_name)
        if not is_typescript(file_name):
            report["violations"].append({"file": file_name, "reason": "unsupported_file",
                                         "message": "Changed file is not TypeScript and cannot be authorized "
                                                    "by a symbol allowlist"})
            report["files"].append({"file": file_name, "status": "rejected"})
            continue
        before = read(file_name)
        old_selected = [resolve_symbol(before, selector) for selector in selectors_by_file.get(file_name, [])]
        ordered = sorted(old_selected, key=lambda symbol: (symbol["node"].start_byte, -symbol["node"].end_byte))
        for previous, current in zip(ordered, ordered[1:]):
            require_that(current["node"].start_byte >= previous["node"].end_byte, "overlapping_allow",
                         f"Overlapping allow entries in {file_name}; keep only the enclosing symbol")
        if file is None:
            continue  # Still validate permissions in unchanged files.
        candidate = apply_exactly(before.source, file)
        after = parse_source(candidate, file.path)
        new_selected = []
        for symbol in old_selected:
            try:
                new_selected.append(resolve_symbol(after, symbol["selector"]))
            except FenceError as error:
                raise FenceError("symbol_disappeared",
                                 f"Allowed symbol {file.path}::{symbol['selector']} was removed, renamed or became "
                                 "ambiguous; those changes require separate review", {"cause": error.code}) from None
        structurally_equal = syntax_fingerprint(before, old_selected) == syntax_fingerprint(after, new_selected)
        exactly_equal = same_outside_text(before, after, old_selected, new_selected)
        outside = None
        if not structurally_equal:
            outside = _describe_changes(syntax_events(before, old_selected), syntax_events(after, new_selected),
                                        before, after, file.path, "outside_syntax")
        elif not exactly_equal and not ignore_formatting:
            outside = _describe_whitespace(before, after, old_selected, new_selected, file.path)
        if outside is not None:
            report["violations"].extend(outside["changes"])
        if structurally_equal and not exactly_equal and ignore_formatting:
            report["ignoredFormatting"].append({"file": file.path, "reason": "outside_whitespace"})
        for old_symbol, new_symbol in zip(old_selected, new_selected):
            if before.text(old_symbol["node"]) != after.text(new_symbol["node"]):
                report["accepted"].append({"file": file.path, "selector": old_symbol["selector"],
                                           "kind": old_symbol["kind"], "baseline": old_symbol["range"],
                                           "candidate": new_symbol["range"]})
        entry = {
            "file": file.path, "status": "rejected" if outside is not None else "accepted",
            "baselineSha256": _sha256(before.source), "candidateSha256": _sha256(candidate),
            "outsideChanges": outside["total"] if outside is not None else 0,
            "truncated": outside["truncated"] if outside is not None else False,
        }
        if outside is not None and outside.get("diagnosticLimit"):
            entry["countIsLowerBound"] = True
            entry["diagnosticLimit"] = outside["diagnosticLimit"]
        report["files"].append(entry)
    report["ok"] = len(report["violations"]) == 0
    return report


__all__ = ["FenceError", "check_patch", "directory_baseline", "git_baseline", "list_symbols", "parse_selector",
           "parse_source"]
