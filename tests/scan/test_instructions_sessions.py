"""Sections 2 and 3: AGENTS.md budget, CLAUDE.md imports, session pairing."""
from __future__ import annotations

import shlex

from agentwrench.doctor.engine import scan as doctor_scan

from .conftest import (claude_clean, claude_orphan, codex_call, codex_clean, codex_orphan, codex_output,
                       codex_prompt, run, run_json)


def agents_md(size: int) -> str:
    head = "# Project\n\n## Build\n\n"
    body = "".join(f"- rule {i}: keep things tidy and tested.\n" for i in range(size // 40))
    return head + body + "\n## Security\n\n- never commit secrets\n\n## Release\n\n- tag on Tuesdays\n"


def test_agents_md_truncation(setup, capsys):
    text = agents_md(40_000)
    (setup.project / "AGENTS.md").write_text(text)
    (setup.home / ".codex").mkdir()
    (setup.home / ".codex" / "AGENTS.md").write_text("# Global\n\n- be brief\n")
    code, doc = run_json(capsys, setup.args("--sessions", "0"))
    codex = doc["instructions"]["codex"]
    assert code == 0 and codex["truncated"] and codex["limit_bytes"] == 32768
    assert codex["project_bytes_loaded"] == 32768
    assert codex["dropped_bytes"] == len(text.encode()) - 32768
    assert codex["cuts"][0]["dropped_headings"][-2:] == ["## Security", "## Release"]
    assert codex["estimated_tokens"] == -(-codex["rendered_bytes"] // 4)
    finding = next(f for f in doc["findings"] if f["area"] == "instructions")
    assert finding["severity"] == "error" and '"Security", "Release"' in finding["message"]
    assert finding["command"] == [f"agentwrench agentsmd explain --cwd {shlex.quote(str(setup.project))} "
                                  f"--codex-home {shlex.quote(str(setup.home / '.codex'))}"]
    code, out, _ = run(capsys, setup.args("--sessions", "0", "--no-color"))
    assert "truncated" in out and "agentwrench agentsmd explain" in out


def test_agents_md_budget_from_config_and_high_usage(setup, capsys):
    (setup.project / "AGENTS.md").write_text(agents_md(3_000))
    setup.codex_config("project_doc_max_bytes = 3500\n")
    code, doc = run_json(capsys, setup.args("--sessions", "0"))
    codex = doc["instructions"]["codex"]
    assert codex["limit_bytes"] == 3500 and not codex["truncated"] and codex["usage"] > 0.8
    assert any("of Codex's 3,500-byte budget" in f["message"] for f in doc["findings"])


def test_claude_md_imports_and_user_memory(setup, capsys):
    (setup.project / "docs").mkdir()
    (setup.project / "docs" / "a.md").write_text("A" * 1000)
    (setup.project / "CLAUDE.md").write_text("# P\n@docs/a.md\n@docs/missing.md\n")
    (setup.home / ".claude").mkdir()
    (setup.home / ".claude" / "CLAUDE.md").write_text("U" * 200)
    code, doc = run_json(capsys, setup.args("--sessions", "0"))
    claude = doc["instructions"]["claude-code"]
    total = 200 + len("# P\n@docs/a.md\n@docs/missing.md\n") + 1000
    assert claude["loaded_bytes"] == total and claude["estimated_tokens"] == -(-total // 4)
    assert claude["imports"] == 2
    assert {f["scope"] for f in claude["files"]} >= {"user", "ancestor", "import"}
    finding = next(f for f in doc["findings"] if "import" in f["message"])
    assert "docs/missing.md (file not found)" in finding["message"] and "--agent claude" in finding["command"][0]
    assert doc["summary"]["clients"]["claude-code"]["instruction_tokens"] == claude["estimated_tokens"]


def test_large_claude_memory_warning(setup, capsys):
    (setup.project / "CLAUDE.md").write_text("x" * 45_000)
    code, doc = run_json(capsys, setup.args("--sessions", "0"))
    assert any(f["severity"] == "warning" and "45,000 bytes" in f["message"] for f in doc["findings"])


def test_sessions_good_and_broken(setup, capsys):
    setup.claude_session("good", claude_clean())
    bad = setup.claude_session("bad", claude_orphan())
    setup.codex_session("good", codex_clean())
    codex_bad = setup.codex_session("bad", codex_orphan())
    code, doc = run_json(capsys, setup.args())
    claude, codex = doc["sessions"]["claude-code"], doc["sessions"]["codex"]
    assert (claude["checked"], claude["clean"], claude["broken"]) == (2, 1, 1)
    assert (codex["checked"], codex["clean"], codex["broken"]) == (2, 1, 1)
    assert claude["files"][0]["path"] == str(bad) and claude["files"][0]["issues"] == {"RD201": 1}
    assert codex["files"][0]["path"] == str(codex_bad) and codex["files"][0]["issues"] == {"RD101": 1}
    assert claude["example_command"] == [f"agentwrench doctor repair {shlex.quote(str(bad))} --dry-run",
                                         f"agentwrench doctor repair {shlex.quote(str(bad))} --in-place --backup"]
    assert sum(f["area"] == "sessions" for f in doc["findings"]) == 2
    # The suggested command really repairs the file (dry run only; nothing written).
    from agentwrench.doctor import cli as doctor_cli
    assert doctor_cli.main(["repair", str(bad), "--dry-run"]) == 0
    capsys.readouterr()
    code, out, _ = run(capsys, setup.args("--no-color"))
    assert "1 broken" in out and "agentwrench doctor repair" in out


def test_sessions_limit_picks_most_recent(setup, capsys):
    setup.claude_session("old-bad", claude_orphan(), age=7200)
    setup.claude_session("new-good", claude_clean(), age=60 * 60)
    code, doc = run_json(capsys, setup.args("--sessions", "1"))
    assert doc["sessions"]["claude-code"]["checked"] == 1 and doc["sessions"]["claude-code"]["broken"] == 0
    code, doc = run_json(capsys, setup.args("--sessions", "0"))
    assert doc["sessions"]["claude-code"]["checked"] == 0


def test_live_session_is_not_counted_as_broken(setup, capsys):
    setup.claude_session("running", claude_orphan(prompt_after=False), age=5)
    setup.claude_session("moved-on", claude_orphan(prompt_after=True), age=5)
    setup.codex_session("running", [{"type": "session_meta", "payload": {"id": "z"}}, codex_prompt("go"),
                                    codex_call("c1"), codex_output("c1"), codex_call("c2")], age=5)
    setup.claude_session("stale", claude_orphan(prompt_after=False), age=3 * 3600, folder="-other")
    code, doc = run_json(capsys, setup.args())
    claude, codex = doc["sessions"]["claude-code"], doc["sessions"]["codex"]
    status = {row["path"].rsplit("/", 1)[1]: row["status"] for row in claude["files"]}
    assert status == {"running.jsonl": "live", "moved-on.jsonl": "broken", "stale.jsonl": "broken"}
    assert codex["live"] == 1 and codex["broken"] == 0


def test_unreadable_and_subagent_files(setup, capsys):
    setup.claude_session("good", claude_clean())
    nested = setup.home / ".claude" / "projects" / "-proj" / "sess" / "subagents" / "agent.jsonl"
    nested.parent.mkdir(parents=True)
    nested.write_text("not json\n")
    garbage = setup.claude_session("garbage", [])
    garbage.write_bytes(b"\xff\xfe not a session\n")
    code, doc = run_json(capsys, setup.args())
    claude = doc["sessions"]["claude-code"]
    assert claude["checked"] == 2  # subagent logs are not top-level sessions
    assert doctor_scan is not None
    assert claude["broken"] + claude["errors"] == 1
