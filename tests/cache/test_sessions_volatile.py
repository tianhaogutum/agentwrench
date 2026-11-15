"""Port of test/sessions-volatile.test.js."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agentwrench.cache import CacheError, analyze_sessions
from agentwrench.cache.jscompat import number_to_string

from .helpers import (
    AFTER,
    BEFORE,
    CONFIG,
    cli,
    clone,
    fixture,
    js,
    read_bytes,
    serialize,
    without,
)

PATHS = [BEFORE, AFTER]
FIELDS = ["/messages/0/content"]


def base():
    return {
        "model": "gpt-test",
        "messages": [{"role": "system", "content": "turn=1"}, {"role": "user", "content": "Stable question"}],
    }


def save(directory: Path, name: str, entries) -> str:
    path = directory / name
    path.write_text(serialize(entries), encoding="utf-8")
    return str(path)


RAW_RANKING = [
    {
        "rank": 1,
        "path": "/messages/0/content",
        "pairs": 2,
        "sessions": 2,
        "unsharedBytes": 122,
        "classCounts": {"system_changed": 2},
        "contributors": [
            {"path": path, "pairs": 1, "unsharedBytes": 61, "classCounts": {"system_changed": 1}}
            for path in (PATHS[1], PATHS[0])
        ],
    }
]
CONFIGURED_SUMMARY = {
    "stability": (100 / 119 + 1) / 2,
    "breakingPairs": 1,
    "usageBreaks": 0,
    "usageComparedPairs": 0,
    "classCounts": {"append_only": 1, "history_rewritten": 1},
    "rankedPaths": [
        {
            "rank": 1,
            "path": "/messages/1/content",
            "pairs": 1,
            "sessions": 1,
            "unsharedBytes": 19,
            "classCounts": {"history_rewritten": 1},
            "contributors": [
                {"path": PATHS[0], "pairs": 1, "unsharedBytes": 19, "classCounts": {"history_rewritten": 1}}
            ],
        }
    ],
}


def test_keeps_raw_summaries_and_adds_independent_configured_rankings() -> None:
    originals = [read_bytes(p) for p in (*PATHS, CONFIG)]
    raw = analyze_sessions(PATHS)
    r = analyze_sessions(PATHS, volatile_fields=FIELDS)
    assert [r["sessionCount"], r["requests"], r["pairs"], r["stability"]] == [2, 4, 2, 64 / 125]
    assert r["classCounts"] == {"system_changed": 2}
    assert r["rankedPaths"] == RAW_RANKING
    assert r["configured"] == CONFIGURED_SUMMARY
    assert re.search("Usage describes raw requests", r["volatility"]["note"])
    raw_top = without(r, "configured", "volatility", "sessions")
    assert {**raw_top, "sessions": [without(s, "configured", "volatility") for s in r["sessions"]]} == raw
    for s in r["sessions"]:
        assert s["volatility"]["fields"] == [{"path": FIELDS[0], "matchedRequests": 2}]
        first = s["path"] == PATHS[0]
        assert s["configured"] == {
            "stability": 100 / 119 if first else 1,
            "breakingPairs": 1 if first else 0,
            "usageBreaks": 0,
            "usageComparedPairs": 0,
            "classCounts": {"history_rewritten": 1} if first else {"append_only": 1},
        }
    assert js(r) == js(analyze_sessions(PATHS[::-1], volatile_fields=FIELDS))
    assert analyze_sessions(PATHS, volatile_fields=[]) == raw
    assert FIELDS == ["/messages/0/content"]
    for path, original in zip((*PATHS, CONFIG), originals):
        assert read_bytes(path) == original


def test_cli_selects_raw_configured_policy_explicitly_and_retains_original_usage_breaks(tmp_path) -> None:
    args = ["sessions", *PATHS, "--volatile-fields", CONFIG]
    r = cli(*args, "--json", "--min-stability", "0.9")
    assert r.status == 1, r.stderr
    report = json.loads(r.stdout)
    assert report["configured"] == CONFIGURED_SUMMARY
    assert cli(*args, "--json", "--policy-view", "raw", "--min-stability", "0.9").stdout == r.stdout
    configured = cli(*args, "--json", "--policy-view", "configured", "--min-stability", "0.9")
    assert configured.status == 0, configured.stderr
    assert configured.stdout == r.stdout
    assert (
        cli(
            *args, "--policy-view", "configured", "--min-stability", number_to_string(report["configured"]["stability"])
        ).status
        == 0
    )
    assert cli(*args, "--policy-view", "configured", "--min-stability", "1").status == 1
    assert cli(*args, "--fail-on", "history_rewritten").status == 0
    assert cli(*args, "--policy-view", "configured", "--fail-on", "history_rewritten").status == 1
    assert cli(*args, "--fail-on", "system_changed").status == 1
    assert cli(*args, "--policy-view", "configured", "--fail-on", "system_changed").status == 0
    text = cli(*args, "--policy-view", "configured")
    assert text.status == 0, text.stderr
    for pattern in (
        r"raw ranked paths",
        r"configured ranked paths",
        r"raw stability: 0\.512000",
        r"configured stability: 0\.920168",
        r"matchedRequests",
        r"policy view: configured",
    ):
        assert re.search(pattern, text.stdout)
    assert re.search("policy view: raw", cli(*args).stdout)
    # Both files have their own usage chain; no boundary pair may add evidence.
    b = base()
    b["messages"][0]["content"] = "turn=2"

    def wrap(body):
        return {
            "request": body,
            "ts": "preserved",
            "usage": {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0}},
        }

    usage_paths = [
        save(tmp_path, "a.jsonl", [wrap(base()), wrap(b)]),
        save(tmp_path, "b.jsonl", [wrap(base()), wrap(b)]),
    ]
    usage_args = ["sessions", *usage_paths, "--volatile-fields", CONFIG, "--policy-view", "configured", "--json"]
    payload = cli(*usage_args, "--fail-on", "system_changed")
    assert payload.status == 0, payload.stderr
    u = json.loads(payload.stdout)
    assert u["pairs"] == 2
    assert u["usageBreaks"] == 2
    assert u["configured"]["usageBreaks"] == 2
    assert u["configured"]["classCounts"] == {"append_only": 2}
    assert all(s["usageBreaks"] == 1 and s["configured"]["usageBreaks"] == 1 for s in u["sessions"])
    usage = cli(*usage_args, "--fail-on", "usage_break")
    assert usage.status == 1, usage.stderr
    assert usage.stdout == payload.stdout


def test_validates_allowlists_separately_per_file_without_partial_cli_report(tmp_path) -> None:
    good = save(tmp_path, "a-good.jsonl", [base(), base()])
    missing = base()
    del missing["messages"][0]["content"]
    container = base()
    container["messages"][0]["content"] = [{"type": "text", "text": "private"}]
    bad = save(tmp_path, "z-bad.jsonl", [missing, missing])
    own_config = tmp_path / "fields.json"
    own_config.write_text(js(FIELDS))
    for entries in ([missing, missing], [base(), container]):
        save(tmp_path, "z-bad.jsonl", entries)
        with pytest.raises(CacheError, match=r"session.*z-bad.*(?:matches no|line 2:.*scalar)"):
            analyze_sessions([good, bad], volatile_fields=FIELDS)
        r = cli("sessions", good, bad, "--volatile-fields", str(own_config), "--json")
        assert r.status == 2
        assert r.stdout == ""
        assert re.search("z-bad", r.stderr)
    for volatile_fields in (
        None,
        "bad",
        ["/messages/~2/content"],
        FIELDS + FIELDS,
        ["/model"],
        ["/messages/0/role"],
        ["/metadata/time"],
        ["/messages/0"],
        ["/messages/00/content"],
        ["/messages/0/toString"],
    ):
        with pytest.raises(CacheError, match=re.compile("volatile", re.I)):
            analyze_sessions([good], volatile_fields=volatile_fields)
        own_config.write_text(js(volatile_fields))
        r = cli("sessions", good, "--volatile-fields", str(own_config), "--json")
        assert r.status == 2
        assert r.stdout == ""
    for value in ('{"password":"do-not-echo"', bytes([0xC3, 0x28]), " " * 65537):
        if isinstance(value, bytes):
            own_config.write_bytes(value)
        else:
            own_config.write_text(value)
        r = cli("sessions", good, "--volatile-fields", str(own_config), "--json")
        assert r.status == 2
        assert r.stdout == ""
        assert "do-not-echo" not in r.stderr
    own_config.write_text("[]")
    assert (
        cli("sessions", good, "--volatile-fields", str(own_config), "--json").stdout
        == cli("sessions", good, "--json").stdout
    )
    for extra in (
        ["--policy-view", "configured"],
        ["--policy-view", "typo"],
        ["--volatile-fields", str(own_config), "--policy-view", "configured"],
        ["--volatile-fields", str(tmp_path)],
        ["--volatile-fields", CONFIG, "--volatile-fields", CONFIG],
        ["--volatile-fields"],
    ):
        r = cli("sessions", good, *extra, "--json")
        assert r.status == 2
        assert r.stdout == ""
    alias = tmp_path / "alias.jsonl"
    alias.symlink_to("a-good.jsonl")
    for invalid_paths in ([good, str(alias)], [good] * 33, [good, fixture("malformed.jsonl")]):
        r = cli("sessions", *invalid_paths, "--volatile-fields", CONFIG, "--json")
        assert r.status == 2
        assert r.stdout == ""


def test_configured_pooling_preserves_singleton_boundaries_partial_coverage_types_and_key_order(tmp_path) -> None:
    a, b = base(), base()
    b["messages"][0]["content"] = "turn=2"
    single_a, single_b = save(tmp_path, "single-a.jsonl", [a]), save(tmp_path, "single-b.jsonl", [b])
    single = analyze_sessions([single_a, single_b], volatile_fields=FIELDS)
    assert single["pairs"] == 0
    assert single["configured"]["stability"] is None
    assert single["configured"]["rankedPaths"] == []
    assert single["configured"]["classCounts"] == {}
    assert all(
        s["configured"]["stability"] is None and s["volatility"]["fields"][0]["matchedRequests"] == 1
        for s in single["sessions"]
    )
    assert (
        cli(
            "sessions",
            single_a,
            single_b,
            "--volatile-fields",
            CONFIG,
            "--policy-view",
            "configured",
            "--min-stability",
            "1",
        ).status
        == 0
    )
    stable = save(tmp_path, "stable.jsonl", [a, b, a])
    changed = save(
        tmp_path,
        "changed.jsonl",
        [a, {**b, "messages": [b["messages"][0], {"role": "user", "content": "Changed question"}]}],
    )
    pooled = analyze_sessions([single_a, stable, changed], volatile_fields=FIELDS)
    assert pooled["pairs"] == 3
    assert pooled["configured"]["stability"] == (100 / 119 + 2) / 3

    def drop(x):
        del x["messages"][0]["content"]

    def nullify(x):
        x["messages"][0]["content"] = None

    def reorder(x):
        x["messages"][0] = {"content": x["messages"][0]["content"], "role": "system"}

    for mutation in (drop, nullify, reorder):
        c = base()
        mutation(c)
        partial = save(tmp_path, "partial.jsonl", [a, c])
        r = analyze_sessions([partial], volatile_fields=FIELDS)
        assert r["configured"]["stability"] < 1
        assert r["configured"]["breakingPairs"] == 1
        assert r["configured"]["classCounts"] == {"system_changed": 1}
        assert r["sessions"][0]["volatility"]["fields"][0]["matchedRequests"] == (
            2 if "content" in c["messages"][0] else 1
        )


def test_forwards_mixed_forced_formats_and_escapes_configured_ranked_paths_and_allowlist_keys(tmp_path) -> None:
    entries = [
        base(),
        {"model": "claude-test", "messages": [{"role": "user", "content": "same"}]},
        {"model": "gpt-test", "input": "same"},
    ]
    key, pointer = "a/b~\u001b", "/tools/0/description"
    mixed = []
    for i, body in enumerate(entries):
        body["tools"] = [
            {"type": "function", "name": "read", "function": {"name": "read"}, "description": "v1", key: "old"}
        ]
        b = clone(body)
        b["tools"][0]["description"] = "v2"
        b["tools"][0][key] = "new"
        mixed.append(save(tmp_path, f"{i}.jsonl", [body, b]))
    r = analyze_sessions(mixed, volatile_fields=[pointer])
    assert [s["format"] for s in r["sessions"]] == ["openai-chat", "anthropic", "openai-responses"]
    assert r["configured"]["rankedPaths"][0]["path"] == "/tools/0/a~1b~0\u001b"
    assert r["configured"]["rankedPaths"][0]["pairs"] == 3
    assert r["configured"]["rankedPaths"][0]["sessions"] == 3
    own_config = tmp_path / "fields.json"
    own_config.write_text(js([pointer]))
    text = cli("sessions", *mixed, "--volatile-fields", str(own_config))
    assert text.status == 0, text.stderr
    assert "\u001b" not in text.stdout
    own_config.write_text(js([pointer, "/tools/0/a~1b~0\u001b"]))
    masked = cli("sessions", *mixed, "--volatile-fields", str(own_config), "--json")
    assert masked.status == 0, masked.stderr
    assert json.loads(masked.stdout)["configured"]["stability"] == 1
    assert "\u001b" not in cli("sessions", *mixed, "--volatile-fields", str(own_config)).stdout
    forced = analyze_sessions([mixed[0]], format="anthropic", volatile_fields=[pointer])
    assert forced["sessions"][0]["format"] == "anthropic"
    assert forced["configured"]["rankedPaths"][0]["pairs"] == 1
    # A pointer modeled on Chat is unmodeled on Responses, even if present there.
    with pytest.raises(CacheError, match=r"session.*2.jsonl.*unmodeled"):
        analyze_sessions([mixed[0], mixed[2]], volatile_fields=FIELDS)
