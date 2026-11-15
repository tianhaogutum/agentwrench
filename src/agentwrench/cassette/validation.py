"""Machine-readable validation report v1, independent of cassette versioning."""
from __future__ import annotations

import re

from .format import CassetteError, cassette_diagnostics, read_cassette, validation_issue

REPORT_VERSION = 1


def validation_report(path, *, frame=None):
    """Return (report, exit status): 0 complete, 1 invalid, 2 unreadable JSON.

    Schema/unsupported-version errors are invalid (1). Files that cannot be read
    or parsed within the size limit produce a report with unknown counts (2).
    Complete means envelope/ID integrity, not method-specific MCP conformance
    or support for replaying server-initiated requests.

    With a frame selector, add the exact stored event and its matching peer.
    Read and validate one snapshot; never create a replayer. Schema errors
    leave both null. Invalid selectors exit 2 with a structured diagnostic.
    """
    report = {"report_version": REPORT_VERSION, "cassette_version": None,
              "valid": False, "event_count": None, "diagnostics": []}
    if frame is not None:
        report.update(frame=None, peer=None)
    try:
        data = read_cassette(path)
    except CassetteError as exc:
        report["diagnostics"] = [{"code": exc.code, "message": str(exc), "location": exc.location}]
        return report, 2
    if isinstance(data, dict):
        version = data.get("version")
        if type(version) is int:
            report["cassette_version"] = version
        if isinstance(data.get("events"), list):
            report["event_count"] = len(data["events"])
    pairs = {} if frame is not None else None
    report["diagnostics"] = list(cassette_diagnostics(data, require_complete=True, pairs=pairs))
    report["valid"] = not report["diagnostics"]
    if frame is not None and not any(d["code"] in {"invalid_schema", "unsupported_version"}
                                     for d in report["diagnostics"]):
        # Check decimal syntax and length before conversion, including selectors
        # beyond Python's integer-string digit limit. Leading zeros are allowed.
        text = str(frame)
        normalized = text.lstrip("0") or "0"
        count = report["event_count"]
        if (re.fullmatch(r"[0-9]+", text) is None or len(normalized) > len(str(count))
                or int(normalized) >= count):
            report["valid"] = False
            report["diagnostics"].append(validation_issue("invalid_frame_index",
                "frame index must be a nonnegative decimal integer less than event_count", "/events"))
            return report, 2
        index = int(normalized)

        def selected(index):
            return {"frame_index": index, "path": f"/events/{index}", "event": data["events"][index]}

        report["frame"] = selected(index)
        if index in pairs:
            report["peer"] = selected(pairs[index])
    return report, 0 if report["valid"] else 1
