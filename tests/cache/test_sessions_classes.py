"""Port of test/sessions-classes.test.js."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agentwrench.cache import CacheError, analyze_sessions
from agentwrench.cache.jscompat import is_safe_integer

from .helpers import AFTER, BEFORE, CONFIG, cli, js, read_bytes, serialize, without

PATHS = [BEFORE, AFTER]
FIELDS = ["/messages/0/content"]


def sessions_cli(*args: str):
    return cli("sessions", *args)


def save(directory: Path, name: str, entries) -> str:
    path = directory / name
    path.write_text(serialize(entries), encoding="utf-8")
    return str(path)


def verify_totals(report) -> None:
    for view in ("raw", "configured"):
        summary = report if view == "raw" else report.get("configured")
        if not summary:
            continue
        pooled: dict[str, int] = {}
        for session in report["sessions"]:
            counts = session["classCounts"] if view == "raw" else session["configured"]["classCounts"]
            assert list(counts) == sorted(counts)
            assert sum(counts.values()) == session["pairs"]
            for key, count in counts.items():
                assert is_safe_integer(count) and count > 0
                pooled[key] = pooled.get(key, 0) + count
        assert pooled == summary["classCounts"]


def test_per_file_raw_configured_classes_sum_to_pooled_counts() -> None:
    originals = [read_bytes(p) for p in (*PATHS, CONFIG)]
    r = analyze_sessions(PATHS, volatile_fields=FIELDS)
    for s in r["sessions"]:
        assert s["classCounts"] == {"system_changed": 1}
        assert s["configured"]["classCounts"] == (
            {"history_rewritten": 1} if s["path"] == PATHS[0] else {"append_only": 1}
        )
        assert sorted(s["configured"]) == [
            "breakingPairs",
            "classCounts",
            "stability",
            "usageBreaks",
            "usageComparedPairs",
        ]
    verify_totals(r)
    raw = analyze_sessions(PATHS)
    assert analyze_sessions(PATHS, volatile_fields=[]) == raw
    assert all("configured" not in s and s["classCounts"]["system_changed"] == 1 for s in raw["sessions"])
    raw_top = without(r, "sessions", "configured", "volatility")
    assert {**raw_top, "sessions": [without(s, "configured", "volatility") for s in r["sessions"]]} == raw
    assert js(analyze_sessions(PATHS[::-1], volatile_fields=FIELDS)) == js(r)
    args = [*PATHS, "--volatile-fields", CONFIG, "--json"]
    output = sessions_cli(*args, "--min-stability", "0.9")
    assert output.status == 1, output.stderr
    assert json.loads(output.stdout) == r
    selected = sessions_cli(*args, "--policy-view", "configured", "--min-stability", "0.9")
    assert selected.status == 0, selected.stderr
    assert selected.stdout == output.stdout
    reversed_result = sessions_cli(*PATHS[::-1], "--volatile-fields", CONFIG, "--json")
    assert reversed_result.status == 0, reversed_result.stderr
    assert reversed_result.stdout == output.stdout
    for path, original in zip((*PATHS, CONFIG), originals):
        assert read_bytes(path) == original


def test_zero_loss_class_failures_identify_escaped_file_labels(tmp_path) -> None:
    empty = {"model": "gpt-test", "tools": [{"name": "lookup", "description": "metadata"}], "messages": []}
    system = {**empty, "messages": [{"role": "system", "content": "policy"}]}

    def wrap(request):
        return {"request": request, "usage": {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0}}}

    changed = save(tmp_path, 'changed"\\\t\n\u001b.jsonl', [wrap(empty), wrap(system)])
    singleton = save(tmp_path, "singleton.jsonl", [wrap(empty)])
    inputs = [changed, singleton]
    own_fields = ["/tools/0/description"]
    own_config = str(tmp_path / "fields.json")
    Path(own_config).write_text(js(own_fields))
    originals = [read_bytes(p) for p in (*inputs, own_config)]
    r = analyze_sessions(inputs, volatile_fields=own_fields)
    s = next(x for x in r["sessions"] if x["path"] == changed)
    single = next(x for x in r["sessions"] if x["path"] == singleton)
    for view in (s, s["configured"]):
        assert view["classCounts"] == {"system_changed": 1}
        assert view["stability"] == 1
        assert view["breakingPairs"] == 0
        assert view["usageBreaks"] == 1
    for view in (single, single["configured"]):
        assert view["classCounts"] == {}
        assert view["stability"] is None
        assert view["usageBreaks"] == 0
    assert r["pairs"] == 1
    for view in (r, r["configured"]):
        assert view["rankedPaths"] == []
        assert view["breakingPairs"] == 0
        assert view["stability"] == 1
    verify_totals(r)
    args = [*inputs, "--volatile-fields", own_config]
    for policy in ("raw", "configured"):
        selected = [*args, "--policy-view", policy]
        success = sessions_cli(*selected, "--min-stability", "1", "--json")
        assert success.status == 0, success.stderr
        assert json.loads(success.stdout) == r
        for gate in ("system_changed", "usage_break"):
            failure = sessions_cli(*selected, "--fail-on", gate, "--json")
            assert failure.status == 1, failure.stderr
            assert failure.stdout == success.stdout
    text = sessions_cli(*args, "--policy-view", "configured", "--fail-on", "system_changed")
    assert text.status == 1, text.stderr
    assert "\u001b" not in text.stdout
    assert re.search(r"session\tformat\tpairs\tstability\traw classes", text.stdout)
    assert re.search(r"session\tconfigured stability\tconfigured classes\tvolatile fields", text.stdout)
    assert f'{js(changed)}\topenai-chat\t1\t1.000000\t{{"system_changed":1}}\t0\t1\t1\n' in text.stdout
    assert f'{js(changed)}\t1.000000\t{{"system_changed":1}}\t' in text.stdout
    assert f"{js(singleton)}\topenai-chat\t0\tn/a\t{{}}\t0\t0\t0\n" in text.stdout
    alone = analyze_sessions([singleton], volatile_fields=own_fields)
    verify_totals(alone)
    assert alone["classCounts"] == {}
    assert alone["configured"]["classCounts"] == {}
    missing = {**empty, "tools": [{"name": "lookup"}]}
    bad = save(tmp_path, "z-missing-singleton.jsonl", [missing])
    with pytest.raises(CacheError, match="matches no request"):
        analyze_sessions([changed, bad], volatile_fields=own_fields)
    invalid = sessions_cli(changed, bad, "--volatile-fields", own_config, "--json")
    assert invalid.status == 2, invalid.stderr
    assert invalid.stdout == ""
    for path, original in zip((*inputs, own_config), originals):
        assert read_bytes(path) == original


def test_per_file_counts_include_repeated_append_only_and_zero_loss_classes(tmp_path) -> None:
    def body(messages):
        return {"model": "gpt-test", "messages": messages}

    def system(content):
        return {"role": "system", "content": content}

    user = {"role": "user", "content": "question"}
    mixed = save(
        tmp_path,
        "mixed.jsonl",
        [
            body([]),
            body([system("old")]),
            body([system("old"), user]),
            body([system("new"), user]),
            body([system("new"), user]),
        ],
    )
    singleton = save(tmp_path, "singleton.jsonl", [{"model": "other-model", "messages": []}])
    inputs = [mixed, singleton]
    r = analyze_sessions(inputs)
    assert r["sessions"][0]["classCounts"] == {"append_only": 2, "system_changed": 2}
    assert r["sessions"][1]["classCounts"] == {}
    assert r["pairs"] == 4
    assert r["breakingPairs"] == 1
    assert r["rankedPaths"][0]["classCounts"] == {"system_changed": 1}
    verify_totals(r)
    assert sorted(r["sessions"][0]) == [
        "breakingPairs",
        "classCounts",
        "format",
        "ignoredFields",
        "pairs",
        "path",
        "requests",
        "stability",
        "usageBreaks",
        "usageComparedPairs",
    ]
    args = [*inputs, "--json"]
    raw = sessions_cli(*args)
    assert raw.status == 0, raw.stderr
    assert json.loads(raw.stdout) == r
    empty_config = str(tmp_path / "empty.json")
    Path(empty_config).write_text("[]")
    empty = sessions_cli(*args, "--volatile-fields", empty_config)
    assert empty.status == 0, empty.stderr
    assert empty.stdout == raw.stdout
    reverse = sessions_cli(*inputs[::-1], "--json")
    assert reverse.status == 0, reverse.stderr
    assert reverse.stdout == raw.stdout
    invalid = sessions_cli(*args, "--volatile-fields", empty_config, "--policy-view", "configured")
    assert invalid.status == 2, invalid.stderr
    assert invalid.stdout == ""
