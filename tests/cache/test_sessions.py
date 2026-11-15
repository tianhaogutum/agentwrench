"""Port of test/sessions.test.js."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agentwrench.cache import SESSION_LIMITS, CacheError, analyze_sessions
from agentwrench.cache.jscompat import number_to_string

from .helpers import CAPTURE, cli, fixture, js, read_bytes, serialize


def base():
    return {"model": "gpt-test", "messages": [{"role": "user", "content": "A" * 100}]}


def save(directory: Path, name: str, entries) -> str:
    path = directory / name
    path.write_text(serialize(entries), encoding="utf-8")
    return str(path)


def example(directory: Path) -> list[str]:
    a, b, c = base(), base(), base()
    b["messages"][0]["content"] = "B" * 100
    c["model"] = "other-model"
    return [save(directory, "a.jsonl", [a, b]), save(directory, "b.jsonl", [a, b]), save(directory, "c.jsonl", [a, c])]


def test_ranks_exact_independent_prefix_losses_with_order_stable_json(tmp_path) -> None:
    paths = example(tmp_path)
    originals = [read_bytes(p) for p in paths]
    report = analyze_sessions(paths)
    assert report["schemaVersion"] == 1
    assert report["sessionCount"] == 3
    assert report["requests"] == 6
    assert report["pairs"] == 3
    assert report["breakingPairs"] == 3
    assert report["usageBreaks"] == 0
    assert report["stability"] == (58 / 162 + 58 / 162 + 10 / 162) / 3
    assert report["classCounts"] == {"history_rewritten": 2, "model_changed": 1}
    assert report["rankedPaths"] == [
        {
            "rank": 1,
            "path": "/messages/0/content",
            "pairs": 2,
            "sessions": 2,
            "unsharedBytes": 208,
            "classCounts": {"history_rewritten": 2},
            "contributors": [
                {"path": path, "pairs": 1, "unsharedBytes": 104, "classCounts": {"history_rewritten": 1}}
                for path in paths[:2]
            ],
        },
        {
            "rank": 2,
            "path": "/model",
            "pairs": 1,
            "sessions": 1,
            "unsharedBytes": 152,
            "classCounts": {"model_changed": 1},
            "contributors": [{"path": paths[2], "pairs": 1, "unsharedBytes": 152, "classCounts": {"model_changed": 1}}],
        },
    ]
    assert [s["path"] for s in report["sessions"]] == paths
    assert js(report) == js(analyze_sessions(paths[::-1]))
    assert "A" * 100 not in js(report)
    for path, original in zip(paths, originals):
        assert read_bytes(path) == original


def test_pools_pairs_never_joins_boundaries_permits_mixed_formats_and_excludes_zero_loss(tmp_path) -> None:
    a, b = base(), base()
    b["messages"][0]["content"] = "B" * 100
    changed = save(tmp_path, "changed.jsonl", [a, b])
    stable = save(tmp_path, "stable.jsonl", [a, a, a])
    single = save(tmp_path, "single.jsonl", [{"model": "claude-test", "messages": []}])
    r = analyze_sessions([stable, single, changed])
    assert r["requests"] == 6
    assert r["pairs"] == 3
    assert r["stability"] == (58 / 162 + 2) / 3
    assert r["breakingPairs"] == 1
    assert next(s for s in r["sessions"] if s["path"] == single)["stability"] is None
    assert r["classCounts"] == {"append_only": 2, "history_rewritten": 1}
    alone = analyze_sessions([single])
    assert alone["stability"] is None
    assert alone["rankedPaths"] == []
    system = save(
        tmp_path,
        "system.jsonl",
        [
            {"model": "gpt-test", "messages": []},
            {"model": "gpt-test", "messages": [{"role": "system", "content": "policy"}]},
        ],
    )
    appended = analyze_sessions([system])
    assert appended["stability"] == 1
    assert appended["breakingPairs"] == 0
    assert appended["classCounts"] == {"system_changed": 1}
    assert appended["rankedPaths"] == []


def test_rank_ties_use_pair_counts_then_locale_independent_pointer_order(tmp_path) -> None:
    paths = []
    for i, key in enumerate(["b/b", "a~a"]):
        a = {"model": "gpt-test", "messages": [{"role": "user", key: "old"}], "temperature": 0}
        b = json.loads(json.dumps(a))
        b["messages"][0][key] = "new"
        usage = {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0}}
        paths.append(save(tmp_path, f"{i}.jsonl", [{"request": a, "usage": usage}, {"request": b, "usage": usage}]))
    r = analyze_sessions(paths)
    assert r["rankedPaths"][0]["unsharedBytes"] == r["rankedPaths"][1]["unsharedBytes"]
    assert [p["path"] for p in r["rankedPaths"]] == ["/messages/0/a~0a", "/messages/0/b~1b"]
    assert r["usageBreaks"] == 2
    assert [s["ignoredFields"] for s in r["sessions"]] == [["temperature"], ["temperature"]]

    # Both paths lose 16 bytes in total; the second path occurs in two pairs.
    def message(key, value):
        return {"model": "gpt-test", "messages": [{"role": "user", key: value}]}

    long = save(tmp_path, "long.jsonl", [message("a", "A" * 12), message("a", "B" * 12)])
    frequent = save(tmp_path, "frequent.jsonl", [message("z", "AAAA"), message("z", "BBBB"), message("z", "AAAA")])
    ties = analyze_sessions([long, frequent])["rankedPaths"]
    assert [[p["path"], p["unsharedBytes"], p["pairs"]] for p in ties] == [
        ["/messages/0/z", 16, 2],
        ["/messages/0/a", 16, 1],
    ]


def test_validation_bounds_counts_rejects_duplicate_files_and_preserves_per_file_validation(tmp_path) -> None:
    paths = example(tmp_path)
    assert (SESSION_LIMITS.files, SESSION_LIMITS.records) == (32, 20000)
    for invalid in ([], None, "x", [1], [""], ["missing"] * 33):
        with pytest.raises(CacheError, match=re.compile("session", re.I)):
            analyze_sessions(invalid)
    with pytest.raises(CacheError, match=re.compile("duplicate", re.I)):
        analyze_sessions([paths[0], paths[0]])
    link = tmp_path / "alias.jsonl"
    link.symlink_to("a.jsonl")
    with pytest.raises(CacheError, match=re.compile("duplicate", re.I)):
        analyze_sessions([paths[0], str(link)])
    with pytest.raises(CacheError, match=re.compile("format", re.I)):
        analyze_sessions([paths[0]], format="typo")
    with pytest.raises(CacheError, match=re.compile("regular file", re.I)):
        analyze_sessions([str(tmp_path)])
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"password":"do-not-echo"')
    with pytest.raises(CacheError, match="line 1: malformed JSON"):
        analyze_sessions([paths[0], str(bad)])
    many = [{"model": "gpt-test", "messages": []}] * 10000
    large1, large2 = save(tmp_path, "large1.jsonl", many), save(tmp_path, "large2.jsonl", many)
    single = save(tmp_path, "single.jsonl", [base()])
    assert analyze_sessions([large1, large2])["requests"] == 20000
    with pytest.raises(CacheError, match=re.compile("20000 records", re.I)):
        analyze_sessions([large1, large2, single])


def test_cli_emits_stable_json_and_gates_pooled_stability_classes_usage(tmp_path) -> None:
    paths = example(tmp_path)
    result = cli("sessions", *paths, "--json", "--min-stability", "0.3")
    assert result.status == 1, result.stderr
    r = json.loads(result.stdout)
    assert r["rankedPaths"][0]["unsharedBytes"] == 208
    assert cli("sessions", *paths, "--json").stdout == cli("sessions", *paths[::-1], "--json").stdout
    threshold = number_to_string(r["stability"])
    assert cli("sessions", *paths, "--min-stability", threshold).status == 0
    assert cli("sessions", *paths, "--fail-on", "model_changed").status == 1
    assert cli("sessions", *paths, "--fail-on", "system_changed").status == 0
    usage = cli("sessions", CAPTURE, "--fail-on", "usage_break", "--json")
    assert usage.status == 1
    assert json.loads(usage.stdout)["usageBreaks"] == 1
    single = cli("sessions", fixture("single_request.jsonl"), "--min-stability", "1", "--json")
    assert single.status == 0
    assert json.loads(single.stdout)["stability"] is None
    text = cli("sessions", *paths)
    assert text.status == 0
    assert re.search("208", text.stdout)
    assert re.search(r"/messages/0/content", text.stdout)
    assert re.search(r"stability: 0\.259259", text.stdout)
    for args in (
        [],
        [*paths, "--min-stability", "2"],
        [*paths, "--fail-on", "typo"],
        [paths[0], paths[0]],
        [paths[0]] * 33,
        [*paths, "--policy-view", "configured"],
        [paths[0], fixture("malformed.jsonl")],
    ):
        invalid = cli("sessions", *args, "--json")
        assert invalid.status == 2, invalid.stderr
        assert invalid.stdout == ""
    terminal = save(
        tmp_path,
        "terminal\u001b.jsonl",
        [
            {"model": "gpt-test", "messages": [{"role": "user", "\u001b": "old"}]},
            {"model": "gpt-test", "messages": [{"role": "user", "\u001b": "new"}]},
        ],
    )
    assert "\u001b" not in cli("sessions", terminal).stdout
