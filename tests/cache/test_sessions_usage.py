"""Port of test/sessions-usage.test.js."""

from __future__ import annotations

import pytest

from agentwrench.cache import CacheError, analyze, analyze_sessions, parse_capture
from agentwrench.cache.jscompat import MAX_SAFE_INTEGER, is_safe_integer

from .helpers import (
    CAPTURE,
    CONFIG,
    Saver,
    cli,
    js,
    read_bytes,
    read_entries,
    read_text,
    without,
)

FIELDS = ["/messages/0/content"]
RAW_HEADER = "session\tformat\tpairs\tstability\traw classes\traw breaking pairs\tcaptured usage breaks\tcaptured usage compared pairs"
CONFIGURED_HEADER = (
    "session\tconfigured stability\tconfigured classes\tvolatile fields\tconfigured breaking pairs\t"
    "captured usage breaks\tcaptured usage compared pairs"
)


def sessions_cli(*args: str):
    return cli("sessions", *args)


class Setup:
    def __init__(self, directory) -> None:
        self.save = Saver(directory)
        entries = read_entries(CAPTURE)
        self.changed = self.save.entries('changed"\\\t\n\u001b.jsonl', entries)
        self.missing = self.save.entries("missing.jsonl", [without(e, "usage") for e in entries])
        self.singleton = self.save.entries("singleton.jsonl", entries[:1])
        self.inputs = [self.changed, self.missing, self.singleton]


def totals(r) -> None:
    for configured in (False, True):
        view = r.get("configured") if configured else r
        if not view:
            continue
        total = 0
        for s in r["sessions"]:
            v = s["configured"] if configured else s
            assert is_safe_integer(v["usageComparedPairs"])
            assert v["usageBreaks"] <= v["usageComparedPairs"] <= s["pairs"]
            total += v["usageComparedPairs"]
        assert view["usageComparedPairs"] == total
        assert view["usageBreaks"] <= total <= r["pairs"]


def check_text(text: str, r) -> None:
    lines = text.rstrip().split("\n")
    for header, configured in ((RAW_HEADER, False), (CONFIGURED_HEADER, True)):
        at = lines.index(header) if header in lines else -1
        if configured and "configured" not in r:
            assert at == -1
            continue
        assert at >= 0, "original captured usage coverage is labeled"
        for i, s in enumerate(r["sessions"]):
            row = lines[at + 1 + i].split("\t")
            v = s["configured"] if configured else s
            assert row[0] == js(s["path"])
            assert row[-3:] == [str(v["breakingPairs"]), str(v["usageBreaks"]), str(v["usageComparedPairs"])]
    assert len([line for line in lines if line == f"captured usage compared pairs: {r['usageComparedPairs']}"]) == (
        2 if "configured" in r else 1
    )
    assert "\u001b" not in text
    assert text.endswith(r["note"] + "\n"), "complete policy-failure output"


def test_session_usage_coverage_counts_original_evidence_separately_from_breaks(tmp_path) -> None:
    setup = Setup(tmp_path)
    inputs, changed, missing, singleton = setup.inputs, setup.changed, setup.missing, setup.singleton
    originals = [read_bytes(p) for p in (*inputs, CONFIG)]
    r = analyze_sessions(inputs, volatile_fields=FIELDS)
    assert r["usageComparedPairs"] == 1
    assert r["configured"]["usageComparedPairs"] == 1
    assert r["pairs"] == 2
    assert r["usageBreaks"] == 1
    assert r["configured"]["usageBreaks"] == 1
    assert r["configured"]["rankedPaths"] == []
    for s in r["sessions"]:
        for v in (s, s["configured"]):
            assert v["usageComparedPairs"] == (1 if s["path"] == changed else 0)
    assert next(s for s in r["sessions"] if s["path"] == missing)["configured"]["classCounts"] == {"append_only": 1}
    assert next(s for s in r["sessions"] if s["path"] == singleton)["stability"] is None
    totals(r)
    assert js(analyze_sessions(inputs[::-1], volatile_fields=FIELDS)) == js(r)
    for policy in ("raw", "configured"):
        args = [*inputs, "--volatile-fields", CONFIG, "--policy-view", policy]
        ok = sessions_cli(*args)
        assert ok.status == 0, ok.stderr
        check_text(ok.stdout, r)
        fail = sessions_cli(*args, "--fail-on", "usage_break")
        assert fail.status == 1, fail.stderr
        assert fail.stdout == ok.stdout
        result_json = sessions_cli(*args, "--json", "--fail-on", "usage_break")
        assert result_json.status == 1, result_json.stderr
        assert result_json.stdout == js(r, 2) + "\n"
        reverse = sessions_cli(*inputs[::-1], "--volatile-fields", CONFIG, "--policy-view", policy, "--json")
        assert reverse.status == 0, reverse.stderr
        assert reverse.stdout == result_json.stdout
    alone = sessions_cli(
        singleton,
        "--volatile-fields",
        CONFIG,
        "--policy-view",
        "configured",
        "--min-stability",
        "1",
        "--fail-on",
        "usage_break",
    )
    assert alone.status == 0, alone.stderr
    check_text(alone.stdout, analyze_sessions([singleton], volatile_fields=FIELDS))
    for path, original in zip((*inputs, CONFIG), originals):
        assert read_bytes(path) == original


def with_usage(request, usage):
    """``{ request, usage }`` where an undefined usage is omitted by JSON.stringify."""
    return {"request": request} if usage is ... else {"request": request, "usage": usage}


def test_session_coverage_follows_usage_chain_requirements_zero_counters_and_fallbacks(tmp_path) -> None:
    setup = Setup(tmp_path)
    chat = {"model": "gpt-test", "messages": [{"role": "system", "content": "policy"}]}
    responses = {"model": "gpt-test", "instructions": "policy", "input": []}
    anthropic = {"model": "claude-test", "system": "policy", "messages": []}

    def details(cached_tokens):
        return {"prompt_tokens_details": {"cached_tokens": cached_tokens}}

    maximum = MAX_SAFE_INTEGER
    cases = [
        (chat, {}, {}, 0, 0),
        (chat, {"prompt_tokens": 0}, details(0), 0, 0),
        (chat, details(0), details(0), 1, 0),  # cached-only fallback, valid zeros
        (chat, {**details(0), "prompt_tokens": 7}, details(7), 1, 0),
        (chat, {"prompt_tokens_details": {"cached_tokens": 2, "cache_write_tokens": 3}}, details(5), 1, 0),
        (chat, {"prompt_tokens_details": {"cached_tokens": maximum, "cache_write_tokens": 1}}, details(0), 0, 0),
        (
            responses,
            {"input_tokens_details": {"cached_tokens": 2}},
            {"input_tokens_details": {"cached_tokens": 0}},
            1,
            1,
        ),
        (
            anthropic,
            {"cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            {"cache_read_input_tokens": 0},
            1,
            0,
        ),
        (anthropic, {"cache_read_input_tokens": 1}, {"cache_read_input_tokens": 1}, 0, 0),
        (
            anthropic,
            {"cache_read_input_tokens": maximum, "cache_creation_input_tokens": 1},
            {"cache_read_input_tokens": 0},
            0,
            0,
        ),
        (chat, ..., details(0), 0, 0),
        (chat, details(0), ..., 0, 0),
    ]
    inputs = []
    for i, (request, previous, nxt, compared, breaks) in enumerate(cases):
        path = setup.save.entries(f"case-{i}.jsonl", [with_usage(request, previous), with_usage(request, nxt)])
        inputs.append(path)
        rows = analyze(parse_capture(read_text(path)))["rows"]
        assert len([row for row in rows if row["usage"] is not None]) == compared
        r = analyze_sessions([path])
        assert r["usageComparedPairs"] == compared, f"case {i}"
        assert r["usageBreaks"] == breaks, f"case {i}"
        totals(r)
        assert "rows" not in r["sessions"][0]
    batch = analyze_sessions(inputs)
    totals(batch)
    assert batch["usageComparedPairs"] == 5
    assert batch["usageBreaks"] == 1
    assert batch["pairs"] == 12


def test_coverage_preserves_allowlists_and_independent_validation_without_partial_report(tmp_path) -> None:
    setup = Setup(tmp_path)
    save, inputs, missing, singleton = setup.save, setup.inputs, setup.missing, setup.singleton
    empty = save.entries("empty.json", [[]])
    raw = analyze_sessions(inputs)
    assert raw["usageComparedPairs"] == 1
    totals(raw)
    assert analyze_sessions(inputs, volatile_fields=[]) == raw
    plain = sessions_cli(*inputs, "--json")
    assert plain.status == 0, plain.stderr
    empty_json = sessions_cli(*inputs, "--volatile-fields", empty, "--json")
    assert empty_json.status == 0, empty_json.stderr
    assert empty_json.stdout == plain.stdout
    no_evidence = sessions_cli(missing, singleton, "--fail-on", "usage_break")
    assert no_evidence.status == 0, no_evidence.stderr
    check_text(no_evidence.stdout, analyze_sessions([missing, singleton]))
    request = {"model": "gpt-test", "messages": []}
    absent = save.entries("z-unmatched.jsonl", [{"request": request}])
    invalid_usage = [
        save.entries(f"z-invalid-{i}.jsonl", [{"request": request, "usage": usage}])
        for i, usage in enumerate(
            [
                {"prompt_tokens_details": {"cached_tokens": -1}},
                {"prompt_tokens_details": {"cached_tokens": "0"}},
                {"prompt_tokens_details": []},
                {"cache_creation_input_tokens": float(MAX_SAFE_INTEGER + 1)},
            ]
        )
    ]
    for args in [
        *[[*inputs, path] for path in invalid_usage],
        [*inputs, absent, "--volatile-fields", CONFIG],
        [*inputs, "--volatile-fields", empty, "--policy-view", "configured"],
        [*inputs, "--fail-on", "unknown"],
    ]:
        for mode in ([], ["--json"]):
            bad = sessions_cli(*args, *mode)
            assert bad.status == 2, bad.stderr
            assert bad.stdout == ""
            assert bad.stderr
    with pytest.raises(CacheError, match="matches no request"):
        analyze_sessions([*inputs, absent], volatile_fields=FIELDS)
