"""Versioned, payload-free audit metadata. Checkpoints are trusted local files."""
from dataclasses import asdict

from .engine import Event, Issue, Node, OUTPUTS, PAIRS, Report, usable_id


VERSION = 2


def relevant_lines(report):
    return ({event.line for event in report.events} |
            {node.line for node in report.nodes.values()} |
            {issue.line for issue in report.issues})


def pack(report, offsets, terminated):
    return {"format": report.format, "lines": report.lines, "terminated": terminated,
            "events": [asdict(event) for event in report.events],
            "nodes": {uid: asdict(node) for uid, node in report.nodes.items()},
            "issues": [asdict(issue) for issue in report.issues],
            "locations": [[line, *offsets[line]] for line in sorted(relevant_lines(report))]}


def unpack(value, file, requested_format, offset):
    """Strictly check shapes/types/references before letting metadata reach pair()."""
    def require(condition):
        if not condition:
            raise ValueError("invalid pairing state")

    def natural(number):
        return type(number) is int and number >= 0

    def line(number):
        return natural(number) and 1 <= number <= value["lines"]

    def nullable_id(identifier):
        return identifier is None or usable_id(identifier)

    require(isinstance(value, dict) and set(value) == {
        "format", "lines", "terminated", "events", "nodes", "issues", "locations"})
    require(value["format"] in ("empty", "codex", "claude"))
    require(natural(value["lines"]) and type(value["terminated"]) is bool)
    require((value["lines"] == 0) == (offset == 0))
    require(offset > 0 or value["terminated"])
    require(value["format"] != "empty" or requested_format == "auto" and offset == 0)
    require(requested_format == "auto" or value["format"] == requested_format)
    require(isinstance(value["events"], list) and isinstance(value["nodes"], dict) and
            isinstance(value["issues"], list) and isinstance(value["locations"], list))
    report = Report(file, value["format"], lines=value["lines"])
    for uid, node in value["nodes"].items():
        require(usable_id(uid) and isinstance(node, dict) and
                set(node) == {"line", "parent", "session", "sidechain"})
        require(line(node["line"]) and nullable_id(node["parent"]) and
                nullable_id(node["session"]) and type(node["sidechain"]) is bool)
        report.nodes[uid] = Node(**node)
    require(len({node.line for node in report.nodes.values()}) == len(report.nodes))
    for event in value["events"]:
        require(isinstance(event, dict) and set(event) == {
            "line", "block", "call_id", "kind", "output_kind", "node"})
        require(line(event["line"]) and usable_id(event["call_id"]))
        if report.format == "claude":
            require(usable_id(event["node"]) and event["node"] in report.nodes and
                    report.nodes[event["node"]].line == event["line"])
            require(natural(event["block"]) and event["kind"] in ("tool_use", "tool_result"))
            require(event["output_kind"] == ("tool_result" if event["kind"] == "tool_use" else ""))
        else:
            require(event["node"] is None and event["block"] is None)
            require(isinstance(event["kind"], str) and event["kind"] in PAIRS.keys() | OUTPUTS)
            require(event["output_kind"] == PAIRS.get(event["kind"], ""))
        report.events.append(Event(**event))
    positions = [(e.line, e.block if e.block is not None else -1) for e in report.events]
    require(positions == sorted(set(positions)))
    for issue in value["issues"]:
        require(isinstance(issue, dict) and set(issue) == {"code", "line", "detail", "call_id", "block"})
        require(line(issue["line"]) and isinstance(issue["detail"], str) and
                nullable_id(issue["call_id"]) and (issue["block"] is None or natural(issue["block"])))
        require(issue["code"] in (("RD206", "RD207") if report.format == "claude" else ("RD103", "RD107")))
        report.issues.append(Issue(**issue))
    require(report.format == "claude" or not report.nodes)
    require(report.format != "empty" or not (report.events or report.issues))
    offsets = {}
    last_line, last_end = 0, 0
    for location in value["locations"]:
        require(isinstance(location, list) and len(location) == 3)
        number, start, end = location
        require(line(number) and number > last_line and natural(start) and natural(end) and
                last_end <= start < end <= offset)
        require(number != 1 or start == 0)
        require(number != report.lines or end == offset)
        offsets[number] = (start, end)
        last_line, last_end = number, end
    require(set(offsets) == relevant_lines(report))
    return report, offsets, value["terminated"]
