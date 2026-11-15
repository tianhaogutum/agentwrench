"""Port of test/analyze-classes.test.js."""

from __future__ import annotations

import json
import re

from agentwrench.cache import CLASSES, analyze, parse_capture
from agentwrench.cache.jscompat import is_safe_integer

from .helpers import (
    AFTER,
    BEFORE,
    CAPTURE,
    CONFIG,
    Saver,
    cli,
    clone,
    js,
    read_bytes,
    read_entries,
    read_text,
    score,
    without,
)

FIELDS = ["/messages/0/content"]
USAGE = CAPTURE


def analyze_cli(*args: str):
    return cli("analyze", *args)


def parse(values):
    return parse_capture("\n".join(js(v) for v in values))


BASE = {"model": "gpt-test", "tools": [{"name": "lookup", "description": "metadata"}], "messages": []}
SYSTEM = {**BASE, "messages": [{"role": "system", "content": "policy"}]}
QUESTION = {**SYSTEM, "messages": [*SYSTEM["messages"], {"role": "user", "content": "a"}]}
CHANGED = {**QUESTION, "messages": [*SYSTEM["messages"], {"role": "user", "content": "b"}]}
MIXED = [BASE, SYSTEM, QUESTION, CHANGED, CHANGED, {**CHANGED, "model": "gpt-other"}]
MIXED_COUNTS = {"append_only": 2, "history_rewritten": 1, "model_changed": 1, "system_changed": 1}


def check_counts(report, expected) -> None:
    assert report["classCounts"] == expected
    assert list(report["classCounts"]) == sorted(expected)
    assert sum(report["classCounts"].values()) == report["pairs"]
    for key, count in report["classCounts"].items():
        assert key in CLASSES
        assert is_safe_integer(count) and count > 0
        assert count == len([row for row in report["rows"] if row["class"] == key])


def check_text(text: str, report) -> None:
    lines = text.rstrip().split("\n")
    views = [("raw ", report), ("configured ", report["configured"])] if "configured" in report else [("", report)]
    assert len([line for line in lines if re.match(r"^(raw |configured )?classes: ", line)]) == len(views)
    for label, view in views:
        at = lines.index(f"{label}stability: {score(view['stability'])}")
        assert lines[at + 1] == f"{label}classes: {js(view['classCounts'])}"
        assert lines[at + 2] == f"captured usage compared pairs: {view['usageComparedPairs']}"


def test_observed_class_counts_separate_views_and_original_usage() -> None:
    for path, configured_counts in [
        (BEFORE, {"history_rewritten": 1}),
        (AFTER, {"append_only": 1}),
        (USAGE, {"append_only": 1}),
    ]:
        records = parse(read_entries(path))
        original = clone(records)
        report = analyze(records, FIELDS)
        check_counts(report, {"system_changed": 1})
        check_counts(report["configured"], configured_counts)
        if path == USAGE:
            assert report["usageBreaks"] == 1
            assert report["configured"]["usageBreaks"] == 1
            assert report["configured"]["rows"][0]["usage"] is report["rows"][0]["usage"]
            assert report["configured"]["rows"][0]["usage"]["payloadClass"] == "system_changed"
        else:
            assert report["rows"][0]["usage"] is None
        raw = analyze(records)
        check_counts(raw, {"system_changed": 1})
        assert "configured" not in raw
        assert analyze(records, []) == raw
        single = analyze(records[:1], FIELDS)
        check_counts(single, {})
        check_counts(single["configured"], {})
        assert records == original


def test_counts_append_only_and_zero_loss_rows_independently_of_loss_order_and_usage() -> None:
    records = parse(MIXED)
    original = clone(records)
    report = analyze(records, ["/tools/0/description"])
    for view in (report, report["configured"]):
        check_counts(view, MIXED_COUNTS)
        assert [row["class"] for row in view["rows"]] == [
            "system_changed",
            "append_only",
            "history_rewritten",
            "append_only",
            "model_changed",
        ]
        assert view["rows"][0]["stability"] == 1
        assert view["rows"][0]["firstDivergence"]
        assert view["usageBreaks"] == 0
        assert view["usageComparedPairs"] == 0
    a, b = read_entries(BEFORE)
    alternating = analyze(parse([a, b, b, a]), FIELDS)
    check_counts(alternating, {"append_only": 1, "system_changed": 2})
    check_counts(alternating["configured"], {"append_only": 1, "history_rewritten": 2})
    assert records == original


def test_class_summaries_appear_once_after_stability_and_in_json_jsonl(tmp_path) -> None:
    save = Saver(tmp_path)
    own = save("fields.json", '["/tools/0/description"]')
    empty = save("empty.json", "[]")
    single = save("singleton.jsonl", js(read_entries(BEFORE)[0]) + "\n")
    spaced = save("mixed.jsonl", "\n" + "\n\n".join(js(v) for v in MIXED) + "\n")
    inputs = [(BEFORE, CONFIG), (AFTER, CONFIG), (USAGE, CONFIG), (single, CONFIG), (spaced, own)]
    for path, allowlist in inputs:
        original, original_config = read_bytes(path), read_bytes(allowlist)
        for options in ([], ["--volatile-fields", empty], ["--volatile-fields", allowlist]):
            volatile = json.loads(read_text(options[1])) if options else []
            report = analyze(parse_capture(original.decode()), volatile)
            result = analyze_cli(path, *options, "--json")
            assert result.status == 0, result.stderr
            assert json.loads(result.stdout) == report
            # This assertion fails on the old renderer even when the API lacks counts.
            assert isinstance(report["classCounts"], dict)
            text = analyze_cli(path, *options)
            assert text.status == 0, text.stderr
            check_text(text.stdout, report)
            jsonl = analyze_cli(path, *options, "--format", "jsonl")
            assert jsonl.status == 0, jsonl.stderr
            summary, *pairs = [json.loads(line) for line in jsonl.stdout.rstrip().split("\n")]
            raw = without(report, "rows", "configured", "volatility")
            expected = {"schemaVersion": 1, "type": "summary", "raw": raw}
            if "configured" in report:
                expected["configured"] = without(report["configured"], "rows")
                expected["volatility"] = report["volatility"]
            assert summary == expected
            assert len(pairs) == report["pairs"]
            for i, pair in enumerate(pairs):
                want = {"schemaVersion": 1, "type": "pair", "pairIndex": i, "raw": report["rows"][i]}
                if "configured" in report:
                    want["configured"] = report["configured"]["rows"][i]
                assert pair == want
            if path == spaced:
                assert [[p["raw"]["from"], p["raw"]["to"]] for p in pairs] == [
                    [2, 4],
                    [4, 6],
                    [6, 8],
                    [8, 10],
                    [10, 12],
                ]
            if not options:
                assert analyze_cli(path, "--volatile-fields", empty).stdout == text.stdout
        assert read_bytes(path) == original
        assert read_bytes(allowlist) == original_config


def test_class_summaries_preserve_complete_policy_reports_including_zero_loss_gates(tmp_path) -> None:
    save = Saver(tmp_path)
    zero = save("zero.jsonl", "\n".join(js(v) for v in (BASE, SYSTEM)) + "\n")
    own = save("fields.json", '["/tools/0/description"]')
    for mode in ([], ["--json"], ["--format", "jsonl"]):
        for path, allowlist, raw_class, configured_class in [
            (BEFORE, CONFIG, "system_changed", "history_rewritten"),
            (AFTER, CONFIG, "system_changed", "append_only"),
            (zero, own, "system_changed", "system_changed"),
        ]:
            args = [path, "--volatile-fields", allowlist, *mode]
            plain = analyze_cli(*args)
            assert plain.status == 0, plain.stderr
            for policy in ("raw", "configured"):
                for gate, expected in (("model_changed", 0), (raw_class if policy == "raw" else configured_class, 1)):
                    result = analyze_cli(*args, "--policy-view", policy, "--fail-on", gate)
                    assert result.status == expected, result.stderr
                    want = (
                        plain.stdout
                        if mode
                        else plain.stdout.replace("policy view: raw\n", f"policy view: {policy}\n", 1)
                    )
                    assert result.stdout == want
