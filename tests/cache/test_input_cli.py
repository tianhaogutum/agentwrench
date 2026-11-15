"""Port of test/input-cli.test.js."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from agentwrench.cache import LIMITS, CacheError, FsError, parse_capture, read_capture

from .helpers import cli, clone, js, read_bytes
from .helpers import fixture as fixture_path

BASE = {"model": "gpt-test", "messages": []}


def fixture(name: str) -> str:
    return fixture_path(f"{name}.jsonl")


def test_malformed_json_gives_line_number_without_echoing_payload() -> None:
    result = cli("analyze", fixture("malformed"))
    assert result.status == 2
    assert re.search("line 2: malformed JSON", result.stderr)
    assert result.stdout == ""
    with pytest.raises(CacheError, match="line 2: malformed JSON$"):
        parse_capture(js(BASE) + '\n{"password":"hidden"')


def test_mixed_formats_rejected_explicit_compatible_messages_interpretation_supported() -> None:
    assert cli("analyze", fixture("mixed_formats")).status == 2
    assert cli("analyze", fixture("mixed_formats"), "--format", "openai-chat").status == 0
    with pytest.raises(CacheError, match="Responses input"):
        parse_capture(js(BASE), "openai-responses")


def test_input_constraints_types_unsupported_server_state_and_duplicate_identities() -> None:
    bad = [
        None,
        [],
        12,
        {"request": None},
        {},
        {**BASE, "model": ""},
        {**BASE, "tools": {}},
        {**BASE, "tools": [{}]},
        {**BASE, "tools": [{"name": "x"}, {"name": "x"}]},
        {**BASE, "messages": [{"role": "user", "content": 1}]},
        {**BASE, "messages": [None]},
        {**BASE, "previous_response_id": "resp_1"},
        {**BASE, "conversation": "conv_1"},
        {**BASE, "functions": []},
        {"request": BASE, "usage": []},
        {"model": "gpt-test", "input": [1]},
        {"model": "gpt-test", "input": "hi", "instructions": False},
        {**BASE, "model": "claude-test", "system": {}},
    ]
    for value in bad:
        with pytest.raises(CacheError, match="^line 1:"):
            parse_capture(js(value))
    with pytest.raises(CacheError, match="no requests"):
        parse_capture("  \n")
    with pytest.raises(CacheError, match="non-finite"):
        parse_capture('{"model":"gpt-test","messages":[],"temperature":1e999}')
    assert parse_capture("\r\n" + js(BASE) + "\r\n")[0].line == 2
    nested: object = 0
    for _ in range(69):
        nested = [nested]
    with pytest.raises(CacheError, match="depth"):
        parse_capture(js({**BASE, "extra": nested}))
    with pytest.raises(CacheError, match="node"):
        parse_capture(js({**BASE, "extra": [None] * LIMITS.nodes}))
    with pytest.raises(CacheError, match="4 MiB"):
        parse_capture(js({**BASE, "extra": "a" * LIMITS.line_bytes}))
    with pytest.raises(CacheError, match="10000 records"):
        parse_capture((js(BASE) + "\n") * (LIMITS.records + 1))


def test_file_errors_invalid_utf8_oversized_and_nonregular_input(tmp_path) -> None:
    invalid = tmp_path / "invalid"
    invalid.write_bytes(bytes([0xC3, 0x28]))
    with pytest.raises(CacheError, match="UTF-8"):
        read_capture(str(invalid))
    with pytest.raises(CacheError, match="regular file"):
        read_capture(str(tmp_path))
    with pytest.raises(FsError) as error:
        read_capture(str(tmp_path / "missing"))
    assert error.value.code == "ENOENT"
    large = tmp_path / "large"
    large.write_bytes(b"")
    os.truncate(large, LIMITS.file_bytes + 1)
    with pytest.raises(CacheError, match="32 MiB"):
        read_capture(str(large))


def test_cli_json_output_and_policy_exits_are_machine_readable() -> None:
    clean = cli("analyze", fixture("anthropic_tool_reorder"), "--json")
    assert clean.status == 0
    assert clean.stderr == ""
    report = json.loads(clean.stdout)
    assert report["rows"][1]["firstDivergence"]["path"] == "/tools/0"
    assert cli("analyze", fixture("openai_chat_volatile_system"), "--fail-on", "system_changed").status == 1
    assert cli("analyze", fixture("anthropic_tool_reorder"), "--fail-on", "tool_reordered,usage_break").status == 1
    assert cli("analyze", fixture("anthropic_tool_reorder"), "--fail-on", "usage_break").status == 1
    assert cli("analyze", fixture("anthropic_tool_reorder"), "--min-stability", "0.9").status == 1
    assert cli("analyze", fixture("anthropic_tool_reorder"), "--min-stability", "0").status == 0
    single = cli("analyze", fixture("single_request"), "--min-stability", "1")
    assert single.status == 0
    assert re.search("nothing to compare", single.stdout)


def test_cli_rejects_invalid_options_and_missing_files_with_exit_2(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    sr = fixture("single_request")
    cases = [
        ["nope"],
        ["analyze"],
        ["analyze", "no-such-file"],
        ["analyze", sr, "--format", "wrong"],
        ["analyze", sr, "--fail-on", "typo"],
        ["analyze", sr, "--min-stability", "NaN"],
        ["analyze", sr, "--min-stability", "1.1"],
        ["analyze", sr, "--min-stability", "-1"],
        ["analyze", sr, "--json", "--json"],
        ["analyze", sr, "--format"],
        ["canonicalize", sr],
        ["canonicalize", sr, "--out", "unused", "--rules", "sort-tools,typo"],
        ["canonicalize", sr, "--out", "unused", "--rules", "sort-tools,sort-tools"],
        ["compare", sr, sr, "--fail-on", "append_only"],
        ["analyze", sr, "--out", "unused"],
    ]
    for args in cases:
        r = cli(*args)
        assert r.status == 2, " ".join(args)
        assert re.search("agentwrench cache:", r.stderr)
    assert not Path("unused").exists()
    assert cli("--help").status == 0
    assert cli("--version").stdout.strip() == "0.1.0"


def test_end_to_end_canonicalize_and_compare_no_overwrites_input_preserved(tmp_path) -> None:
    output = str(tmp_path / "canonical.jsonl")
    source = fixture("anthropic_tool_reorder")
    original = read_bytes(source)
    result = cli("canonicalize", source, "--out", output)
    assert result.status == 0, result.stderr
    assert read_bytes(source) == original
    comparison = cli("compare", source, output, "--json")
    assert comparison.status == 0, comparison.stderr
    r = json.loads(comparison.stdout)
    assert r["after"] == 1
    assert r["improvement"] > 0.48
    assert cli("canonicalize", source, "--out", output).status == 2
    assert cli("canonicalize", source, "--out", source).status == 2
    assert cli("compare", source, fixture("single_request")).status == 2
    once = tmp_path / "once.jsonl"
    once.write_text(js({"model": "claude-test", "messages": []}) + "\n")
    assert cli("compare", str(once), fixture("single_request")).status == 2
    assert "before\tafter\tdelta" in cli("compare", source, output).stdout


def test_untrusted_terminal_keys_are_escaped_and_paths_are_only_explicit_cli_paths(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    capture = tmp_path / "capture.jsonl"
    a = {**BASE, "tools": [{"name": "x", "input_schema": {"\u001b[31m": "old"}}]}
    b = clone(a)
    b["tools"][0]["input_schema"]["\u001b[31m"] = "new"
    capture.write_text("\n".join(js(v) for v in (a, b)) + "\n")
    result = cli("analyze", str(capture))
    assert result.status == 0
    assert "\u001b" not in result.stdout
    output = str(tmp_path / "safe.jsonl")
    path_capture = tmp_path / "paths.jsonl"
    path_capture.write_text(
        js({**BASE, "messages": [{"role": "user", "content": "../../do-not-execute.sh; $(touch forbidden)"}]})
    )
    assert cli("canonicalize", str(path_capture), "--out", output).status == 0
    assert not Path("forbidden").resolve().exists()


def test_supplied_malformed_usage_counters_fail_clearly() -> None:
    for usage in (
        {"cache_read_input_tokens": -1},
        {"prompt_tokens_details": {"cached_tokens": "5"}},
        {"input_tokens_details": []},
    ):
        with pytest.raises(CacheError, match="line 1: usage"):
            parse_capture(js({"request": BASE, "usage": usage}))


def test_module_entry_point_runs_as_a_subprocess() -> None:
    """Python addition: ``python -m agentwrench.cache`` writes UTF-8 and returns exit codes."""
    src = str(Path(__file__).resolve().parents[2] / "src")
    env = {**os.environ, "PYTHONPATH": src + os.pathsep + os.environ.get("PYTHONPATH", ""), "PYTHONIOENCODING": "ascii"}
    run = lambda *args: subprocess.run([sys.executable, "-m", "agentwrench.cache", *args], capture_output=True, env=env)  # noqa: E731
    help_result = run("--help")
    assert help_result.returncode == 0
    assert help_result.stdout.decode("utf-8").startswith("agentwrench cache 0.1.0 — offline")
    failure = run("analyze", fixture("malformed"))
    assert failure.returncode == 2
    assert failure.stderr == b'agentwrench cache: "line 2: malformed JSON"\n'
    gated = run("analyze", fixture("anthropic_tool_reorder"), "--fail-on", "usage_break")
    assert gated.returncode == 1
