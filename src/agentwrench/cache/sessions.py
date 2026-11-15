"""Pool independent session captures and rank earliest breaking paths by prefix loss."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .analyze import analyze, ordered_counts
from .capture import FORMATS, read_capture
from .errors import CacheError, FsError
from .jscompat import quote, utf16_key


@dataclass(frozen=True)
class SessionLimits:
    files: int = 32
    records: int = 20000


SESSION_LIMITS = SessionLimits()

METRIC = "mean shared serialized-prefix bytes / previous-request bytes across all within-session pairs"
NOTE = (
    "Offline diagnostic estimate, not measured cache savings. Each positive prefix loss is attributed only to "
    "its earliest divergent path; later changes are not enumerated."
)


def _increment(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def _by_loss(item: dict[str, Any]) -> tuple[int, int, bytes]:
    return (-item["unsharedBytes"], -item["pairs"], utf16_key(item["path"]))


@dataclass
class _Group:
    path: str
    pairs: int = 0
    sessions: int = 0
    unshared_bytes: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    contributors: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class _Totals:
    counts: dict[str, int] = field(default_factory=dict)
    paths_by_loss: dict[str, _Group] = field(default_factory=dict)
    pairs: int = 0
    stability_sum: float = 0.0
    breaking_pairs: int = 0
    usage_breaks: int = 0
    usage_compared_pairs: int = 0


def _add_session(totals: _Totals, report: dict[str, Any], path: str) -> dict[str, Any]:
    totals.pairs += report["pairs"]
    totals.usage_breaks += report["usageBreaks"]
    breaking_pairs = usage_compared_pairs = 0
    counts: dict[str, int] = {}
    for row in report["rows"]:
        # Configured rows retain original usage; count evidence before loss filtering.
        if row["usage"] is not None:
            usage_compared_pairs += 1
        totals.stability_sum += row["stability"]
        _increment(totals.counts, row["class"])
        # Observed classes also include zero-loss pairs that cannot enter rankings.
        _increment(counts, row["class"])
        unshared = row["previousBytes"] - row["sharedBytes"]
        if not row["firstDivergence"] or unshared == 0:
            continue
        breaking_pairs += 1
        pointer = row["firstDivergence"]["path"]
        group = totals.paths_by_loss.setdefault(pointer, _Group(pointer))
        group.pairs += 1
        group.unshared_bytes += unshared
        _increment(group.counts, row["class"])
        # One compact entry per (divergent pointer, supplied file label), not per row.
        if path not in group.contributors:
            group.contributors[path] = {"path": path, "pairs": 0, "unsharedBytes": 0, "counts": {}}
            group.sessions += 1
        contributor = group.contributors[path]
        contributor["pairs"] += 1
        contributor["unsharedBytes"] += unshared
        _increment(contributor["counts"], row["class"])
    totals.breaking_pairs += breaking_pairs
    totals.usage_compared_pairs += usage_compared_pairs
    return {
        "stability": report["stability"],
        "breakingPairs": breaking_pairs,
        "usageBreaks": report["usageBreaks"],
        "usageComparedPairs": usage_compared_pairs,
        "classCounts": ordered_counts(counts),
    }


def _summary(totals: _Totals) -> dict[str, Any]:
    groups = [
        {"path": g.path, "pairs": g.pairs, "sessions": g.sessions, "unsharedBytes": g.unshared_bytes, "group": g}
        for g in totals.paths_by_loss.values()
    ]
    ranked = []
    for rank, item in enumerate(sorted(groups, key=_by_loss), start=1):
        group: _Group = item["group"]
        contributors = sorted(group.contributors.values(), key=_by_loss)
        ranked.append(
            {
                "rank": rank,
                "path": group.path,
                "pairs": group.pairs,
                "sessions": group.sessions,
                "unsharedBytes": group.unshared_bytes,
                "classCounts": ordered_counts(group.counts),
                "contributors": [
                    {
                        "path": c["path"],
                        "pairs": c["pairs"],
                        "unsharedBytes": c["unsharedBytes"],
                        "classCounts": ordered_counts(c["counts"]),
                    }
                    for c in contributors
                ],
            }
        )
    return {
        "stability": totals.stability_sum / totals.pairs if totals.pairs else None,
        "breakingPairs": totals.breaking_pairs,
        "usageBreaks": totals.usage_breaks,
        "usageComparedPairs": totals.usage_compared_pairs,
        "classCounts": ordered_counts(totals.counts),
        "rankedPaths": ranked,
    }


def analyze_sessions(paths: Any, format: str = "auto", volatile_fields: Any = ()) -> dict[str, Any]:
    """Analyze each file as one independent session and pool the pairs.

    Paths are sorted before accumulating floats so argument order cannot change
    either scores or tie-breaking in the report.
    """
    if (
        not isinstance(paths, (list, tuple))
        or not paths
        or len(paths) > SESSION_LIMITS.files
        or any(not isinstance(p, str) or not p for p in paths)
    ):
        raise CacheError(f"sessions requires 1..{SESSION_LIMITS.files} capture paths")
    if format not in FORMATS:
        raise CacheError("unknown session format")
    inputs: list[tuple[str, str]] = []
    sources: set[str] = set()
    for path in sorted(paths, key=utf16_key):
        try:
            # realpath(3) semantics: os.stat rejects "file/" like the C call does,
            # which os.path.realpath alone would silently normalize away.
            os.stat(path)
            source = os.path.realpath(path, strict=True)
        except OSError as error:
            raise FsError(error, "realpath", path) from None
        if source in sources:
            raise CacheError("duplicate resolved session path")
        sources.add(source)
        inputs.append((path, source))

    sessions = []
    raw, configured = _Totals(), _Totals()
    requests = 0
    volatility = None
    for path, source in inputs:
        # Parse/analyze one bounded capture at a time; retain only compact summaries.
        try:
            records = read_capture(source, format)
            if requests + len(records) > SESSION_LIMITS.records:
                raise CacheError(f"sessions exceeds {SESSION_LIMITS.records} records in total")
            report = analyze(records, volatile_fields)
        except CacheError as error:
            raise CacheError(f"session {quote(path)}: {error.message}") from None
        requests += report["requests"]
        session = {
            "path": path,
            "format": report["format"],
            "requests": report["requests"],
            "pairs": report["pairs"],
            **_add_session(raw, report, path),
            "ignoredFields": report["ignoredFields"],
        }
        if "configured" in report:
            session["configured"] = _add_session(configured, report["configured"], path)
            session["volatility"] = {"fields": report["volatility"]["fields"]}
            volatility = {"note": report["volatility"]["note"]}
        sessions.append(session)
    raw_summary = _summary(raw)
    ranked_paths = raw_summary.pop("rankedPaths")
    result: dict[str, Any] = {
        "schemaVersion": 1,
        "metric": METRIC,
        "note": NOTE,
        "sessionCount": len(sessions),
        "requests": requests,
        "pairs": raw.pairs,
        **raw_summary,
        "sessions": sessions,
        "rankedPaths": ranked_paths,
    }
    if volatility:
        result["configured"] = _summary(configured)
        result["volatility"] = volatility
    return result
