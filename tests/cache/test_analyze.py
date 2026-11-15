"""Port of test/analyze.test.js."""

from __future__ import annotations

import re

import pytest

from agentwrench.cache import (
    analyze,
    differing_span,
    first_difference,
    parse_capture,
    segments,
    usage_chain,
)

from .helpers import clone, fixture, js


def capture(name: str):
    with open(fixture(f"{name}.jsonl"), encoding="utf-8") as handle:
        return parse_capture(handle.read())


def report(name: str):
    return analyze(capture(name))


def pair(a, b, format: str = "auto"):
    return analyze(parse_capture("\n".join(js(v) for v in (a, b)), format))["rows"][0]


def base():
    return {"model": "gpt-synthetic", "messages": [{"role": "user", "content": "hello"}]}


CLASSES = [
    "append_only",
    "tool_reordered",
    "tool_added",
    "tool_removed",
    "tool_schema_changed",
    "system_changed",
    "history_rewritten",
    "cache_control_moved",
    "model_changed",
]


@pytest.mark.parametrize("format", ["anthropic", "openai_chat"])
@pytest.mark.parametrize("kind", CLASSES)
def test_fixture_class(format: str, kind: str) -> None:
    row = report(f"{format}_{kind}")["rows"][0]
    assert row["class"] == kind
    assert row["sharedBytes"] <= row["previousBytes"]
    assert row["sharedEstimatedTokens"] == row["sharedBytes"] / 4
    assert 0 <= row["stability"] <= 1
    if kind == "append_only":
        assert row["stability"] == 1
        assert row["firstDivergence"] is None
    else:
        assert row["firstDivergence"]["path"].startswith("/")


def test_exact_pointer_into_nested_schema_and_history_representation() -> None:
    assert (
        report("anthropic_tool_schema_changed")["rows"][0]["firstDivergence"]["path"]
        == "/tools/0/input_schema/properties/query/type"
    )
    assert (
        report("openai_chat_tool_schema_changed")["rows"][0]["firstDivergence"]["path"]
        == "/tools/0/function/parameters/properties/query/type"
    )
    assert report("openai_chat_history_rewritten")["rows"][0]["firstDivergence"]["path"] == "/messages/1/content"
    assert re.search("not assumed equivalent", report("openai_chat_history_rewritten")["rows"][0]["reason"])
    assert report("anthropic_tool_reordered")["rows"][0]["firstDivergence"]["path"] == "/tools/0"


def test_rfc6901_escaping_missing_array_item_object_key_order_and_falsy_values() -> None:
    assert first_difference({"a/b": {"~x": 0}}, {"a/b": {"~x": 1}}) == "/a~1b/~0x"
    assert first_difference([0], [0, False], "/input") == "/input/1"
    assert first_difference({"a": 0, "b": False}, {"b": False, "a": 0}) == "/a"
    assert first_difference(None, False, "/value") == "/value"
    assert first_difference({"x": [None]}, {"x": [None]}) is None


def test_tools_appended_at_category_boundary_point_to_the_added_tool() -> None:
    a, b = base(), base()
    a["tools"] = [{"type": "function", "function": {"name": "a"}}]
    b["tools"] = [*a["tools"], {"type": "function", "function": {"name": "b"}}]
    assert pair(a, b)["firstDivergence"] == {"segment": "/tools/1", "path": "/tools/1"}
    assert pair(a, b)["class"] == "tool_added"
    assert pair(b, a)["class"] == "tool_removed"
    assert pair(b, a)["firstDivergence"]["path"] == "/tools/1"


def test_stable_bytes_count_uses_utf8_framing_and_partial_first_segment() -> None:
    a, b = base(), base()
    a["messages"][0]["content"] = "你好A"
    b["messages"][0]["content"] = "你好B"
    row = pair(a, b)
    all_a = b"".join(s.bytes for s in segments(a, "openai-chat"))
    all_b = b"".join(s.bytes for s in segments(b, "openai-chat"))
    expected = 0
    while expected < len(all_a) and all_a[expected] == all_b[expected]:
        expected += 1
    assert row["sharedBytes"] == expected
    assert row["previousBytes"] == len(all_a)


def test_no_tools_or_system_identical_truncation_and_appended_system() -> None:
    a = base()
    assert pair(a, a)["class"] == "append_only"
    assert pair(a, a)["stability"] == 1
    b = clone(a)
    b["messages"] = []
    assert pair(a, b)["class"] == "history_rewritten"
    c = clone(b)
    c["messages"].append({"role": "system", "content": "new policy"})
    assert pair(b, c)["class"] == "system_changed"


def test_all_volatile_detectors_and_spans_of_at_most_120_characters() -> None:
    cases = [
        ("iso_timestamp", "at 2026-01-01T00:00:00Z", "at 2026-01-01T00:00:01Z"),
        ("date", "Date 2026-01-01", "Date 2026-01-02"),
        ("uuid", "id 12345678-1234-1234-1234-123456789abc", "id 12345678-1234-1234-1234-123456789abd"),
        ("absolute_path", "repo /home/alice/project", "repo /home/bob/project"),
        ("absolute_path", "repo C:\\Users\\alice\\project", "repo C:\\Users\\bob\\project"),
        ("counter", "counter=1", "counter=2"),
    ]
    for kind, a, b in cases:
        span = differing_span(a, b)
        assert kind in span["volatile"], f"{kind}: {js(span)}"
        assert len(span["before"]) <= 120 and len(span["after"]) <= 120
    assert len(differing_span("a" * 500, "b" * 500)["after"]) == 120
    assert differing_span("hello world", "hello reader")["volatile"] == []
    assert "counter" in report("openai_chat_volatile_system")["rows"][0]["differingSpan"]["volatile"]


def test_sensitive_looking_system_spans_are_redacted() -> None:
    a, b = base(), base()
    a["messages"].insert(0, {"role": "system", "content": "token=private-first"})
    b["messages"].insert(0, {"role": "system", "content": "token=private-second"})
    assert "private-" not in js(pair(a, b))
    assert re.search("REDACTED", pair(a, b)["differingSpan"]["after"])


def test_anthropic_chain_invariant_correlates_break_with_payload_class() -> None:
    r = report("anthropic_tool_reorder")
    assert r["usageBreaks"] == 1
    assert [[row["usage"]["expected"], row["usage"]["actual"], row["usage"]["usageBreak"]] for row in r["rows"]] == [
        [1000, 1000, False],
        [1100, 0, True],
    ]
    assert r["rows"][1]["usage"]["payloadClass"] == "tool_reordered"
    u = usage_chain(
        {"cache_read_input_tokens": 5, "cache_creation_input_tokens": 2},
        {"cache_read_input_tokens": 8},
        "anthropic",
        "append_only",
    )
    assert u["delta"] == 1
    assert u["usageBreak"] is True


def test_openai_chat_and_responses_usage_optional_writes_and_absent_measurements() -> None:
    assert report("openai_chat_volatile_system")["rows"][0]["usage"]["expected"] == 1024
    r = report("openai_responses_append")
    assert r["format"] == "openai-responses"
    assert r["rows"][0]["class"] == "append_only"
    assert r["rows"][0]["usage"]["usageBreak"] is False
    assert (
        usage_chain(
            {"input_tokens_details": {"cached_tokens": 50}},
            {"input_tokens_details": {"cached_tokens": 0}},
            "openai-responses",
            "system_changed",
        )["delta"]
        == -50
    )
    assert (
        usage_chain(
            {"prompt_tokens_details": {"cached_tokens": 50, "cache_write_tokens": 25}},
            {"prompt_tokens_details": {"cached_tokens": 75}},
            "openai-chat",
            "append_only",
        )["usageBreak"]
        is False
    )
    for invalid in [
        None,
        {},
        {"cache_read_input_tokens": -1, "cache_creation_input_tokens": 0},
        {"cache_read_input_tokens": "5", "cache_creation_input_tokens": 0},
    ]:
        assert usage_chain(invalid, {"cache_read_input_tokens": 0}, "anthropic", "append_only") is None


def test_responses_instructions_input_changes_builtin_tool_and_tool_schema() -> None:
    a = {"model": "gpt-test", "tools": [{"type": "web_search"}], "instructions": "old", "input": "hello"}
    assert pair(a, {**a, "instructions": "new"})["class"] == "system_changed"
    assert pair(a, {**a, "input": [{"role": "user", "content": "hello"}]})["class"] == "history_rewritten"
    assert (
        pair(a, {**a, "tools": [{"type": "web_search", "search_context_size": "low"}]})["class"]
        == "tool_schema_changed"
    )


def test_cache_control_ttl_changes_and_root_controls_are_visible() -> None:
    a = {
        "model": "claude-test",
        "system": [{"type": "text", "text": "policy", "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
        "messages": [],
    }
    b = clone(a)
    b["system"][0]["cache_control"]["ttl"] = "5m"
    assert pair(a, b)["class"] == "cache_control_moved"
    assert pair(a, b)["firstDivergence"]["path"] == "/system/0/cache_control/ttl"
    assert pair(a, {**a, "cache_control": {"type": "ephemeral"}})["class"] == "cache_control_moved"


def test_single_request_and_ignored_controls_explicitly_reported() -> None:
    r = report("single_request")
    assert r["stability"] is None
    assert r["pairs"] == 0
    assert r["message"] == "nothing to compare"
    assert analyze(parse_capture(js({**base(), "temperature": 0, "metadata": {}})))["ignoredFields"] == [
        "metadata",
        "temperature",
    ]


def test_schema_properties_named_cache_control_are_data_not_provider_breakpoints() -> None:
    a = {
        "model": "claude-test",
        "tools": [{"name": "read", "input_schema": {"properties": {"cache_control": {"type": "string"}}}}],
        "messages": [],
    }
    b = clone(a)
    b["tools"][0]["input_schema"]["properties"]["cache_control"]["type"] = "integer"
    assert pair(a, b)["class"] == "tool_schema_changed"
    assert pair(a, b)["firstDivergence"]["path"] == "/tools/0/input_schema/properties/cache_control/type"


def test_responses_system_developer_input_messages_keep_order_and_classify_system_drift() -> None:
    a = {
        "model": "gpt-test",
        "input": [{"role": "developer", "content": "policy A"}, {"role": "user", "content": "question"}],
    }
    b = clone(a)
    b["input"][0]["content"] = "policy B"
    assert pair(a, b)["class"] == "system_changed"
    assert pair(a, b)["firstDivergence"]["path"] == "/input/0/content"
