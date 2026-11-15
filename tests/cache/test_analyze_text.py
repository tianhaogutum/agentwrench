"""Port of test/analyze-text.test.js."""

from __future__ import annotations

import json
import math
import re

from agentwrench.cache import analyze, parse_capture
from agentwrench.cache.jscompat import number_to_string, to_fixed

from .helpers import (
    AFTER,
    BEFORE,
    CAPTURE,
    CONFIG,
    LATER,
    Saver,
    cli,
    clone,
    js,
    read_bytes,
    read_entries,
    read_text,
    serialize,
    without,
)

FIXTURE = CAPTURE
CONFIGURED_MARKER = "configured analysis (scalar values masked):\n"
PAIR_HEADER = "pair\tshared\tsegments\tbytes\test.tokens\tfirst divergence\tclass\n"
PAIR_BLOCKS = re.compile(r"^(\d+)->(\d+)\t[^\n]*\n((?:(?!^\d+->)[\s\S])*)", re.M)
INDENTED_BLOCKS = re.compile(r"^(\d+)->(\d+)\t[^\n]*\n((?:  [^\n]*\n)*)", re.M)


def analyze_cli(*args: str):
    return cli("analyze", *args)


def entries():
    return read_entries(FIXTURE)


def counters(compared: int, breaks: int) -> str:
    return f"captured usage compared pairs: {compared}\ncaptured usage breaks: {breaks}\n"


def evidence(row) -> str:
    return f"  captured usage evidence: {js(row['usageNote'])}"


def replace_policy(text: str, policy: str) -> str:
    return re.sub(r"policy view: raw\n\Z", f"policy view: {policy}\n", text, count=1)


def check_pair_evidence(text: str, report) -> None:
    # Only raw rows get evidence: configured output follows the raw summary.
    raw = re.split(r"^(?:raw )?stability:", text, flags=re.M)[0]
    blocks = list(PAIR_BLOCKS.finditer(raw))
    assert len(blocks) == len(report["rows"])
    for block, row in zip(blocks, report["rows"]):
        assert f"{block[1]}->{block[2]}" == f"{row['from']}->{row['to']}"
        notes = [line for line in block[3].split("\n") if line.startswith("  captured usage evidence:")]
        assert notes == ([evidence(row)] if row["usage"] is None else [])
        if row["usage"] is not None:
            u = row["usage"]
            status = "usage_break" if u["usageBreak"] else "usage_chain_ok"
            expected = (
                f"  {status}: expected={number_to_string(u['expected'])} actual={number_to_string(u['actual'])} "
                f"delta={number_to_string(u['delta'])}; correlated={row['class']}; {u['basis']}\n"
            )
            assert expected in block[3]
    assert len([line for line in text.split("\n") if line.startswith("  captured usage evidence:")]) == len(
        [row for row in report["rows"] if row["usage"] is None]
    )


def test_text_explains_missing_original_usage_evidence_once_per_raw_pair(tmp_path) -> None:
    save = Saver(tmp_path)
    original, original_config = read_bytes(FIXTURE), read_bytes(CONFIG)
    empty = save("empty.json", "[]\n")
    consistent = entries()
    consistent[1]["usage"]["prompt_tokens_details"]["cached_tokens"] = 1024
    cases = [[without(e, "usage") for e in entries()], entries(), consistent, entries()[:1]]
    for i, values in enumerate(cases):
        text = serialize(values)
        path = save(f"capture-{i}.jsonl", text)
        for fields in ([], [empty], [CONFIG]):
            args = [path, *[x for f in fields for x in ("--volatile-fields", f)]]
            report = analyze(parse_capture(text), ["/messages/0/content"] if fields and fields[0] == CONFIG else [])
            plain = analyze_cli(*args)
            assert plain.status == 0, plain.stderr
            check_pair_evidence(plain.stdout, report)
            assert counters(report["usageComparedPairs"], report["usageBreaks"]) in plain.stdout
            if fields and fields[0] == empty:
                assert plain.stdout == analyze_cli(path).stdout
            if len(values) == 1:
                assert report["stability"] is None
                assert "nothing to compare\n" in plain.stdout
                assert "stability: n/a\n" in plain.stdout
            if "configured" in report:
                assert all(row["class"] == "append_only" for row in report["configured"]["rows"])
            for policy in ("raw", "configured") if "configured" in report else ("raw",):
                expected = replace_policy(plain.stdout, policy)
                selected = report["configured"] if policy == "configured" else report
                for gate, fails in [
                    (["--fail-on", "usage_break"], report["usageBreaks"] > 0),
                    (
                        ["--fail-on", "system_changed"],
                        any(row["class"] == "system_changed" for row in selected["rows"]),
                    ),
                    (["--min-stability", "1"], report["pairs"] > 0 and selected["stability"] < 1),
                ]:
                    result = analyze_cli(*args, "--policy-view", policy, *gate)
                    assert result.status == (1 if fails else 0), result.stderr
                    assert result.stdout == expected
        assert read_text(path) == text
    assert read_bytes(FIXTURE) == original
    assert read_bytes(CONFIG) == original_config


def test_text_keeps_missing_usage_evidence_in_physical_pair_order(tmp_path) -> None:
    save = Saver(tmp_path)
    a, b = entries()
    key = 'meta"/~\u001b\t\n'
    a["request"]["messages"][0][key] = "first"
    b["request"]["messages"][0][key] = "second"
    pointer = "/messages/0/" + key.replace("~", "~0").replace("/", "~1")
    fields = save("fields.json", js(["/messages/0/content", pointer]))
    values = [a, b, {"request": a["request"]}, a, b]
    text = "\n" + "\n\n".join(js(v) for v in values) + "\n"
    path = save("partial.jsonl", text)
    for allowlist in ([], ["--volatile-fields", fields]):
        report = analyze(parse_capture(text), ["/messages/0/content", pointer] if allowlist else [])
        assert [[r["from"], r["to"], r["usage"] is None] for r in report["rows"]] == [
            [2, 4, False],
            [4, 6, True],
            [6, 8, True],
            [8, 10, False],
        ]
        assert report["usageComparedPairs"] == 2
        assert report["usageBreaks"] == 2
        if "configured" in report:
            assert all(row["class"] == "append_only" for row in report["configured"]["rows"])
        for policy in ("raw", "configured") if allowlist else ("raw",):
            result = analyze_cli(path, *allowlist, "--policy-view", policy, "--fail-on", "usage_break")
            assert result.status == 1, result.stderr
            check_pair_evidence(result.stdout, report)
            assert result.stdout.count(counters(2, 2)) == (2 if allowlist else 1)
            assert "\u001b" not in result.stdout
            if allowlist:
                assert js(pointer) in result.stdout
    assert read_text(path) == text


def test_text_summarizes_captured_usage_breaks_beside_coverage_in_every_view(tmp_path) -> None:
    save = Saver(tmp_path)
    consistent = entries()
    consistent[1]["usage"]["prompt_tokens_details"]["cached_tokens"] = 1024
    empty = save("empty.json", "[]\n")
    original, original_config = read_bytes(FIXTURE), read_bytes(CONFIG)
    for path, compared, breaks, single in [
        (FIXTURE, 1, 1, False),
        (save("consistent.jsonl", serialize(consistent)), 1, 0, False),
        (save("missing.jsonl", serialize([without(e, "usage") for e in entries()])), 0, 0, False),
        (save("singleton.jsonl", serialize(entries()[:1])), 0, 0, True),
    ]:
        before = read_bytes(path)
        plain = analyze_cli(path)
        assert plain.status == 0, plain.stderr
        stability = "n/a" if single else "0.232117"
        classes = "{}" if single else '{"system_changed":1}'
        assert f"stability: {stability}\nclasses: {classes}\n" + counters(compared, breaks) in plain.stdout
        assert plain.stdout.count(counters(compared, breaks)) == 1
        assert "configured" not in plain.stdout
        empty_result = analyze_cli(path, "--volatile-fields", empty)
        assert empty_result.status == 0, empty_result.stderr
        assert empty_result.stdout == plain.stdout
        args = [path, "--volatile-fields", CONFIG]
        result = analyze_cli(*args)
        assert result.status == 0, result.stderr
        assert f"raw stability: {stability}\nraw classes: {classes}\n" + counters(compared, breaks) in result.stdout
        configured_stability = "n/a" if single else "1.000000"
        configured_classes = "{}" if single else '{"append_only":1}'
        assert (
            f"configured stability: {configured_stability}\nconfigured classes: {configured_classes}\n"
            + counters(compared, breaks)
            in result.stdout
        )
        assert result.stdout.count(counters(compared, breaks)) == 2
        report = json.loads(analyze_cli(*args, "--json").stdout)
        assert result.stdout.endswith(report["volatility"]["note"] + "\npolicy view: raw\n")
        assert report["stability"] == (None if single else 477 / 2055)
        assert [row["class"] for row in report["configured"]["rows"]] == ([] if single else ["append_only"])
        for base, expected in ((([path]), plain.stdout), (args, result.stdout)):
            for policy in ("raw", "configured") if base is args else ("raw",):
                gated = analyze_cli(*base, "--policy-view", policy, "--fail-on", "usage_break")
                assert gated.status == (1 if breaks else 0), gated.stderr
                assert gated.stdout == (replace_policy(expected, "configured") if policy == "configured" else expected)
        if single:
            assert "nothing to compare\n" in result.stdout
        assert read_bytes(path) == before
    configured = analyze_cli(FIXTURE, "--volatile-fields", CONFIG).stdout
    for policy, status in (("raw", 1), ("configured", 0)):
        gated = analyze_cli(
            FIXTURE,
            "--volatile-fields",
            CONFIG,
            "--policy-view",
            policy,
            "--min-stability",
            "1",
            "--fail-on",
            "system_changed",
        )
        assert gated.status == status, gated.stderr
        assert gated.stdout == (replace_policy(configured, "configured") if policy == "configured" else configured)
    assert read_bytes(FIXTURE) == original
    assert read_bytes(CONFIG) == original_config


def test_text_totals_multiple_original_breaks_without_missing_evidence_or_terminal_controls(tmp_path) -> None:
    save = Saver(tmp_path)
    a, b = entries()
    key = 'meta"/~\u001b\t\n'
    b["request"]["messages"][0]["content"] = a["request"]["messages"][0]["content"]
    a["request"]["messages"][0][key] = "first"
    b["request"]["messages"][0][key] = "second"
    pointer = "/messages/0/" + key.replace("~", "~0").replace("/", "~1")
    fields = save("fields.json", js([pointer]))
    text = serialize([a, b, a, {"request": a["request"]}, a])
    path = save("multiple.jsonl", text)
    report = analyze(parse_capture(text), [pointer])
    result = analyze_cli(path, "--volatile-fields", fields, "--policy-view", "configured", "--fail-on", "usage_break")
    assert result.status == 1, result.stderr
    assert result.stdout.count(counters(2, 2)) == 2
    assert report["pairs"] == 4
    assert report["configured"]["usageBreaks"] == 2
    assert all(row["class"] == "append_only" for row in report["configured"]["rows"])
    assert "\u001b" not in result.stdout
    assert js(pointer) in result.stdout
    assert result.stdout.endswith(report["volatility"]["note"] + "\npolicy view: configured\n")
    assert read_text(path) == text


def test_text_shows_existing_configured_reasons_beneath_pairs(tmp_path) -> None:
    save = Saver(tmp_path)
    empty = save("empty.json", "[]\n")
    singleton = save("singleton.jsonl", read_text(BEFORE).split("\n")[0] + "\n")
    for path in (BEFORE, AFTER, FIXTURE, singleton):
        original = read_bytes(path)
        raw = analyze_cli(path)
        empty_result = analyze_cli(path, "--volatile-fields", empty)
        assert raw.status == 0, raw.stderr
        assert empty_result.status == 0, empty_result.stderr
        assert empty_result.stdout == raw.stdout
        assert "  reason:" not in raw.stdout
        args = [path, "--volatile-fields", CONFIG]
        report = analyze(parse_capture(original.decode()), ["/messages/0/content"])
        plain = analyze_cli(*args)
        assert plain.status == 0, plain.stderr
        check_pair_evidence(plain.stdout, report)
        raw_text, configured = plain.stdout.split(CONFIGURED_MARKER)
        assert "  reason:" not in raw_text
        assert [line for line in configured.split("\n") if line.startswith("  reason:")] == [
            f"  reason: {js(row['reason'])}" for row in report["configured"]["rows"] if row.get("reason")
        ]
        for row in report["configured"]["rows"]:
            divergence = row["firstDivergence"]["path"] if row["firstDivergence"] else "-"
            pair_line = (
                f"{row['from']}->{row['to']}\t{to_fixed(row['stability'] * 100, 2)}%\t{row['sharedSegments']}/{row['previousSegments']}\t"
                f"{row['sharedBytes']}\t{math.ceil(row['sharedEstimatedTokens'])}\t{js(divergence)}\t{row['class']}\n"
            )
            assert pair_line + (f"  reason: {js(row['reason'])}\n" if row.get("reason") else "") in configured
        if path == BEFORE:
            assert report["rows"][0]["class"] == "system_changed"
            assert report["configured"]["rows"][0]["class"] == "history_rewritten"
            assert report["configured"]["rows"][0]["firstDivergence"]["path"] == "/messages/1/content"
            assert f"  differing span: {js(report['rows'][0]['differingSpan'])}\n" in plain.stdout
            assert plain.stdout.count(evidence(report["rows"][0])) == 1
            assert plain.stdout.count(report["configured"]["rows"][0]["reason"]) == 1
        else:
            assert "  reason:" not in configured
        for policy in ("raw", "configured"):
            selected = report if policy == "raw" else report["configured"]
            for gate, fails in [
                (
                    ["--fail-on", "history_rewritten"],
                    any(row["class"] == "history_rewritten" for row in selected["rows"]),
                ),
                (["--fail-on", "system_changed"], any(row["class"] == "system_changed" for row in selected["rows"])),
                (["--fail-on", "usage_break"], report["usageBreaks"] > 0),
                (["--min-stability", "0.9"], selected["pairs"] > 0 and selected["stability"] < 0.9),
            ]:
                result = analyze_cli(*args, "--policy-view", policy, *gate)
                assert result.status == (1 if fails else 0), result.stderr
                assert result.stdout == replace_policy(plain.stdout, policy)
        assert read_bytes(path) == original


def test_text_keeps_configured_history_reasons_in_physical_order_across_representation_changes(tmp_path) -> None:
    save = Saver(tmp_path)
    a, b = entries()
    b["request"]["messages"][1]["content"] = [{"type": "text", "text": a["request"]["messages"][1]["content"]}]
    c = clone(b)
    c["request"]["messages"].pop()
    text = "\n" + "\n\n".join(js(v) for v in (a, b, c, c)) + "\n"
    path = save("representation.jsonl", text)
    report = analyze(parse_capture(text), ["/messages/0/content"])
    assert [[r["from"], r["to"], r["class"]] for r in report["configured"]["rows"]] == [
        [2, 4, "history_rewritten"],
        [4, 6, "history_rewritten"],
        [6, 8, "append_only"],
    ]
    plain = analyze_cli(path, "--volatile-fields", CONFIG)
    assert plain.status == 0, plain.stderr
    check_pair_evidence(plain.stdout, report)
    configured = plain.stdout.split(CONFIGURED_MARKER)[1]
    blocks = list(INDENTED_BLOCKS.finditer(configured))
    assert len(blocks) == 3
    for block, row in zip(blocks, report["configured"]["rows"]):
        assert f"{block[1]}->{block[2]}" == f"{row['from']}->{row['to']}"
        assert block[3] == (f"  reason: {js(row['reason'])}\n" if row.get("reason") else "")
    raw = plain.stdout.split("raw stability:")[0]
    assert f"  {report['rows'][1]['reason']}\n" in raw  # Existing raw reason stays unlabeled.
    assert "  reason:" not in raw
    assert read_text(path) == text


def check_configured_spans(text: str, report) -> None:
    configured = text.split(CONFIGURED_MARKER)[1]
    blocks = list(INDENTED_BLOCKS.finditer(configured))
    assert len(blocks) == len(report["configured"]["rows"])
    for block, row in zip(blocks, report["configured"]["rows"]):
        assert f"{block[1]}->{block[2]}" == f"{row['from']}->{row['to']}"
        assert block[3] == (f"  differing span: {js(row['differingSpan'])}\n" if row.get("differingSpan") else "") + (
            f"  reason: {js(row['reason'])}\n" if row.get("reason") else ""
        )
        if row.get("differingSpan"):
            assert json.loads(block[3].split("\n")[0][len("  differing span: ") :]) == row["differingSpan"]
    check_pair_evidence(text, report)


def test_text_shows_existing_configured_differing_spans_once_beneath_their_pairs(tmp_path) -> None:
    save = Saver(tmp_path)
    empty = save("empty.json", "[]\n")
    singleton = save("singleton.jsonl", read_text(LATER).split("\n")[0] + "\n")
    for path in (LATER, BEFORE, AFTER, FIXTURE, singleton):
        original = read_bytes(path)
        raw, empty_result = analyze_cli(path), analyze_cli(path, "--volatile-fields", empty)
        assert raw.status == 0, raw.stderr
        assert empty_result.status == 0, empty_result.stderr
        assert empty_result.stdout == raw.stdout
        assert "configured analysis" not in raw.stdout
        report = analyze(parse_capture(original.decode()), ["/messages/0/content"])
        args = [path, "--volatile-fields", CONFIG]
        plain = analyze_cli(*args)
        assert plain.status == 0, plain.stderr
        check_configured_spans(plain.stdout, report)
        if path == LATER:
            assert report["rows"][0]["class"] == "system_changed"
            assert report["configured"]["rows"][0]["class"] == "system_changed"
            assert report["configured"]["rows"][0]["firstDivergence"]["path"] == "/messages/1/content"
            assert report["rows"][0]["differingSpan"] == {
                "start": 5,
                "before": "turn=1",
                "after": "turn=2",
                "volatile": ["counter"],
            }
            assert report["configured"]["rows"][0]["differingSpan"] == {
                "start": 0,
                "before": "Stable question",
                "after": "Changed question",
                "volatile": [],
            }
            assert (
                '1->2\t84.17%\t2/3\t101\t26\t"/messages/1/content"\tsystem_changed\n'
                '  differing span: {"start":0,"before":"Stable question","after":"Changed question","volatile":[]}\n'
            ) in plain.stdout
            assert plain.stdout.count("  differing span:") == 2
            assert plain.stdout.count(evidence(report["rows"][0])) == 1
        else:
            assert "  differing span:" not in plain.stdout.split(CONFIGURED_MARKER)[1]
        for policy in ("raw", "configured"):
            selected = report if policy == "raw" else report["configured"]
            for gate, fails in [
                (["--fail-on", "system_changed"], any(row["class"] == "system_changed" for row in selected["rows"])),
                (
                    ["--fail-on", "history_rewritten"],
                    any(row["class"] == "history_rewritten" for row in selected["rows"]),
                ),
                (["--fail-on", "usage_break"], report["usageBreaks"] > 0),
                (["--min-stability", "0.9"], selected["pairs"] > 0 and selected["stability"] < 0.9),
            ]:
                result = analyze_cli(*args, "--policy-view", policy, *gate)
                assert result.status == (1 if fails else 0), result.stderr
                assert result.stdout == replace_policy(plain.stdout, policy)
        assert read_bytes(path) == original


def test_text_preserves_configured_span_clipping_redaction_markers_and_escaping(tmp_path) -> None:
    save = Saver(tmp_path)
    a, b = entries()
    a["request"]["messages"][1]["role"] = b["request"]["messages"][1]["role"] = "system"
    cases = [
        ('A"\\\n\r\t\u001b\b\u0000 question', 'B"\\\n\r\t\u001b\b\u0000 question'),
        ("X" * 200 + "A" * 160 + "tail", "X" * 200 + "B" * 160 + "tail"),
        ("password=before-value private", "password=after-value private"),
        ("turn=1 2026-11-11T12:00:00Z", "turn=2 2026-11-11T12:01:00Z"),
        (None, "new system text"),  # A valid non-string system change does not synthesize a span.
    ]
    for i, (before, after) in enumerate(cases):
        left, right = clone(a), clone(b)
        left["request"]["messages"][1]["content"] = before
        right["request"]["messages"][1]["content"] = after
        text = "\n" + "\n\n".join(js(v) for v in (left, right, right)) + "\n"
        path = save(f"span-{i}.jsonl", text)
        report = analyze(parse_capture(text), ["/messages/0/content"])
        assert [[r["from"], r["to"], r["class"]] for r in report["configured"]["rows"]] == [
            [2, 4, "system_changed"],
            [4, 6, "append_only"],
        ]
        span = report["configured"]["rows"][0].get("differingSpan")
        if i == 1:
            assert span["start"] == 200
            assert len(span["before"]) == 120
            assert len(span["after"]) == 120
        if i == 2:
            assert [span["before"], span["after"]] == ["[REDACTED: sensitive-looking system text]"] * 2
        if i == 3:
            assert "counter" in span["volatile"]
            assert "iso_timestamp" in span["volatile"]
        if i == 4:
            assert span is None
        result = analyze_cli(
            path, "--volatile-fields", CONFIG, "--policy-view", "configured", "--fail-on", "system_changed"
        )
        assert result.status == 1, result.stderr
        check_configured_spans(result.stdout, report)
        assert not re.search(r"[\x00-\x08\x0b-\x1f]", result.stdout)
        assert "before-value" not in result.stdout
        assert "after-value" not in result.stdout
        assert "A" * 121 not in result.stdout
        assert "B" * 121 not in result.stdout
        assert read_text(path) == text


def check_configured_metrics(text: str, report) -> None:
    configured = text.split(CONFIGURED_MARKER)[1]
    assert configured.startswith(PAIR_HEADER if report["configured"]["pairs"] else "configured stability: n/a\n")
    assert configured.count(PAIR_HEADER) == (1 if report["configured"]["pairs"] else 0)
    lines = [line for line in configured.split("\n") if re.match(r"^\d+->\d+\t", line)]
    assert [line.split("\t") for line in lines] == [
        [
            f"{row['from']}->{row['to']}",
            f"{to_fixed(row['stability'] * 100, 2)}%",
            f"{row['sharedSegments']}/{row['previousSegments']}",
            str(row["sharedBytes"]),
            str(math.ceil(row["sharedEstimatedTokens"])),  # text rounds up; JSON keeps bytes / 4
            js(row["firstDivergence"]["path"] if row["firstDivergence"] else "-"),
            row["class"],
        ]
        for row in report["configured"]["rows"]
    ]
    assert text.count("UTF-8 serialized bytes / 4 is an estimate, not provider tokens or measured savings.\n") == 1
    check_configured_spans(text, report)


def test_text_gives_configured_metrics_the_same_columns_as_raw_only_when_pairs_exist(tmp_path) -> None:
    save = Saver(tmp_path)
    empty = save("empty.json", "[]\n")
    singleton = save("singleton.jsonl", read_text(LATER).split("\n")[0] + "\n")
    for path in (LATER, BEFORE, AFTER, FIXTURE, singleton):
        original, original_fields = read_bytes(path), read_bytes(CONFIG)
        report = analyze(parse_capture(original.decode()), ["/messages/0/content"])
        plain = analyze_cli(path, "--volatile-fields", CONFIG)
        assert plain.status == 0, plain.stderr
        check_configured_metrics(plain.stdout, report)
        if path == LATER:
            row = report["configured"]["rows"][0]
            assert [
                row["sharedSegments"],
                row["previousSegments"],
                row["sharedBytes"],
                row["sharedEstimatedTokens"],
            ] == [2, 3, 101, 25.25]
            assert (
                PAIR_HEADER
                + '1->2\t84.17%\t2/3\t101\t26\t"/messages/1/content"\tsystem_changed\n'
                + '  differing span: {"start":0,"before":"Stable question","after":"Changed question","volatile":[]}\n'
            ) in plain.stdout
        raw, no_fields = analyze_cli(path), analyze_cli(path, "--volatile-fields", empty)
        assert raw.status == 0, raw.stderr
        assert no_fields.status == 0, no_fields.stderr
        assert no_fields.stdout == raw.stdout
        assert "configured analysis" not in raw.stdout
        assert raw.stdout.count(PAIR_HEADER) == (1 if report["pairs"] else 0)
        assert read_bytes(path) == original
        assert read_bytes(CONFIG) == original_fields


def test_text_keeps_configured_metric_rows_in_physical_order_with_escaped_pointers(tmp_path) -> None:
    save = Saver(tmp_path)
    a, b = entries()
    key = 'meta"/~\u001b\t\n'
    a["request"]["messages"][1][key] = "first"
    b["request"]["messages"][1][key] = "second"
    pointer = "/messages/1/" + key.replace("~", "~0").replace("/", "~1")
    text = "\n" + "\n\n".join(js(v) for v in (a, b, {"request": a["request"]}, a, b)) + "\n"
    path = save("ordered.jsonl", text)
    report = analyze(parse_capture(text), ["/messages/0/content"])
    assert [[r["from"], r["to"], r["class"]] for r in report["configured"]["rows"]] == [
        [2, 4, "history_rewritten"],
        [4, 6, "history_rewritten"],
        [6, 8, "append_only"],
        [8, 10, "history_rewritten"],
    ]
    assert report["usageComparedPairs"] == 2
    assert report["usageBreaks"] == 2
    plain = analyze_cli(path, "--volatile-fields", CONFIG)
    assert plain.status == 0, plain.stderr
    check_configured_metrics(plain.stdout, report)
    assert js(pointer) in plain.stdout
    assert "\u001b" not in plain.stdout
    for policy in ("raw", "configured"):
        for gate, status in ((["--fail-on", "usage_break"], 1), (["--fail-on", "tool_removed"], 0)):
            result = analyze_cli(path, "--volatile-fields", CONFIG, "--policy-view", policy, *gate)
            assert result.status == status, result.stderr
            assert result.stdout == replace_policy(plain.stdout, policy)
    assert read_text(path) == text
