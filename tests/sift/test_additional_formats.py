from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

# Every module here needs the optional `sift` extra (shrinkfit, tiktoken, ...).
for _module in ("shrinkfit", "tiktoken", "json_source_map", "jsonpointer", "strip_ansi"):
    pytest.importorskip(_module)

from agentwrench.sift import summarize, verify


def test_root_failure_status_does_not_override_passed_record_status():
    raw = json.dumps(
        {
            "status": "FAILED",
            "message": "ConnectionError: actual backend failure",
            "reports": [
                {"status": "PASSED", "nodeid": f"test_case_{i}", "when": "call"} for i in range(500)
            ],
        }
    ).encode()
    result = summarize(raw, budget=900, format="json")
    assert any(
        "ConnectionError: actual backend failure" in b["text"] for b in result.document["blocks"]
    )
    assert verify(raw, result.text, budget=900) == result.tokens


@pytest.mark.parametrize(
    "marker,cause",
    [
        ("thread 'checkout' panicked at worker.rs:41:3:", "database checksum mismatch"),
        ("Bail out! checkout database unavailable", "cannot continue test plan"),
        ("not ok - checkout without number", "checksum mismatch"),
    ],
)
def test_documented_failure_markers_keep_middle_context(marker, cause):
    # Synthetic regressions for markers documented by Rust and TAP13; not a Rust run.
    raw = ("heartbeat ok\n" * 500 + marker + "\n" + cause + "\n" + "heartbeat ok\n" * 500).encode()
    result = summarize(raw, budget=500, format="log")
    shown = "".join(b["text"] for b in result.document["blocks"])
    assert marker in shown
    assert cause in shown
    assert verify(raw, result.text, budget=500) == result.tokens


def test_cli_pattern_rules_merge_with_file_rules(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text('{"keep_patterns":["/rows/*/id"]}')
    raw = json.dumps(
        {"rows": [{"id": 1, "name": "one"}, {"id": 2, "name": "two"}], "padding": "noise " * 1000}
    ).encode()
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
            "650",
            "--rules",
            str(path),
            "--keep-pattern",
            "/rows/*/name",
        ],
        input=raw,
        capture_output=True,
        timeout=15,
        env=os.environ.copy(),
    )
    assert result.returncode == 0, result.stderr
    shown = {b["pointer"] for b in json.loads(result.stdout)["blocks"]}
    assert {"/rows/0/id", "/rows/1/id", "/rows/0/name", "/rows/1/name"} <= shown
