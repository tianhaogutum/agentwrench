"""Port of test/compare-volatile.test.js."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from .helpers import cli, clone, js, read_bytes, serialize

FIELDS = ["/messages/0/content"]


def base():
    return {
        "model": "gpt-test",
        "messages": [{"role": "system", "content": "turn=1"}, {"role": "user", "content": "Stable question"}],
    }


class Captures:
    def __init__(self, directory: Path) -> None:
        self.dir = str(directory)
        self.before = str(directory / "before.jsonl")
        self.after = str(directory / "after.jsonl")
        self.config = str(directory / "fields.json")

    def save(self, path: str, entries) -> None:
        Path(path).write_text(serialize(entries), encoding="utf-8")

    def write(self, path: str, value: str | bytes) -> None:
        if isinstance(value, bytes):
            Path(path).write_bytes(value)
        else:
            Path(path).write_text(value, encoding="utf-8")


@pytest.fixture
def captures(tmp_path) -> Captures:
    c = Captures(tmp_path)
    a, b, d = base(), base(), base()
    b["messages"][0]["content"] = d["messages"][0]["content"] = "turn=2"
    b["messages"][1]["content"] = "Changed question"
    c.save(
        c.before,
        [
            {
                "request": a,
                "ts": "original",
                "usage": {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0}},
            },
            {"request": b},
        ],
    )
    c.save(c.after, [a, d])
    c.write(c.config, js(FIELDS))
    return c


def test_compare_reports_distinct_raw_configured_deltas_without_changing_inputs(captures: Captures) -> None:
    before, after, config = captures.before, captures.after, captures.config
    originals = [read_bytes(p) for p in (before, after, config)]
    raw = cli("compare", before, after, "--json")
    assert raw.status == 0, raw.stderr
    result = cli("compare", before, after, "--volatile-fields", config, "--json")
    assert result.status == 0, result.stderr
    assert result.stderr == ""
    report = json.loads(result.stdout)
    configured, volatility = report.pop("configured"), report.pop("volatility")
    assert report == json.loads(raw.stdout)
    assert [report["before"], report["after"], report["improvement"]] == [64 / 125, 64 / 125, 0]
    assert configured == {"before": 100 / 119, "after": 1, "improvement": 1 - 100 / 119}
    assert volatility["before"] == [{"path": FIELDS[0], "matchedRequests": 2}]
    assert volatility["after"] == volatility["before"]
    assert re.search("Usage describes raw requests", volatility["note"])
    reversed_report = json.loads(cli("compare", after, before, "--volatile-fields", config, "--json").stdout)
    assert reversed_report["configured"] == {"before": 1, "after": 100 / 119, "improvement": 100 / 119 - 1}
    text = cli("compare", before, after, "--volatile-fields", config)
    assert text.status == 0, text.stderr
    assert re.search(r"raw stability\t0\.512000\t0\.512000\t0\.000000", text.stdout)
    assert re.search(r"configured stability\t0\.840336\t1\.000000\t0\.159664", text.stdout)
    assert re.search("matchedRequests", text.stdout)
    for path, original in zip((before, after, config), originals):
        assert read_bytes(path) == original
    captures.write(config, "[]")
    assert cli("compare", before, after, "--volatile-fields", config, "--json").stdout == raw.stdout


def test_compare_validates_exact_pointers_independently_on_both_captures(captures: Captures) -> None:
    c = captures

    def invalid(pattern: str) -> None:
        r = cli("compare", c.before, c.after, "--volatile-fields", c.config, "--json")
        assert r.status == 2, r.stderr
        assert r.stdout == ""
        assert re.search(pattern, r.stderr)
        assert "do-not-echo" not in r.stderr

    # Failure can occur on either side, including in a later request on AFTER.
    missing = base()
    del missing["messages"][0]["content"]
    c.save(c.after, [missing, missing])
    invalid("matches no request scalar")
    c.save(c.after, [base(), base()])
    c.save(c.before, [missing, missing])
    invalid("matches no request scalar")
    c.save(c.before, [base(), base()])
    container = base()
    container["messages"][0]["content"] = [{"type": "text", "text": "private"}]
    c.save(c.after, [base(), container])
    invalid("line 2:.*scalar")
    c.save(c.after, [base(), base()])
    for value, pattern in [
        (["/messages/~2/content"], "RFC 6901"),
        (FIELDS + FIELDS, "unique"),
        (["/model"], "protected structural"),
        (["/messages/0/role"], "protected structural"),
        (["/messages/00/content"], "matches no"),
        (["/messages/-/content"], "matches no"),
        (["/request/messages/0/content"], "unmodeled"),
        (["/messages/0"], "scalar"),
        (["/messages/0/toString"], "matches no"),
        (["/metadata/time"], "unmodeled"),
    ]:
        c.write(c.config, js(value))
        invalid(pattern)
    for value in ('{"password":"do-not-echo"', "{}", bytes([0xC3, 0x28]), " " * 65537):
        c.write(c.config, value)
        invalid("malformed JSON|array|UTF-8|64 KiB")
    directory = cli("compare", c.before, c.after, "--volatile-fields", c.dir, "--json")
    assert directory.status == 2
    assert directory.stdout == ""
    assert re.search("regular file", directory.stderr)


def test_compare_retains_singleton_nulls_and_reports_partial_coverage(captures: Captures) -> None:
    c = captures
    c.save(c.before, [base()])
    c.save(c.after, [base()])
    single = cli("compare", c.before, c.after, "--volatile-fields", c.config, "--json")
    assert single.status == 0, single.stderr
    report = json.loads(single.stdout)
    assert [report["before"], report["after"], report["improvement"]] == [None, None, None]
    assert report["configured"] == {"before": None, "after": None, "improvement": None}
    missing = base()
    del missing["messages"][0]["content"]
    changed_type = base()
    changed_type["messages"][0]["content"] = None
    c.save(c.before, [base(), missing])
    c.save(c.after, [base(), changed_type])
    partial = cli("compare", c.before, c.after, "--volatile-fields", c.config, "--json")
    assert partial.status == 0, partial.stderr
    mixed = json.loads(partial.stdout)
    assert mixed["volatility"]["before"][0]["matchedRequests"] == 1
    assert mixed["volatility"]["after"][0]["matchedRequests"] == 2
    assert mixed["configured"]["before"] < 1
    assert mixed["configured"]["after"] < 1


def test_compare_forwards_format_and_escaped_pointers_mismatches_still_fail(captures: Captures) -> None:
    c = captures

    def set_text(b):
        b["system"][0]["text"] = "v2"

    def set_instructions(b):
        b["instructions"] = "v2"

    def set_flag(b):
        b["messages"][0]["a/b~\u001b"] = True

    cases = [
        (
            {"model": "claude-test", "system": [{"type": "text", "text": "v1"}], "messages": []},
            "/system/0/text",
            set_text,
        ),
        ({"model": "gpt-test", "instructions": "v1", "input": "hello"}, "/instructions", set_instructions),
        (
            {"model": "custom", "messages": [{"role": "user", "content": "hi", "a/b~\u001b": False}]},
            "/messages/0/a~1b~0\u001b",
            set_flag,
        ),
    ]
    for a, path, change in cases:
        b = clone(a)
        change(b)
        c.save(c.before, [a, b])
        c.save(c.after, [a, a])
        c.write(c.config, js([path]))
        args = [
            "compare",
            c.before,
            c.after,
            "--volatile-fields",
            c.config,
            *(["--format", "anthropic"] if a["model"] == "custom" else []),
        ]
        result = cli(*args, "--json")
        assert result.status == 0, result.stderr
        report = json.loads(result.stdout)
        assert report["before"] < 1
        assert report["after"] == 1
        assert report["improvement"] > 0
        assert report["configured"] == {"before": 1, "after": 1, "improvement": 0}
        text = cli(*args)
        assert text.status == 0
        assert "\u001b" not in text.stdout
        if a["model"] == "custom":
            assert report["format"] == "anthropic"
    c.save(c.before, [base(), base()])
    c.save(c.after, [base()])
    c.write(c.config, js(FIELDS))
    mismatch = cli("compare", c.before, c.after, "--volatile-fields", c.config, "--json")
    assert mismatch.status == 2
    assert mismatch.stdout == ""
    assert re.search("same format and request count", mismatch.stderr)
    c.save(
        c.after,
        [
            {"model": "claude-test", "messages": base()["messages"]},
            {"model": "claude-test", "messages": base()["messages"]},
        ],
    )
    mismatch = cli("compare", c.before, c.after, "--volatile-fields", c.config, "--json")
    assert mismatch.status == 2
    assert mismatch.stdout == ""
    assert re.search("same format and request count", mismatch.stderr)
    c.write(c.after, '{"password":"do-not-echo"')
    malformed = cli("compare", c.before, c.after, "--volatile-fields", c.config, "--json")
    assert malformed.status == 2
    assert malformed.stdout == ""
    assert "do-not-echo" not in malformed.stderr
    c.save(c.after, [base(), base()])
    for extra in (
        [],
        ["--volatile-fields", c.config],
        ["--policy-view", "configured"],
        ["--min-stability", "1"],
        ["--fail-on", "system_changed"],
    ):
        r = cli("compare", c.before, c.after, "--volatile-fields", *([c.config, *extra] if extra else []))
        assert r.status == 2
        assert r.stdout == ""
