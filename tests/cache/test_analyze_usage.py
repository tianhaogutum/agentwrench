"""Port of test/analyze-usage.test.js."""

from __future__ import annotations

import json
import re

import pytest

from agentwrench.cache import CacheError, analyze, parse_capture
from agentwrench.cache.jscompat import MAX_SAFE_INTEGER, is_safe_integer

from .helpers import (
    CAPTURE,
    CONFIG,
    Saver,
    cli,
    clone,
    js,
    read_bytes,
    read_entries,
    read_text,
    without,
)

FIXTURE = CAPTURE
FIELDS = ["/messages/0/content"]


def entries():
    return read_entries(FIXTURE)


def records_from(values):
    return parse_capture("\n".join(js(v) for v in values))


def analyze_cli(*args: str):
    return cli("analyze", *args)


def check(report, compared: int, breaks: int) -> None:
    for view in (report, report.get("configured")):
        if not view:
            continue
        assert view["usageComparedPairs"] == compared
        assert view["usageBreaks"] == breaks
        assert is_safe_integer(view["usageComparedPairs"])
        assert view["usageBreaks"] <= view["usageComparedPairs"] <= view["pairs"]
        assert len([row for row in view["rows"] if row["usage"] is not None]) == compared


def test_summarizes_original_usage_coverage_in_raw_and_configured_views() -> None:
    consistent = entries()
    consistent[1]["usage"]["prompt_tokens_details"]["cached_tokens"] = 1024
    cases = [
        (entries(), 1, 1),
        ([without(e, "usage") for e in entries()], 0, 0),
        (consistent, 1, 0),
        (entries()[:1], 0, 0),
    ]
    for values, compared, breaks in cases:
        records = records_from(values)
        original = clone(records)
        raw = analyze(records)
        report = analyze(records, FIELDS)
        check(raw, compared, breaks)
        check(report, compared, breaks)
        assert report["configured"]["stability"] == (1 if report["pairs"] else None)
        if report["pairs"]:
            assert report["rows"][0]["class"] == "system_changed"
            assert report["configured"]["rows"][0]["class"] == "append_only"
            assert report["configured"]["rows"][0]["usage"] is report["rows"][0]["usage"]
            if compared:
                assert report["configured"]["rows"][0]["usage"]["payloadClass"] == "system_changed"
            else:
                assert re.search("No chain comparison", report["configured"]["rows"][0]["usageNote"])
        else:
            assert raw["stability"] is None
        assert analyze(records, []) == raw
        assert "configured" not in raw
        assert records == original
    # Count multiple comparable rows; an intervening record without usage breaks both adjacent chains.
    values = entries()
    records = records_from([values[0], values[1], values[0], {"request": values[0]["request"]}, values[1]])
    check(analyze(records, FIELDS), 2, 2)


def with_usage(request, usage):
    """``{ request, usage }`` where an undefined usage is omitted by JSON.stringify."""
    return {"request": request} if usage is ... else {"request": request, "usage": usage}


def test_coverage_follows_original_counter_requirements_and_fallbacks_across_formats() -> None:
    maximum = MAX_SAFE_INTEGER
    chat = {"model": "gpt-test", "messages": [{"role": "system", "content": "turn=1"}]}
    responses = {"model": "gpt-test", "instructions": "turn=1", "input": "hello"}
    anthropic = {"model": "claude-test", "system": "turn=1", "messages": []}
    for body, key, total, path in [
        (chat, "prompt_tokens_details", "prompt_tokens", FIELDS[0]),
        (responses, "input_tokens_details", "input_tokens", "/instructions"),
    ]:

        def details(cached_tokens, key=key):
            return {key: {"cached_tokens": cached_tokens}}

        cases = [
            ({}, {}, 0, 0),
            ({total: 0}, details(0), 0, 0),
            (details(0), details(0), 1, 0),  # valid zero, unknown-write fallback
            ({**details(2), total: 7}, details(7), 1, 0),
            ({key: {"cached_tokens": 2, "cache_write_tokens": 3}, total: 99}, details(5), 1, 0),
            (details(2), details(0), 1, 1),
            (details(0), details(2), 1, 1),
            ({key: {"cached_tokens": maximum, "cache_write_tokens": 1}}, details(0), 0, 0),
            (..., details(0), 0, 0),
            (details(0), ..., 0, 0),
            (None, details(0), 0, 0),
            (details(0), {key: {}}, 0, 0),
        ]
        for previous, nxt, compared, breaks in cases:
            records = records_from([with_usage(body, previous), with_usage(body, nxt)])
            check(analyze(records, [path]), compared, breaks)
    for previous, nxt, compared, breaks in [
        ({"cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}, {"cache_read_input_tokens": 0}, 1, 0),
        ({"cache_read_input_tokens": 2, "cache_creation_input_tokens": 3}, {"cache_read_input_tokens": 0}, 1, 1),
        ({"cache_read_input_tokens": 1}, {"cache_read_input_tokens": 1}, 0, 0),
        ({"cache_read_input_tokens": 0, "cache_creation_input_tokens": 1}, {}, 0, 0),
        ({"cache_read_input_tokens": maximum, "cache_creation_input_tokens": 1}, {"cache_read_input_tokens": 0}, 0, 0),
    ]:
        records = records_from([{"request": anthropic, "usage": previous}, {"request": anthropic, "usage": nxt}])
        check(analyze(records, ["/system"]), compared, breaks)


def test_cli_labels_original_coverage_with_complete_policies_and_rejects_invalid_evidence(tmp_path) -> None:
    save = Saver(tmp_path)
    directory = str(tmp_path)
    consistent = entries()
    consistent[1]["usage"]["prompt_tokens_details"]["cached_tokens"] = 1024
    single = save.entries("singleton.jsonl", entries()[:1])
    missing = save.entries("missing.jsonl", [without(e, "usage") for e in entries()])
    stable = save.entries("consistent.jsonl", consistent)
    empty = save("empty.json", "[]\n")
    original, original_config = read_bytes(FIXTURE), read_bytes(CONFIG)
    for path, compared, breaks in [(FIXTURE, 1, 1), (missing, 0, 0), (stable, 1, 0), (single, 0, 0)]:
        before = read_bytes(path)
        raw = analyze_cli(path, "--json")
        assert raw.status == 0, raw.stderr
        check(json.loads(raw.stdout), compared, breaks)
        empty_result = analyze_cli(path, "--volatile-fields", empty, "--json")
        assert empty_result.status == 0, empty_result.stderr
        assert empty_result.stdout == raw.stdout
        args = [path, "--volatile-fields", CONFIG]
        result = analyze_cli(*args, "--json")
        assert result.status == 0, result.stderr
        check(json.loads(result.stdout), compared, breaks)
        for policy in ("raw", "configured"):
            gated = analyze_cli(*args, "--policy-view", policy, "--fail-on", "usage_break", "--json")
            assert gated.status == (1 if breaks else 0), gated.stderr
            assert gated.stdout == result.stdout
            text = analyze_cli(*args, "--policy-view", policy, "--fail-on", "usage_break")
            assert text.status == (1 if breaks else 0), text.stderr
            raw_score = "n/a" if path == single else "0.232117"
            raw_classes = "{}" if path == single else '{"system_changed":1}'
            assert (
                f"raw stability: {raw_score}\nraw classes: {raw_classes}\ncaptured usage compared pairs: {compared}\n"
                in text.stdout
            )
            configured_score = "n/a" if path == single else "1.000000"
            configured_classes = "{}" if path == single else '{"append_only":1}'
            assert (
                f"configured stability: {configured_score}\nconfigured classes: {configured_classes}\n"
                f"captured usage compared pairs: {compared}\n"
            ) in text.stdout
            assert text.stdout.count(f"captured usage compared pairs: {compared}\n") == 2
        plain = analyze_cli(path)
        assert plain.status == 0, plain.stderr
        assert plain.stdout.count(f"captured usage compared pairs: {compared}\n") == 1
        assert read_bytes(path) == before
    policy_json = analyze_cli(FIXTURE, "--volatile-fields", CONFIG, "--json").stdout
    for args in (
        ["--min-stability", "1"],
        ["--fail-on", "system_changed"],
        ["--policy-view", "configured", "--min-stability", "1", "--fail-on", "system_changed"],
    ):
        r = analyze_cli(FIXTURE, "--volatile-fields", CONFIG, *args, "--json")
        assert r.status == (0 if "configured" in args else 1), r.stderr
        assert r.stdout == policy_json
    request = entries()[0]["request"]
    invalid = [
        save.entries(f"invalid-{i}.jsonl", [entries()[0], {"request": request, "usage": usage}])
        for i, usage in enumerate(
            [
                {"prompt_tokens_details": {"cached_tokens": -1}},
                {"prompt_tokens_details": {"cached_tokens": "0"}},
                {"input_tokens_details": []},
                {"cache_creation_input_tokens": float(MAX_SAFE_INTEGER + 1)},
            ]
        )
    ]
    malformed = save("malformed.jsonl", js(entries()[0]) + "\n{bad\n")
    container = save.entries(
        "container.jsonl", [entries()[0], {"request": {**request, "messages": [{"role": "system", "content": []}]}}]
    )
    bad_config = save("bad-config.json", "{bad")
    unmatched = save("unmatched.json", '["/messages/9/content"]')
    for args in [
        *[[path] for path in invalid],
        [malformed],
        [container, "--volatile-fields", CONFIG],
        [single, "--volatile-fields", unmatched],
        [FIXTURE, "--volatile-fields", bad_config],
        [FIXTURE, "--volatile-fields", directory],
        [FIXTURE, "--volatile-fields", empty, "--policy-view", "configured"],
        [FIXTURE, "--fail-on", "unknown"],
        [FIXTURE, "--min-stability", "NaN"],
    ]:
        for mode in ([], ["--json"]):
            result = analyze_cli(*args, *mode)
            assert result.status == 2, result.stderr
            assert result.stdout == ""
            assert result.stderr
    for path in invalid:
        with pytest.raises(CacheError, match="line 2: usage"):
            parse_capture(read_text(path))
    with pytest.raises(CacheError, match="matches no request"):
        analyze(records_from(entries()[:1]), ["/messages/9/content"])
    assert read_bytes(FIXTURE) == original
    assert read_bytes(CONFIG) == original_config
