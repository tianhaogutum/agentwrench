"""Command line: help, usage errors, JSON schema, --fail-over, color, read-only, demo."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

from agentwrench import cli as top
from agentwrench.scan import SCHEMA, cli

from .conftest import ROOT, claude_orphan, fake, run, run_json

SCHEMA_FILE = ROOT / "docs" / "scan" / "scan.schema.json"
DEMO = ROOT / "examples" / "scan"


def test_help_mentions_probe_executes(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: agentwrench scan")
    assert "--probe" in out and "EXECUTES" in out and "never uses the network" in out
    assert "--fail-over" in out and "--timeout" in out and "--sessions" in out


def test_routed_from_top_level_and_listed(capsys):
    assert top.main(["scan", "--home", "/nonexistent-dir-for-scan-test"]) == 2
    assert "not a directory" in capsys.readouterr().err
    top.main(["--help"])
    out = capsys.readouterr().out
    line = next(line for line in out.splitlines() if line.startswith("  scan "))
    assert "coming soon" not in line


@pytest.mark.parametrize("argv", [["--timeout", "0"], ["--timeout", "abc"], ["--sessions", "-1"],
                                  ["--fail-over", "x"], ["--bogus"]])
def test_usage_errors_exit_2(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2


def test_missing_project_exits_2(setup, capsys, tmp_path):
    code, out, err = run(capsys, ["--home", str(setup.home), "--project", str(tmp_path / "nope")])
    assert code == 2 and out == "" and "--project: not a directory" in err


def test_json_matches_documented_schema(setup, capsys):
    setup.mcp_json({"tracker": fake(), "web": {"type": "http", "url": "https://example.invalid/mcp"}})
    setup.claude_session("bad", claude_orphan())
    (setup.project / "AGENTS.md").write_text("# A\n" + "x" * 40_000)
    schema = json.loads(SCHEMA_FILE.read_text())
    for extra in ((), ("--probe", "--fail-over", "10")):
        code, doc = run_json(capsys, setup.args(*extra))
        jsonschema.validate(doc, schema)
        assert doc["schema"] == SCHEMA == "agentwrench.scan/v1"
        assert doc["home"] == str(setup.home) and doc["project"] == str(setup.project)
        assert doc["summary"]["problems"] == len(doc["findings"])
    assert code == 1 and doc["summary"]["over_limit"]


def test_fail_over(setup, capsys):
    (setup.project / "CLAUDE.md").write_text("x" * 4000)  # 1,000 tokens
    code, doc = run_json(capsys, setup.args("--fail-over", "999"))
    assert code == 1 and doc["summary"]["max_fixed_overhead_tokens"] == 1000
    code, doc = run_json(capsys, setup.args("--fail-over", "1000"))
    assert code == 0
    code, out, _ = run(capsys, setup.args("--fail-over", "999", "--no-color"))
    assert code == 1 and "--fail-over 999: FAIL" in out
    setup.mcp_json({"tracker": fake()})
    code, doc = run_json(capsys, setup.args("--probe", "--fail-over", "5000"))
    assert code == 1 and doc["summary"]["clients"]["claude-code"]["mcp_tokens"] > 5000


class TTY:
    def __init__(self, stream):
        self.stream = stream

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def isatty(self):
        return True


@pytest.mark.parametrize("no_color_env,flag,expect", [(None, (), True), ("1", (), False), (None, ("--no-color",), False)])
def test_color_only_on_tty_without_no_color(setup, capsys, monkeypatch, no_color_env, flag, expect):
    if no_color_env is None:
        monkeypatch.delenv("NO_COLOR", raising=False)
    else:
        monkeypatch.setenv("NO_COLOR", no_color_env)
    (setup.project / "CLAUDE.md").write_text("@missing.md\n")
    monkeypatch.setattr(sys, "stdout", TTY(sys.stdout))
    code = cli.main(setup.args(*flag))
    out = capsys.readouterr().out
    assert code == 0 and ("\x1b[" in out) is expect


def test_not_a_tty_means_no_color(setup, capsys, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    code, out, _ = run(capsys, setup.args())
    assert "\x1b[" not in out


def digest_tree(*roots: Path) -> dict:
    result = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            info = path.lstat()
            data = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
            result[str(path)] = (info.st_mtime_ns, info.st_mode, data)
    return result


def test_scan_is_read_only(setup, capsys):
    setup.claude_json({"tracker": fake()})
    setup.mcp_json({"tracker2": fake()})
    setup.claude_session("bad", claude_orphan())
    (setup.project / "AGENTS.md").write_text("x" * 40_000)
    before = digest_tree(setup.home, setup.project)
    for extra in ((), ("--probe",), ("--json", "--probe")):
        run(capsys, setup.args(*extra))
    assert digest_tree(setup.home, setup.project) == before


def test_empty_home_is_clean(setup, capsys):
    code, out, _ = run(capsys, setup.args("--no-color"))
    assert code == 0 and "No MCP servers configured" in out and "No problems found." in out


def test_demo_example(capsys):
    if shutil.which("python3") is None:
        pytest.skip("demo config runs servers with python3 from PATH")
    code, doc = run_json(capsys, ["--home", str(DEMO / "demo-home"), "--project", str(DEMO / "demo-project"),
                                  "--probe"])
    assert code == 0
    status = {(s["client"], s["name"]): s["status"] for s in doc["mcp"]["servers"]}
    assert status == {
        ("claude-code", "github"): "measured", ("claude-code", "linear"): "remote",
        ("claude-code", "sentry"): "failed", ("claude-code", "playwright"): "measured",
        ("claude-code", "issue-tracker"): "measured", ("codex", "github"): "measured",
        ("codex", "postgres"): "measured",
    }
    assert doc["instructions"]["codex"]["truncated"]
    assert doc["sessions"]["claude-code"]["broken"] == 1 and doc["sessions"]["codex"]["broken"] == 0
    assert [f["tokens_saved"] > 0 for f in doc["summary"]["top_fixes"]] == [True, True, False]
    text = json.dumps(doc)
    assert "demo-not-a-real-token" not in text and "demo-password" not in text


def test_module_entry_point():
    result = subprocess.run([sys.executable, "-m", "agentwrench.scan", "--version"], capture_output=True, text=True,
                            timeout=60)
    assert result.returncode == 0 and result.stdout.startswith("agentwrench scan ")
