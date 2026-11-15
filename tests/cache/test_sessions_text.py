"""Port of test/sessions-text.test.js."""

from __future__ import annotations

import json
import re
from pathlib import Path

from agentwrench.cache import analyze_sessions

from .helpers import (
    AFTER,
    BEFORE,
    CAPTURE,
    CONFIG,
    cli,
    js,
    read_bytes,
    read_text,
    score,
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
    def __init__(self, directory: Path) -> None:
        self.dir = directory
        data = read_bytes(CAPTURE)
        self.changed = str(directory / 'changed"\\\t\n\u001b.jsonl')
        self.singleton = str(directory / "singleton.jsonl")
        Path(self.changed).write_bytes(data)
        Path(self.singleton).write_text(data.decode().split("\n")[0] + "\n", encoding="utf-8")
        self.empty = str(directory / "empty.json")
        Path(self.empty).write_text("[]")
        self.inputs = [self.changed, self.singleton]


def check_text(text: str, report) -> None:
    lines = text.rstrip().split("\n")
    raw = lines.index(RAW_HEADER)
    configured = lines.index(CONFIGURED_HEADER) if CONFIGURED_HEADER in lines else -1
    assert (configured >= 0) == ("configured" in report)
    for i, s in enumerate(report["sessions"]):
        assert lines[raw + i + 1] == (
            f"{js(s['path'])}\t{s['format']}\t{s['pairs']}\t{score(s['stability'])}\t{js(s['classCounts'])}\t"
            f"{s['breakingPairs']}\t{s['usageBreaks']}\t{s['usageComparedPairs']}"
        )
        if "configured" in s:
            c = s["configured"]
            assert lines[configured + i + 1] == (
                f"{js(s['path'])}\t{score(c['stability'])}\t{js(c['classCounts'])}\t{js(s['volatility']['fields'])}\t"
                f"{c['breakingPairs']}\t{c['usageBreaks']}\t{c['usageComparedPairs']}"
            )
    assert "\u001b" not in text
    assert text.endswith(report["note"] + "\n"), "policy failures retain the complete report"


def check_pooled_classes(text: str, report) -> None:
    lines = text.rstrip().split("\n")
    expected = [("raw ", report), ("configured ", report["configured"])] if "configured" in report else [("", report)]
    assert len([line for line in lines if re.match(r"^(raw |configured )?classes: ", line)]) == len(expected)
    for prefix, view in expected:
        at = lines.index(f"{prefix}stability: {score(view['stability'])}")
        assert lines[at + 1] == f"{prefix}classes: {js(view['classCounts'])}"
        assert lines[at + 2] == f"breaking pairs: {view['breakingPairs']}; usage breaks: {view['usageBreaks']}"
        assert lines[at + 3] == f"captured usage compared pairs: {view['usageComparedPairs']}"
        assert "usage_break" not in view["classCounts"]
    check_text(text, report)


def test_pooled_session_classes_include_configured_append_only_pairs_and_complete_policies() -> None:
    paths = [BEFORE, AFTER]
    originals = [read_bytes(p) for p in (*paths, CONFIG)]
    report = analyze_sessions(paths, volatile_fields=FIELDS)
    assert report["classCounts"] == {"system_changed": 2}
    assert js(report["configured"]["classCounts"]) == '{"append_only":1,"history_rewritten":1}'
    assert report["configured"]["rankedPaths"][0]["classCounts"] == {"history_rewritten": 1}
    for policy in ("raw", "configured"):
        args = [*paths, "--volatile-fields", CONFIG, "--policy-view", policy]
        ok = sessions_cli(*args)
        assert ok.status == 0, ok.stderr
        check_pooled_classes(ok.stdout, report)
        reversed_result = sessions_cli(*paths[::-1], "--volatile-fields", CONFIG, "--policy-view", policy)
        assert reversed_result.status == 0, reversed_result.stderr
        assert reversed_result.stdout == ok.stdout
        for gate, status in [
            (["--min-stability", "0.9"], 1 if policy == "raw" else 0),
            (["--fail-on", "append_only"], 1 if policy == "configured" else 0),
            (["--fail-on", "history_rewritten"], 1 if policy == "configured" else 0),
            (["--fail-on", "system_changed"], 1 if policy == "raw" else 0),
        ]:
            result = sessions_cli(*args, *gate)
            assert result.status == status, result.stderr
            assert result.stdout == ok.stdout
            result_json = sessions_cli(*args, *gate, "--json")
            assert result_json.status == status, result_json.stderr
            assert result_json.stdout == js(report, 2) + "\n"
    for path, original in zip((*paths, CONFIG), originals):
        assert read_bytes(path) == original


def test_pooled_session_classes_retain_zero_loss_classes_original_usage_and_singletons(tmp_path) -> None:
    setup = Setup(tmp_path)
    body = {"model": "gpt-test", "tools": [{"name": "lookup", "description": "metadata"}], "messages": []}
    system = {**body, "messages": [{"role": "system", "content": "policy"}]}

    def wrap(request):
        return {"request": request, "usage": {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0}}}

    changed = str(tmp_path / 'zero-loss"\\\t\n\u001b.jsonl')
    singleton = str(tmp_path / "zero-singleton.jsonl")
    Path(changed).write_text("\n".join(js(wrap(b)) for b in (body, system, system)) + "\n", encoding="utf-8")
    Path(singleton).write_text(js(wrap(body)) + "\n", encoding="utf-8")
    own_config = str(tmp_path / "fields.json")
    Path(own_config).write_text('["/tools/0/description"]')
    originals = [read_bytes(p) for p in (changed, singleton, own_config, setup.empty)]
    for inputs in ([changed, singleton], [singleton]):
        for args, volatile_fields in (
            ([], []),
            (["--volatile-fields", setup.empty], []),
            (["--volatile-fields", own_config], ["/tools/0/description"]),
        ):
            report = analyze_sessions(inputs, volatile_fields=volatile_fields)
            has_pairs = len(inputs) == 2
            for view in (report, report.get("configured")):
                if not view:
                    continue
                assert view["classCounts"] == ({"append_only": 1, "system_changed": 1} if has_pairs else {})
                assert view["rankedPaths"] == []
                assert view["breakingPairs"] == 0
                assert view["usageBreaks"] == (2 if has_pairs else 0)
                assert view["usageComparedPairs"] == (2 if has_pairs else 0)
            for policy in ("raw", "configured") if "configured" in report else ("raw",):
                selected = [*inputs, *args, "--policy-view", policy]
                ok = sessions_cli(*selected, "--min-stability", "1")
                assert ok.status == 0, ok.stderr
                check_pooled_classes(ok.stdout, report)
                for gate in ("system_changed", "append_only", "usage_break"):
                    result = sessions_cli(*selected, "--fail-on", gate)
                    assert result.status == (1 if has_pairs else 0), result.stderr
                    assert result.stdout == ok.stdout
        assert sessions_cli(*inputs).stdout == sessions_cli(*inputs, "--volatile-fields", setup.empty).stdout
    for path, original in zip((changed, singleton, own_config, setup.empty), originals):
        assert read_bytes(path) == original


def test_session_text_locates_breaks_and_usage_failures_beside_escaped_labels(tmp_path) -> None:
    setup = Setup(tmp_path)
    inputs, changed, singleton = setup.inputs, setup.changed, setup.singleton
    originals = [read_bytes(p) for p in (*inputs, CONFIG)]
    report = analyze_sessions(inputs, volatile_fields=FIELDS)
    s = next(x for x in report["sessions"] if x["path"] == changed)
    assert report["pairs"] == 1
    assert s["breakingPairs"] == 1
    assert s["configured"]["breakingPairs"] == 0
    assert s["configured"]["classCounts"] == {"append_only": 1}
    assert report["configured"]["rankedPaths"] == []
    assert s["usageBreaks"] == 1
    assert s["configured"]["usageBreaks"] == 1
    single = next(x for x in report["sessions"] if x["path"] == singleton)
    for view in (single, single["configured"]):
        assert view["breakingPairs"] == 0
        assert view["usageBreaks"] == 0
        assert view["stability"] is None
    for policy in ("raw", "configured"):
        args = [*inputs, "--volatile-fields", CONFIG, "--policy-view", policy]
        ok = sessions_cli(*args)
        assert ok.status == 0, ok.stderr
        check_text(ok.stdout, report)
        failure = sessions_cli(*args, "--fail-on", "usage_break")
        assert failure.status == 1, failure.stderr
        assert failure.stdout == ok.stdout
        reversed_result = sessions_cli(*inputs[::-1], "--volatile-fields", CONFIG, "--policy-view", policy)
        assert reversed_result.status == 0, reversed_result.stderr
        assert reversed_result.stdout == ok.stdout
        result_json = sessions_cli(*args, "--json", "--fail-on", "usage_break")
        assert result_json.status == 1, result_json.stderr
        assert result_json.stdout == js(report, 2) + "\n"
    alone = sessions_cli(
        singleton,
        "--volatile-fields",
        CONFIG,
        "--policy-view",
        "configured",
        "--fail-on",
        "usage_break",
        "--min-stability",
        "1",
    )
    assert alone.status == 0, alone.stderr
    check_text(alone.stdout, analyze_sessions([singleton], volatile_fields=FIELDS))
    for path, original in zip((*inputs, CONFIG), originals):
        assert read_bytes(path) == original


def test_session_text_counters_cover_allowlists_and_missing_usage_without_partial_tables(tmp_path) -> None:
    setup = Setup(tmp_path)
    inputs, empty = setup.inputs, setup.empty
    raw_report = analyze_sessions(inputs)
    raw = sessions_cli(*inputs, "--fail-on", "usage_break")
    assert raw.status == 1, raw.stderr
    check_text(raw.stdout, raw_report)
    empty_text = sessions_cli(*inputs, "--volatile-fields", empty, "--fail-on", "usage_break")
    assert empty_text.status == 1, empty_text.stderr
    assert empty_text.stdout == raw.stdout
    missing_usage = str(tmp_path / "missing-usage.jsonl")
    lines = read_text(CAPTURE).strip().split("\n")
    Path(missing_usage).write_text(
        "\n".join(js(json.loads(line)["request"]) for line in lines) + "\n", encoding="utf-8"
    )
    no_usage = analyze_sessions([missing_usage], volatile_fields=FIELDS)
    assert no_usage["sessions"][0]["usageBreaks"] == 0
    assert no_usage["sessions"][0]["configured"]["usageBreaks"] == 0
    ok = sessions_cli(missing_usage, "--volatile-fields", CONFIG, "--fail-on", "usage_break")
    assert ok.status == 0, ok.stderr
    check_text(ok.stdout, no_usage)
    bad = str(tmp_path / "z-invalid-singleton.jsonl")
    Path(bad).write_text(js({"model": "gpt-test", "messages": []}) + "\n")
    for mode in ([], ["--json"]):
        for args in (
            [*inputs, bad, "--volatile-fields", CONFIG],
            [*inputs, "--volatile-fields", empty, "--policy-view", "configured"],
            [*inputs, "--fail-on", "unknown"],
        ):
            invalid = sessions_cli(*args, *mode)
            assert invalid.status == 2, invalid.stderr
            assert invalid.stdout == ""
            assert invalid.stderr
