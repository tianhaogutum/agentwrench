"""Policy failures explain themselves on stderr; stdout and exit codes are unchanged."""

from __future__ import annotations

from pathlib import Path

from .helpers import cli

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "cache"
REORDER = str(EXAMPLES / "anthropic_tool_reorder.jsonl")
SESSIONS = [str(EXAMPLES / "sessions" / name) for name in ("a.jsonl", "b.jsonl", "c.jsonl")]


def test_analyze_fail_on_names_the_matched_classes_on_stderr_only() -> None:
    plain = cli("analyze", REORDER)
    for extra in ([], ["--json"], ["--format", "jsonl"]):
        baseline = cli("analyze", REORDER, *extra)
        failed = cli("analyze", REORDER, *extra, "--fail-on", "tool_reordered,usage_break")
        assert failed.status == 1
        assert failed.stdout == baseline.stdout
        assert failed.stderr == (
            "agentwrench cache: policy failed: --fail-on matched tool_reordered (1 pair), usage_break (1 pair)\n"
        )
    assert plain.stderr == ""


def test_analyze_min_stability_names_the_threshold() -> None:
    failed = cli("analyze", REORDER, "--min-stability", "0.9")
    assert failed.status == 1
    assert failed.stderr == "agentwrench cache: policy failed: stability 0.511052 is below --min-stability 0.9\n"
    passed = cli("analyze", REORDER, "--min-stability", "0.5", "--fail-on", "system_changed")
    assert passed.status == 0 and passed.stderr == ""


def test_sessions_fail_on_counts_pairs_and_combines_reasons() -> None:
    failed = cli("sessions", *SESSIONS, "--json", "--fail-on", "model_changed,history_rewritten",
                 "--min-stability", "1")
    assert failed.status == 1
    assert failed.stdout == cli("sessions", *SESSIONS, "--json").stdout
    assert failed.stderr == (
        "agentwrench cache: policy failed: --fail-on matched model_changed (1 pair), history_rewritten (2 pairs); "
        "stability 0.259259 is below --min-stability 1\n"
    )


def test_text_estimated_tokens_round_up_like_slim() -> None:
    text = cli("analyze", REORDER).stdout
    assert "\t1965\t492\t" in text and "\t46\t12\t" in text
    assert "491.25" not in text
    assert '"sharedEstimatedTokens": 491.25' in cli("analyze", REORDER, "--json").stdout


def test_canonicalize_onto_an_existing_file_names_the_option(tmp_path) -> None:
    out = tmp_path / "canonical.jsonl"
    assert cli("canonicalize", REORDER, "--out", out).status == 0
    again = cli("canonicalize", REORDER, "--out", out)
    assert again.status == 2 and again.stdout == ""
    assert again.stderr == 'agentwrench cache: "--out already exists (or is a symlink); choose a new file"\n'
