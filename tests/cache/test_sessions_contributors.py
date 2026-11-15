"""Port of test/sessions-contributors.test.js."""

from __future__ import annotations

import json
import re
from pathlib import Path

from agentwrench.cache import analyze_sessions

from .helpers import AFTER, BEFORE, CONFIG, cli, js, read_bytes, serialize

FIELDS = ["/messages/0/content"]
PATHS = [BEFORE, AFTER]


def sessions_cli(*args: str):
    return cli("sessions", *args)


def contributor(path: str, pairs: int, unshared_bytes: int, class_counts):
    return {"path": path, "pairs": pairs, "unsharedBytes": unshared_bytes, "classCounts": class_counts}


def save(directory: Path, name: str, entries) -> str:
    path = directory / name
    path.write_text(serialize(entries), encoding="utf-8")
    return str(path)


def verify_totals(view, pairs: int) -> None:
    entries = 0
    for row in view["rankedPaths"]:
        assert len(row["contributors"]) == row["sessions"]
        assert len({c["path"] for c in row["contributors"]}) == row["sessions"]
        assert sum(c["pairs"] for c in row["contributors"]) == row["pairs"]
        assert sum(c["unsharedBytes"] for c in row["contributors"]) == row["unsharedBytes"]
        classes: dict[str, int] = {}
        for c in row["contributors"]:
            assert sorted(c) == ["classCounts", "pairs", "path", "unsharedBytes"]
            assert c["pairs"] > 0 and c["unsharedBytes"] > 0
            assert sum(c["classCounts"].values()) == c["pairs"]
            for key, count in c["classCounts"].items():
                classes[key] = classes.get(key, 0) + count
        assert classes == row["classCounts"]
        entries += len(row["contributors"])
    assert entries <= pairs


def test_ranked_raw_configured_paths_identify_exact_contributing_files() -> None:
    originals = [read_bytes(p) for p in (*PATHS, CONFIG)]
    r = analyze_sessions(PATHS, volatile_fields=FIELDS)
    assert [r["requests"], r["pairs"], r["stability"], r["configured"]["stability"]] == [
        4,
        2,
        0.512,
        (100 / 119 + 1) / 2,
    ]
    assert r["rankedPaths"] == [
        {
            "rank": 1,
            "path": FIELDS[0],
            "pairs": 2,
            "sessions": 2,
            "unsharedBytes": 122,
            "classCounts": {"system_changed": 2},
            "contributors": [
                contributor(PATHS[1], 1, 61, {"system_changed": 1}),
                contributor(PATHS[0], 1, 61, {"system_changed": 1}),
            ],
        }
    ]
    assert r["configured"]["rankedPaths"] == [
        {
            "rank": 1,
            "path": "/messages/1/content",
            "pairs": 1,
            "sessions": 1,
            "unsharedBytes": 19,
            "classCounts": {"history_rewritten": 1},
            "contributors": [contributor(PATHS[0], 1, 19, {"history_rewritten": 1})],
        }
    ]
    verify_totals(r, r["pairs"])
    verify_totals(r["configured"], r["pairs"])
    args = [*PATHS, "--volatile-fields", CONFIG, "--json"]
    raw = sessions_cli(*args, "--min-stability", "0.9")
    assert raw.status == 1, raw.stderr
    assert json.loads(raw.stdout) == r
    configured = sessions_cli(*args, "--policy-view", "configured", "--min-stability", "0.9")
    assert configured.status == 0, configured.stderr
    assert configured.stdout == raw.stdout
    gate = sessions_cli(*args, "--policy-view", "configured", "--fail-on", "history_rewritten")
    assert gate.status == 1, gate.stderr
    assert gate.stdout == raw.stdout
    assert sessions_cli(*PATHS[::-1], "--volatile-fields", CONFIG, "--json").stdout == raw.stdout
    assert js(analyze_sessions(PATHS[::-1], volatile_fields=FIELDS)) == js(r)
    unconfigured = analyze_sessions(PATHS)
    assert unconfigured["rankedPaths"] == r["rankedPaths"]
    assert analyze_sessions(PATHS, volatile_fields=[]) == unconfigured
    for path, original in zip((*PATHS, CONFIG), originals):
        assert read_bytes(path) == original


def test_contributors_sort_by_loss_pair_count_and_label_and_exclude_boundaries(tmp_path) -> None:
    def body(value, role="user"):
        return {
            "model": "gpt-test",
            "tools": [{"type": "function", "function": {"name": "lookup", "description": "metadata"}}],
            "messages": [{"role": role, "z": value}],
        }

    most = save(tmp_path, "z-most.jsonl", [body("A" * 16), body("B" * 16)])
    once = save(tmp_path, "a-once.jsonl", [body("A" * 12), body("B" * 12)])
    twice = save(tmp_path, "b-twice.jsonl", [body("AAAA"), body("BBBB"), body("AAAA")])
    escaped = save(tmp_path, 'A"\\\u001b.jsonl', [body("A" * 12, "system"), body("B" * 12, "system")])
    singleton = save(tmp_path, "singleton.jsonl", [body("unrelated")])
    empty = {**body("unused"), "messages": []}
    addition = save(tmp_path, "addition.jsonl", [empty, body("new system", "system")])
    stable = save(tmp_path, "stable.jsonl", [body("same"), body("same")])
    inputs = [once, most, singleton, twice, addition, escaped, stable]
    volatile_fields = ["/tools/0/function/description"]
    r = analyze_sessions(inputs, volatile_fields=volatile_fields)
    expected = [
        contributor(most, 1, 20, {"history_rewritten": 1}),
        contributor(twice, 2, 16, {"history_rewritten": 2}),
        contributor(escaped, 1, 16, {"system_changed": 1}),
        contributor(once, 1, 16, {"history_rewritten": 1}),
    ]
    for view in (r, r["configured"]):
        assert len(view["rankedPaths"]) == 1
        assert view["rankedPaths"][0]["contributors"] == expected
        assert view["rankedPaths"][0]["classCounts"] == {"history_rewritten": 4, "system_changed": 1}
        verify_totals(view, r["pairs"])
    assert r["pairs"] == 7
    assert r["breakingPairs"] == 5
    assert js(analyze_sessions(inputs[::-1], volatile_fields=volatile_fields)) == js(r)
    own_config = tmp_path / "fields.json"
    own_config.write_text(js(volatile_fields))
    text = sessions_cli(*inputs, "--volatile-fields", str(own_config))
    assert text.status == 0, text.stderr
    assert "\u001b" not in text.stdout
    rows = [line for line in text.stdout.split("\n") if line.startswith("1\t")]
    assert len(rows) == 2
    for row in rows:
        assert row.endswith(js(expected)), row
    assert re.search(r"first divergence\tclasses\tcontributors", text.stdout)


def test_maximum_file_batch_retains_compact_contributors_and_singletons_have_none(tmp_path) -> None:
    def body(content):
        return {
            "model": "gpt-test",
            "messages": [{"role": "system", "content": content}, {"role": "user", "content": "same"}],
        }

    inputs = [save(tmp_path, f"{i:02d}.jsonl", [body("old"), body("new")]) for i in range(32)]
    r = analyze_sessions(inputs, volatile_fields=FIELDS)
    assert r["pairs"] == 32
    assert len(r["rankedPaths"][0]["contributors"]) == 32
    assert [c["path"] for c in r["rankedPaths"][0]["contributors"]] == inputs
    verify_totals(r, r["pairs"])
    verify_totals(r["configured"], r["pairs"])
    assert r["configured"]["rankedPaths"] == []
    singles = [
        save(tmp_path, "single-a.jsonl", [body("different")]),
        save(tmp_path, "single-b.jsonl", [body("history")]),
    ]
    single = analyze_sessions(singles, volatile_fields=FIELDS)
    assert single["pairs"] == 0
    for view in (single, single["configured"]):
        assert view["stability"] is None
        assert view["rankedPaths"] == []
