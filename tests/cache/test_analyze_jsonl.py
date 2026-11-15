"""Port of test/analyze-jsonl.test.js."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .helpers import (
    CAPTURE,
    CONFIG,
    Saver,
    cli,
    clone,
    fixture,
    js,
    read_bytes,
    read_entries,
    without,
)

FIELDS = CONFIG


def entries():
    return read_entries(CAPTURE)


def check_export(args: list[str], expected_exit: int = 0, input_format: str | None = None):
    result_json = cli("analyze", *args, *(["--format", input_format] if input_format else []), "--json")
    result = cli("analyze", *args, "--format", "jsonl", *(["--input-format", input_format] if input_format else []))
    assert result_json.status == expected_exit, result_json.stderr
    assert result.status == expected_exit, result.stderr
    # A policy failure explains itself in one stderr line; stdout stays the JSONL contract.
    if expected_exit == 1:
        assert result.stderr.startswith("agentwrench cache: policy failed") and result.stderr.count("\n") == 1
        assert result_json.stderr == result.stderr
    else:
        assert result.stderr == ""
    records = [json.loads(line) for line in result.stdout.rstrip().split("\n")]
    assert result.stdout == "\n".join(js(r) for r in records) + "\n"
    report = json.loads(result_json.stdout)
    rows, configured = report["rows"], report.get("configured")
    summary = {"schemaVersion": 1, "type": "summary", "raw": without(report, "rows", "configured", "volatility")}
    if configured:
        summary["configured"] = without(configured, "rows")
        summary["volatility"] = report["volatility"]
    assert records[0] == summary
    assert len(records) == report["pairs"] + 1
    for index, row in enumerate(rows):
        pair = {"schemaVersion": 1, "type": "pair", "pairIndex": index, "raw": row}
        if configured:
            pair["configured"] = configured["rows"][index]
        assert records[index + 1] == pair
        if configured:
            assert pair["configured"]["usage"] == pair["raw"]["usage"]
    return records, result.stdout


def test_jsonl_exports_exact_ordered_summaries_rows_for_all_dialects_usage_states_and_allowlists(tmp_path) -> None:
    save = Saver(tmp_path)
    consistent = entries()
    consistent[1]["usage"]["prompt_tokens_details"]["cached_tokens"] = 1024
    singleton = save.entries("singleton.jsonl", entries()[:1])
    missing = save.entries("missing.jsonl", [without(e, "usage") for e in entries()])
    stable = save.entries("consistent.jsonl", consistent)
    empty = save("empty.json", "[]\n")
    for path, coverage, breaks in [(CAPTURE, 1, 1), (missing, 0, 0), (stable, 1, 0), (singleton, 0, 0)]:
        before, config_before = read_bytes(path), read_bytes(FIELDS)
        _, raw_stdout = check_export([path])
        assert check_export([path, "--volatile-fields", empty])[1] == raw_stdout
        records, _ = check_export([path, "--volatile-fields", FIELDS])
        summary = records[0]
        pair = records[1] if len(records) > 1 else None
        for view in (summary["raw"], summary["configured"]):
            assert view["usageComparedPairs"] == coverage
            assert view["usageBreaks"] == breaks
        if path == singleton:
            assert summary["raw"]["pairs"] == 0
            assert summary["raw"]["stability"] is None
            assert summary["configured"]["stability"] is None
            assert pair is None
        else:
            assert pair["raw"]["class"] == "system_changed"
            assert pair["raw"]["firstDivergence"]["path"] == "/messages/0/content"
            assert pair["configured"]["class"] == "append_only"
            assert pair["configured"]["firstDivergence"] is None
            if coverage:
                assert pair["configured"]["usage"]["payloadClass"] == "system_changed"
                assert pair["raw"]["usage"]["expected"] == 1024
                assert pair["raw"]["usage"]["actual"] == (0 if breaks else 1024)
            else:
                assert pair["raw"]["usage"] is None
                assert re.search("No chain comparison", pair["raw"]["usageNote"])
        assert read_bytes(path) == before
        assert read_bytes(FIELDS) == config_before
    for name in ("anthropic_tool_reorder", "openai_responses_append", "single_request"):
        check_export([fixture(f"{name}.jsonl")])
    # Pair ordinal is independent of physical line numbers, and missing evidence breaks both adjacent chains.
    a, b = entries()
    mixed = save("mixed.jsonl", "\n" + "\n\n".join(js(v) for v in (a, b, a, {"request": a["request"]}, b)) + "\n")
    records, _ = check_export([mixed, "--volatile-fields", FIELDS])
    summary, pairs = records[0], records[1:]
    assert summary["raw"]["pairs"] == 4
    assert summary["raw"]["usageComparedPairs"] == 2
    assert summary["raw"]["usageBreaks"] == 2
    assert [[p["pairIndex"], p["raw"]["from"], p["raw"]["to"]] for p in pairs] == [
        [0, 2, 4],
        [1, 4, 6],
        [2, 6, 8],
        [3, 8, 10],
    ]
    assert [p["raw"]["usage"] is not None for p in pairs] == [True, True, False, False]


def test_jsonl_preserves_complete_output_for_raw_configured_class_stability_and_usage_policies() -> None:
    args = [CAPTURE, "--volatile-fields", FIELDS]
    plain = check_export(args)[1]
    for policy in ("raw", "configured"):
        assert check_export([*args, "--policy-view", policy, "--fail-on", "usage_break"], 1)[1] == plain
        assert (
            check_export(
                [*args, "--policy-view", policy, "--fail-on", "system_changed", "--min-stability", "1"],
                1 if policy == "raw" else 0,
            )[1]
            == plain
        )
        assert (
            check_export(
                [*args, "--policy-view", policy, "--fail-on", "append_only"], 1 if policy == "configured" else 0
            )[1]
            == plain
        )


def test_jsonl_validates_every_input_config_option_before_stdout(tmp_path) -> None:
    save = Saver(tmp_path)
    directory = str(tmp_path)
    values = entries()
    prefix = "\n".join(js(v) for v in values) + "\n"
    invalid = clone(values[1])
    invalid["usage"]["prompt_tokens_details"]["cached_tokens"] = -1
    container = clone(values[1])
    container["request"]["messages"][0]["content"] = []
    empty = save("empty.json", "[]")
    bad_config = save("bad-config.json", "{bad")
    unmatched = save("unmatched.json", '["/messages/9/content"]')
    cases = [
        [save("malformed.jsonl", prefix + "{bad\n")],
        [save.entries("invalid-usage.jsonl", [*values, invalid])],
        [save.entries("container.jsonl", [*values, container]), "--volatile-fields", FIELDS],
        [fixture("mixed_formats.jsonl")],
        [save("empty.jsonl", "\n")],
        [directory],
        [str(Path(directory, "missing"))],
        [CAPTURE, "--volatile-fields", bad_config],
        [CAPTURE, "--volatile-fields", unmatched],
        [CAPTURE, "--volatile-fields", directory],
        [CAPTURE, "--volatile-fields", str(Path(directory, "absent"))],
        [CAPTURE, "--policy-view", "configured"],
        [CAPTURE, "--policy-view", "typo"],
        [CAPTURE, "--volatile-fields", empty, "--policy-view", "configured"],
        [CAPTURE, "--fail-on", "typo"],
        [CAPTURE, "--min-stability", "NaN"],
        [CAPTURE, "--json"],
        [CAPTURE, "--format", "auto"],
        [CAPTURE, "--format", "jsonl"],
    ]
    for args in cases:
        result = cli("analyze", *args, "--format", "jsonl")
        assert result.status == 2, js(args)
        assert result.stdout == "", js(args)
        assert result.stderr
    for args in (
        ["sessions", CAPTURE],
        ["compare", CAPTURE, CAPTURE],
        ["canonicalize", CAPTURE, "--out", str(Path(directory, "out"))],
    ):
        result = cli(*args, "--format", "jsonl")
        assert result.status == 2
        assert result.stdout == ""


def test_jsonl_safely_encodes_control_characters_in_breaking_pointers(tmp_path) -> None:
    save = Saver(tmp_path)
    a, b = entries()
    key = 'meta"/~\u001b\t\n'
    b["request"]["messages"][0]["content"] = a["request"]["messages"][0]["content"]
    a["request"]["messages"][0][key] = "old"
    b["request"]["messages"][0][key] = "new"
    pointer = "/messages/0/" + key.replace("~", "~0").replace("/", "~1")
    config = save("fields.json", js([pointer]))
    path = save.entries("escaped.jsonl", [a, b])
    records, stdout = check_export([path, "--volatile-fields", config])
    pair = records[1]
    assert pair["raw"]["firstDivergence"]["path"] == pointer
    assert pair["configured"]["class"] == "append_only"
    assert "\u001b" not in stdout
    assert "\t" not in stdout
    assert js(pointer) in stdout


def test_explicit_jsonl_capture_dialect_matches_legacy_forced_json(tmp_path) -> None:
    save = Saver(tmp_path)
    mixed = fixture("mixed_formats.jsonl")
    original = read_bytes(mixed)
    records, stdout = check_export([mixed], 0, "openai-chat")
    summary, pair = records
    assert summary["raw"]["format"] == "openai-chat"
    assert summary["raw"]["requests"] == 2
    assert summary["raw"]["pairs"] == 1
    assert pair["pairIndex"] == 0
    assert [pair["raw"]["from"], pair["raw"]["to"]] == [1, 2]
    assert "configured" not in summary
    assert "configured" not in pair
    reordered = cli("analyze", "--input-format", "openai-chat", mixed, "--format", "jsonl")
    assert reordered.status == 0
    assert reordered.stderr == ""
    assert reordered.stdout == stdout
    for selection in ([], ["--input-format", "auto"]):
        result = cli("analyze", mixed, "--format", "jsonl", *selection)
        assert result.status == 2
        assert result.stdout == ""
        assert re.search("mixed formats", result.stderr)
    empty = save("empty.json", "[]")
    singleton = save.entries("singleton.jsonl", entries()[:1])
    for path, dialect in [
        (CAPTURE, "openai-chat"),
        (fixture("anthropic_tool_reorder.jsonl"), "anthropic"),
        (fixture("openai_responses_append.jsonl"), "openai-responses"),
        (singleton, "openai-chat"),
    ]:
        raw_records, raw_stdout = check_export([path], 0, dialect)
        assert check_export([path, "--volatile-fields", empty], 0, dialect)[1] == raw_stdout
        assert check_export([path], 0, "auto")[1] == check_export([path])[1]
        if path == singleton:
            assert len(raw_records) == 1
            assert raw_records[0]["raw"]["stability"] is None
            assert raw_records[0]["raw"]["usageComparedPairs"] == 0
    # Explicit selection preserves physical line numbering as well as ordered pair ordinals.
    spaced = save("spaced.jsonl", "\n" + "\n\n".join(original.decode().strip().split("\n")) + "\n")
    assert [
        [p["pairIndex"], p["raw"]["from"], p["raw"]["to"]] for p in check_export([spaced], 0, "openai-chat")[0][1:]
    ] == [[0, 2, 4]]
    assert read_bytes(mixed) == original


def test_explicit_jsonl_dialect_retains_configured_original_usage_and_complete_policy_exits(tmp_path) -> None:
    save = Saver(tmp_path)
    singleton = save.entries("singleton.jsonl", entries()[:1])
    args = [CAPTURE, "--volatile-fields", FIELDS]
    (summary, pair), stdout = check_export(args, 0, "openai-chat")
    assert summary["raw"]["usageBreaks"] == 1
    assert summary["configured"]["usageComparedPairs"] == 1
    assert pair["raw"]["class"] == "system_changed"
    assert pair["configured"]["class"] == "append_only"
    assert pair["raw"]["usage"] == pair["configured"]["usage"]
    assert pair["configured"]["usage"]["payloadClass"] == "system_changed"
    for view in ("raw", "configured"):
        selected = [*args, "--policy-view", view]
        assert check_export([*selected, "--fail-on", "usage_break"], 1, "openai-chat")[1] == stdout
        assert (
            check_export([*selected, "--fail-on", "system_changed"], 1 if view == "raw" else 0, "openai-chat")[1]
            == stdout
        )
        assert check_export([*selected, "--min-stability", "1"], 1 if view == "raw" else 0, "openai-chat")[1] == stdout
        assert (
            check_export([*selected, "--fail-on", "append_only"], 1 if view == "configured" else 0, "openai-chat")[1]
            == stdout
        )
    records, _ = check_export(
        [singleton, "--volatile-fields", FIELDS, "--policy-view", "configured", "--min-stability", "1"],
        0,
        "openai-chat",
    )
    assert len(records) == 1
    assert records[0]["configured"]["stability"] is None
    assert records[0]["configured"]["usageBreaks"] == 0


def test_jsonl_input_selectors_and_selected_dialect_validation_fail_atomically(tmp_path) -> None:
    save = Saver(tmp_path)
    bad_usage = clone(entries()[1])
    bad_usage["usage"]["prompt_tokens_details"]["cached_tokens"] = -1
    bad_system = {"model": "gpt-test", "messages": [], "system": {}}
    bad_messages = {"model": "gpt-test", "input": "hello"}
    bad_input = {"model": "gpt-test", "messages": []}
    for values, dialect, error in [
        ([*entries(), bad_usage], "openai-chat", "usage"),
        ([{"model": "gpt-test", "messages": []}, bad_system], "anthropic", "line 2: system must"),
        ([{"model": "gpt-test", "input": "ok"}, bad_input], "openai-responses", "line 2: Responses input"),
        ([*entries(), bad_messages], "openai-chat", "line 3: messages must"),
    ]:
        path = save.entries(dialect + ".jsonl", values)
        legacy = cli("analyze", path, "--format", dialect, "--json")
        result = cli("analyze", path, "--format", "jsonl", "--input-format", dialect)
        assert result.status == 2
        assert result.stdout == ""
        assert re.search(error, result.stderr)
        assert result.stderr == legacy.stderr
        assert legacy.status == 2
        assert legacy.stdout == ""
    empty = save("empty.json", "[]")
    cases = [
        [CAPTURE, "--format", "jsonl", "--input-format", "typo"],
        [CAPTURE, "--format", "jsonl", "--input-format", "jsonl"],
        [CAPTURE, "--format", "jsonl", "--input-format", ""],
        [CAPTURE, "--format", "jsonl", "--input-format"],
        [CAPTURE, "--input-format", "--format", "jsonl"],
        [CAPTURE, "--format", "jsonl", "--input-format", "auto", "--input-format", "auto"],
        [CAPTURE, "--input-format", "openai-chat", "--format", "jsonl", "--input-format", "anthropic"],
        [CAPTURE, "--format", "jsonl", "--input-format", "openai-chat", "--json"],
        [CAPTURE, "--format", "jsonl", "--input-format", "openai-chat", "--format", "auto"],
        *[
            [CAPTURE, *(["--format", format] if format else []), "--input-format", "openai-chat", *mode]
            for format in (None, "auto", "openai-chat", "anthropic")
            for mode in ([], ["--json"])
        ],
        [CAPTURE, "--format", "jsonl", "--input-format", "openai-chat", "--policy-view", "configured"],
        [
            CAPTURE,
            "--format",
            "jsonl",
            "--input-format",
            "openai-chat",
            "--volatile-fields",
            empty,
            "--policy-view",
            "configured",
        ],
        [CAPTURE, "--format", "jsonl", "--input-format", "openai-chat", "--volatile-fields", save("bad.json", "{bad")],
    ]
    for args in cases:
        result = cli("analyze", *args)
        assert result.status == 2, js(args)
        assert result.stdout == "", js(args)
        assert result.stderr
    for args in (["sessions", CAPTURE], ["compare", CAPTURE, CAPTURE], ["canonicalize", CAPTURE, "--out", "unused"]):
        result = cli(*args, "--input-format", "openai-chat")
        assert result.status == 2
        assert result.stdout == ""
        assert re.search("unknown option", result.stderr)
