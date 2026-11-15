"""Port of test/volatile.test.js."""

from __future__ import annotations

import json
import re

import pytest

from agentwrench.cache import CacheError, analyze, parse_capture
from agentwrench.cache.jscompat import parse_json

from .helpers import CAPTURE, Saver, cli, clone, js, read_bytes, read_text, without

FIXTURE = CAPTURE
FIELDS = ["/messages/0/content"]


def records_from(entries):
    return parse_capture("\n".join(js(e) for e in entries))


def base():
    return {
        "model": "gpt-test",
        "messages": [{"role": "system", "content": "turn=1"}, {"role": "user", "content": "Stable question"}],
    }


def test_allowlist_reports_raw_and_configured_stability_and_original_usage_without_mutation() -> None:
    records = parse_capture(read_text(FIXTURE))
    original = clone(records)
    raw = analyze(records)
    report = analyze(records, FIELDS)
    assert report["stability"] == 477 / 2055
    assert report["rows"][0]["class"] == "system_changed"
    assert report["rows"][0]["firstDivergence"]["path"] == FIELDS[0]
    assert report["configured"]["stability"] == 1
    assert report["configured"]["rows"][0]["class"] == "append_only"
    assert report["configured"]["rows"][0]["firstDivergence"] is None
    assert report["rows"][0]["volatileChanges"] == [{"path": FIELDS[0], "classification": "known_volatile"}]
    assert report["volatility"]["fields"] == [{"path": FIELDS[0], "matchedRequests": 2}]
    # Historical usage remains evidence of the raw request, including its payload class.
    assert report["configured"]["rows"][0]["usage"] == raw["rows"][0]["usage"]
    assert report["configured"]["usageBreaks"] == 1
    without_config = without(report, "configured", "volatility")
    assert {**without_config, "rows": [without(row, "volatileChanges") for row in report["rows"]]} == raw
    assert records == original
    assert analyze(records, []) == raw


def test_configured_analysis_exposes_later_drift_missing_fields_type_changes_and_key_order() -> None:
    a, b = base(), base()
    b["messages"][0]["content"] = "turn=2"
    b["messages"][1]["content"] = "Changed question"
    report = analyze(records_from([a, b]), FIELDS)
    assert report["configured"]["rows"][0]["class"] == "history_rewritten"
    assert report["configured"]["rows"][0]["firstDivergence"]["path"] == "/messages/1/content"
    assert report["configured"]["stability"] < 1
    missing = clone(a)
    del missing["messages"][0]["content"]
    assert analyze(records_from([a, missing]), FIELDS)["configured"]["rows"][0]["class"] == "system_changed"
    changed_type = clone(a)
    changed_type["messages"][0]["content"] = None
    typed = analyze(records_from([a, changed_type]), FIELDS)
    assert typed["configured"]["rows"][0]["class"] == "system_changed"
    assert typed["rows"][0]["volatileChanges"] == []
    reordered = clone(a)
    reordered["messages"][0] = {"content": a["messages"][0]["content"], "role": "system"}
    assert analyze(records_from([a, reordered]), FIELDS)["configured"]["rows"][0]["class"] == "system_changed"


def test_exact_body_relative_pointers_cover_formats_escaped_keys_falsy_and_prototype_keys() -> None:
    def set_system(a):
        a["system"][0]["text"] = "at 2"

    def set_instructions(a):
        a["instructions"] = "at 2"

    def set_input(a):
        a["input"][0]["content"][0]["text"] = "at 2"

    def set_escaped(a):
        a["messages"][0]["content"][0]["a/b~c"] = 12

    def set_proto(a):
        a["messages"][0]["__proto__"]["constructor"] = True

    cases = [
        (
            {"model": "claude-test", "system": [{"type": "text", "text": "at 1"}], "messages": []},
            "/system/0/text",
            set_system,
        ),
        ({"model": "gpt-test", "instructions": "at 1", "input": "hello"}, "/instructions", set_instructions),
        (
            {
                "model": "gpt-test",
                "input": [{"role": "developer", "content": [{"type": "input_text", "text": "at 1"}]}],
            },
            "/input/0/content/0/text",
            set_input,
        ),
        (
            {
                "model": "claude-test",
                "messages": [{"role": "user", "content": [{"type": "text", "text": "Hi", "a/b~c": 0}]}],
            },
            "/messages/0/content/0/a~1b~0c",
            set_escaped,
        ),
        (
            parse_json(
                '{"model":"gpt-test","messages":[{"role":"user","content":null,"__proto__":{"constructor":false}}]}'
            ),
            "/messages/0/__proto__/constructor",
            set_proto,
        ),
    ]
    for a, path, change in cases:
        b = clone(a)
        change(b)
        entries = [{"request": a, "ts": "unchanged"}, {"request": b, "ts": "unchanged"}]
        report = analyze(records_from(entries), [path])
        assert report["stability"] < 1, path
        assert report["configured"]["stability"] == 1, path
        assert report["rows"][0]["volatileChanges"] == [{"path": path, "classification": "known_volatile"}]
    assert not hasattr(object(), "polluted")
    nullable = base()
    nullable["messages"][0]["content"] = None
    assert analyze(records_from([nullable]), FIELDS)["configured"]["stability"] is None


def test_invalid_unmatched_container_unmodeled_and_structural_targets_fail_clearly() -> None:
    records = records_from([base(), base()])
    for volatile_fields in [
        None,
        "bad",
        [1],
        [""],
        ["messages/0/content"],
        ["/messages/~2/content"],
        [*FIELDS, *FIELDS],
        ["/messages/00/content"],
        ["/messages/-/content"],
        ["/messages/2/content"],
        ["/request/messages/0/content"],
        ["/messages/0"],
        ["/messages/0/role"],
        ["/model"],
        ["/messages/constructor"],
        ["/messages/0/toString"],
        ["/x"] * 129,
        ["/" + "x" * 1025],
    ]:
        with pytest.raises(CacheError, match=re.compile("volatile", re.I)):
            analyze(records, volatile_fields)
    unmodeled = base()
    unmodeled["metadata"] = {"time": "1"}
    with pytest.raises(CacheError, match=re.compile("unmodeled", re.I)):
        analyze(records_from([unmodeled]), ["/metadata/time"])
    tool = {**base(), "tools": [{"type": "function", "function": {"name": "read"}}]}
    with pytest.raises(CacheError, match=re.compile("structural", re.I)):
        analyze(records_from([tool]), ["/tools/0/function/name"])
    container = base()
    container["messages"][0]["content"] = [{"type": "text", "text": "Hi"}]
    with pytest.raises(CacheError, match=re.compile("scalar", re.I)):
        analyze(records_from([base(), container]), FIELDS)


def test_cli_allowlist_reports_both_views_ci_defaults_raw_configured_is_explicit(tmp_path) -> None:
    save = Saver(tmp_path)
    directory = str(tmp_path)
    config = save("fields.json", js(FIELDS))
    original = read_bytes(FIXTURE)
    result = cli("analyze", FIXTURE, "--volatile-fields", config, "--json")
    assert result.status == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["configured"]["stability"] == 1
    assert cli("analyze", FIXTURE, "--volatile-fields", config, "--min-stability", "1").status == 1
    assert (
        cli(
            "analyze",
            FIXTURE,
            "--volatile-fields",
            config,
            "--policy-view",
            "configured",
            "--min-stability",
            "1",
            "--fail-on",
            "system_changed",
        ).status
        == 0
    )
    assert (
        cli(
            "analyze", FIXTURE, "--volatile-fields", config, "--policy-view", "configured", "--fail-on", "usage_break"
        ).status
        == 1
    )
    text = cli("analyze", FIXTURE, "--volatile-fields", config)
    assert text.status == 0
    assert re.search(r"raw stability: 0\.232117", text.stdout)
    assert re.search(r"configured stability: 1\.000000", text.stdout)
    assert re.search("known_volatile", text.stdout)
    for extra in (
        ["--policy-view", "configured"],
        ["--policy-view", "typo"],
        ["--volatile-fields", directory],
        ["--volatile-fields", config, "--policy-view", "typo"],
    ):
        assert cli("analyze", FIXTURE, *extra).status == 2
    for value in (
        '{"password":"do-not-echo"',
        "{}",
        '["/messages/~7"]',
        '["/missing"]',
        '["/messages"]',
        '["/messages/0/content","/messages/0/content"]',
    ):
        save("fields.json", value)
        invalid = cli("analyze", FIXTURE, "--volatile-fields", config)
        assert invalid.status == 2
        assert invalid.stdout == ""
        assert "do-not-echo" not in invalid.stderr
    save("fields.json", bytes([0xC3, 0x28]))
    assert re.search("UTF-8", cli("analyze", FIXTURE, "--volatile-fields", config).stderr)
    save("fields.json", " " * 65537)
    assert re.search("64 KiB", cli("analyze", FIXTURE, "--volatile-fields", config).stderr)
    assert read_bytes(FIXTURE) == original
