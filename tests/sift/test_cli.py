from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

# Every module here needs the optional `sift` extra (shrinkfit, tiktoken, ...).
for _module in ("shrinkfit", "tiktoken", "json_source_map", "jsonpointer", "strip_ansi"):
    pytest.importorskip(_module)

import tiktoken


def cli(*args, data=b""):
    return subprocess.run(
        [sys.executable, "-m", "agentwrench", "sift", *args],
        input=data,
        capture_output=True,
        timeout=15,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def test_stdin_stats_and_exact_stdout_budget():
    result = cli(
        "summarize", "-", "--format", "json", "--budget", "300", "--stats", data=b'{"ok":true}'
    )
    assert result.returncode == 0
    assert result.stdout.endswith(b"}")
    assert not result.stdout.endswith(b"\n")
    measured = len(tiktoken.get_encoding("cl100k_base").encode_ordinary(result.stdout.decode()))
    assert json.loads(result.stderr) == {"tokens": measured, "budget": 300}


def test_budget_error_has_no_partial_stdout():
    result = cli("summarize", "-", "--format", "json", "--budget", "5", data=b'{"ok":true}')
    assert result.returncode == 3
    assert result.stdout == b""
    assert json.loads(result.stderr)["error"] == "budget_exceeded"


def test_invalid_json_and_missing_files():
    bad = cli("summarize", "-", "--format", "json", "--budget", "300", data=b'{"x":')
    assert bad.returncode == 2
    assert b"Traceback" not in bad.stderr
    absent = cli("summarize", "does-not-exist.json", "--format", "json", "--budget", "300")
    assert absent.returncode == 4
    assert json.loads(absent.stderr)["error"] == "io_error"


def test_rules_merge_and_recovery(tmp_path):
    rules = tmp_path / "rules.json"
    rules.write_text('{"keep":["/first"]}')
    raw = json.dumps(
        {"first": "café", "noise": list(range(200)), "last": "EOF"}, ensure_ascii=False
    ).encode()
    result = cli(
        "summarize",
        "-",
        "--format",
        "json",
        "--budget",
        "450",
        "--rules",
        str(rules),
        "--keep",
        "/last",
        data=raw,
    )
    assert result.returncode == 0
    document = json.loads(result.stdout)
    assert {"/first", "/last"} <= {b["pointer"] for b in document["blocks"]}
    summary = tmp_path / "summary.json"
    summary.write_bytes(result.stdout)
    block = next(b for b in document["blocks"] if b["pointer"] == "/first")
    start, end = block["bytes"]
    read = cli("recover", "-", "--summary", str(summary), "--bytes", f"{start}:{end}", data=raw)
    assert read.returncode == 0
    assert read.stdout == raw[start:end]
    changed = cli(
        "recover", "-", "--summary", str(summary), "--bytes", f"{start}:{end}", data=raw + b" "
    )
    assert changed.returncode == 2
    assert changed.stdout == b""


def test_double_stdin_is_rejected_before_reading():
    result = cli(
        "summarize", "-", "--rules", "-", "--format", "json", "--budget", "300", data=b"{}"
    )
    assert result.returncode == 2
    assert "both read stdin" in json.loads(result.stderr)["message"]


def test_invalid_rule_file(tmp_path):
    path = tmp_path / "rules.json"
    for value in ({"typo": []}, {"keep": "x"}, {"keep_lines": [None]}, {"context_lines": True}):
        path.write_text(json.dumps(value))
        result = cli(
            "summarize",
            "-",
            "--rules",
            str(path),
            "--format",
            "json",
            "--budget",
            "300",
            data=b"{}",
        )
        assert result.returncode == 2
        assert result.stdout == b""
        assert b"Traceback" not in result.stderr


def test_closed_output_pipe_fails_without_interpreter_traceback():
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "agentwrench",
                "sift",
                "summarize",
                "-",
                "--format",
                "json",
                "--budget",
                "500",
            ],
            input=b'{"ok":true}',
            stdout=write_fd,
            stderr=subprocess.PIPE,
            timeout=15,
        )
    finally:
        os.close(write_fd)
    assert result.returncode == 4
    assert json.loads(result.stderr)["message"] == "output pipe closed"
    assert b"Traceback" not in result.stderr


def test_duplicate_rule_keys_are_not_silently_last_wins(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text('{"keep":["/error"],"keep":[]}')
    result = cli(
        "summarize",
        "-",
        "--rules",
        str(path),
        "--format",
        "json",
        "--budget",
        "500",
        data=b'{"error":"EOF"}',
    )
    assert result.returncode == 2
    assert result.stdout == b""
    assert "duplicate" in json.loads(result.stderr)["message"]


def test_verify_cli_checks_actual_saved_serialization(tmp_path):
    source = tmp_path / "input.json"
    source.write_bytes(b'{"ok":true}')
    result = cli("summarize", str(source), "--format", "json", "--budget", "500")
    summary = tmp_path / "summary.json"
    summary.write_bytes(result.stdout)
    checked = cli("verify", str(source), "--summary", str(summary), "--budget", "500")
    assert checked.returncode == 0
    assert json.loads(checked.stdout)["verified"] is True
    broken = json.loads(result.stdout)
    broken["blocks"][0]["text"] = "changed"
    summary.write_text(json.dumps(broken))
    checked = cli("verify", str(source), "--summary", str(summary))
    assert checked.returncode == 2
    assert checked.stdout == b""


def test_stdout_is_utf8_even_when_terminal_encoding_is_ascii():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agentwrench",
            "sift",
            "summarize",
            "-",
            "--format",
            "json",
            "--budget",
            "500",
        ],
        input='{"message":"café 注文 😀"}'.encode(),
        capture_output=True,
        timeout=15,
        env={**os.environ, "PYTHONIOENCODING": "ascii"},
    )
    assert result.returncode == 0, result.stderr
    assert "café 注文 😀" in result.stdout.decode("utf-8")
