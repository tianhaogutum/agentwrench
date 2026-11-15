"""Section 1: MCP inventory, --probe, dedupe, failures and redaction."""
from __future__ import annotations

import asyncio
import json
import sys

import pytest

from agentwrench.slim import DiscoveryError, measure
from agentwrench.slim.discover import run_discover

from .conftest import FAKE_SERVER, FIXTURES, PY, SECRET, fake, run, run_json

TOOLS_40 = json.loads((FAKE_SERVER.parent / "tools-40.json").read_text())["tools"]


def by_name(doc: dict, client: str, name: str) -> dict:
    return next(s for s in doc["mcp"]["servers"] if s["client"] == client and s["name"] == name)


def test_without_probe_lists_servers_and_starts_nothing(setup, capsys, tmp_path):
    marker = tmp_path / "started"
    starter = {"command": PY, "args": ["-c", f"open({str(marker)!r}, 'w')"]}
    setup.claude_json({"global-one": starter}, projects={str(setup.project): {"mcpServers": {"local-one": fake()}}})
    setup.mcp_json({"proj-one": fake(), "remote": {"type": "http", "url": "https://example.invalid/mcp"}})
    setup.codex_config(f'[mcp_servers.codex-one]\ncommand = {json.dumps(PY)}\nargs = ["-c", "pass"]\n')
    code, out, err = run(capsys, setup.args("--no-color"))
    assert code == 0, err
    for name in ("global-one", "local-one", "proj-one", "remote", "codex-one"):
        assert name in out
    assert "--probe" in out and "starts each configured stdio server" in out
    assert not marker.exists()
    code, doc = run_json(capsys, setup.args())
    scopes = {(s["client"], s["name"]): (s["scope"], s["status"]) for s in doc["mcp"]["servers"]}
    assert scopes[("claude-code", "global-one")] == ("global", "listed")
    assert scopes[("claude-code", "local-one")] == ("local", "listed")
    assert scopes[("claude-code", "proj-one")] == ("project", "listed")
    assert scopes[("claude-code", "remote")] == ("project", "remote")
    assert scopes[("codex", "codex-one")] == ("global", "listed")
    assert doc["mcp"]["probed"] is False
    assert doc["summary"]["clients"]["claude-code"]["mcp_tokens"] is None
    assert not marker.exists()


def test_probe_measures_fake_server_and_suggests_slim(setup, capsys):
    setup.mcp_json({"tracker": fake()})
    code, doc = run_json(capsys, setup.args("--probe"))
    assert code == 0
    server = by_name(doc, "claude-code", "tracker")
    expected = measure(TOOLS_40)
    assert server["status"] == "measured"
    assert (server["tools"], server["bytes"], server["estimated_tokens"]) == (
        40, expected["bytes"], expected["estimatedTokens"])
    assert [t["name"] for t in server["top_tools"]] == [t["name"] for t in expected["top"][:3]]
    assert server["slim"]["budget"] == 200 * 40
    assert server["slim"]["estimated_tokens_saved"] > 0
    fix = doc["summary"]["top_fixes"][0]
    assert fix["tokens_saved"] == server["slim"]["estimated_tokens_saved"]
    assert fix["command"][0].startswith("agentwrench slim discover --output tracker.tools.json")
    assert " -- " in fix["command"][0] and fix["command"][0].endswith("fake_server.py static")
    assert fix["command"][1] == "agentwrench slim plan tracker.tools.json --budget 8000"
    overhead = doc["summary"]["clients"]["claude-code"]
    assert overhead["mcp_tokens"] == expected["estimatedTokens"] and overhead["complete"]


def test_probe_text_output(setup, capsys):
    setup.mcp_json({"tracker": fake()})
    code, out, _ = run(capsys, setup.args("--probe", "--no-color"))
    assert code == 0
    assert "1. MCP tool definitions" in out and "tracker" in out and "slim: -" in out
    assert "Largest tools:" in out and "agentwrench slim plan tracker.tools.json --budget 8000" in out


def test_probe_failures_are_reported_and_scan_continues(setup, capsys, tmp_path):
    setup.mcp_json({
        "hangs": fake("hold"),
        "exits": fake("early"),
        "missing": {"command": str(tmp_path / "no-such-binary")},
        "ok": fake(),
    })
    code, doc = run_json(capsys, setup.args("--probe", "--timeout", "1"))
    assert code == 0
    assert by_name(doc, "claude-code", "hangs")["error"] == "timed out after 1s"
    assert "exited" in by_name(doc, "claude-code", "exits")["error"]
    assert by_name(doc, "claude-code", "missing")["error"].startswith("cannot start")
    assert by_name(doc, "claude-code", "ok")["status"] == "measured"
    failed = [f for f in doc["findings"] if "failed to start" in f["message"]]
    assert len(failed) == 3 and all(f["severity"] == "error" for f in failed)
    assert doc["mcp"]["elapsed_ms"] < 8000  # probes run concurrently


def test_remote_servers_are_listed_not_measured(setup, capsys):
    setup.claude_json({"remote": {"type": "sse", "url": "https://example.invalid/sse"}})
    setup.codex_config('[mcp_servers.web]\nurl = "https://example.invalid/mcp"\n')
    code, doc = run_json(capsys, setup.args("--probe"))
    assert code == 0
    assert by_name(doc, "claude-code", "remote")["status"] == "remote"
    assert by_name(doc, "codex", "web")["status"] == "remote"
    assert by_name(doc, "codex", "web")["url"] == "https://example.invalid/mcp"
    assert doc["summary"]["clients"]["codex"]["complete"] is False


def test_same_server_in_both_clients_is_probed_once(setup, capsys):
    setup.claude_json({"tracker": fake()})
    setup.codex_config(f'[mcp_servers.tracker]\ncommand = {json.dumps(PY)}\n'
                       f'args = [{json.dumps(str(FAKE_SERVER))}, "static"]\n')
    code, doc = run_json(capsys, setup.args("--probe"))
    claude, codex = by_name(doc, "claude-code", "tracker"), by_name(doc, "codex", "tracker")
    assert claude["estimated_tokens"] == codex["estimated_tokens"]
    assert codex["duplicate_of"] == "Claude Code global tracker" and codex["slim"] is not None
    slim_fixes = [f for f in doc["findings"] if f["tokens_saved"]]
    assert len(slim_fixes) == 1 and "Claude Code + Codex" in slim_fixes[0]["message"]
    for client in ("claude-code", "codex"):
        assert doc["summary"]["clients"][client]["mcp_tokens"] == claude["estimated_tokens"]


def test_scope_precedence_and_disabled_servers(setup, capsys):
    setup.claude_json({"dup": fake(), "off": fake()},
                      projects={str(setup.project): {"disabledMcpServers": ["off"],
                                                     "disabledMcpjsonServers": ["json-off"]}})
    setup.mcp_json({"dup": fake(), "json-off": fake()})
    setup.codex_config(f'[mcp_servers.idle]\ncommand = {json.dumps(PY)}\nenabled = false\n')
    code, doc = run_json(capsys, setup.args())
    assert by_name(doc, "claude-code", "off")["status"] == "disabled"
    assert by_name(doc, "claude-code", "json-off")["status"] == "disabled"
    assert by_name(doc, "codex", "idle")["status"] == "disabled"
    global_dup = next(s for s in doc["mcp"]["servers"] if s["name"] == "dup" and s["scope"] == "global")
    assert global_dup["status"] == "shadowed" and global_dup["shadowed_by"] == "project"
    assert doc["summary"]["clients"]["claude-code"]["mcp_servers"] == 1


def test_codex_tool_filters_apply_to_measurement(setup, capsys):
    setup.codex_config(f'[mcp_servers.tracker]\ncommand = {json.dumps(PY)}\n'
                       f'args = [{json.dumps(str(FAKE_SERVER))}, "static"]\n'
                       'enabled_tools = ["read_issue", "create_issue", "delete_issue"]\n'
                       'disabled_tools = ["delete_issue"]\n')
    code, doc = run_json(capsys, setup.args("--probe"))
    server = by_name(doc, "codex", "tracker")
    kept = [t for t in TOOLS_40 if t["name"] in ("read_issue", "create_issue")]
    assert server["tools"] == 2 and server["estimated_tokens"] == measure(kept)["estimatedTokens"]


def test_invalid_server_and_unreadable_config_are_findings(setup, capsys):
    setup.claude_json({"bad": {"command": "x", "url": "https://example.invalid"}})
    (setup.project / ".mcp.json").write_text("{not json", encoding="utf-8")
    code, doc = run_json(capsys, setup.args())
    assert code == 0
    assert by_name(doc, "claude-code", "bad")["status"] == "invalid"
    messages = " ".join(f["message"] for f in doc["findings"])
    assert "invalid config" in messages and ".mcp.json" in messages


def test_older_protocol_servers_are_measured_and_stderr_is_discarded(setup, capfd):
    setup.mcp_json({"old": {"command": PY, "args": [str(FIXTURES / "old_protocol_server.py")]}})
    code = __import__("agentwrench.scan.cli", fromlist=["main"]).main(setup.args("--probe", "--json"))
    out, err = capfd.readouterr()
    doc = json.loads(out)
    assert code == 0 and by_name(doc, "claude-code", "old")["tools"] == 1
    assert "STDERR-NOISE-MARKER" not in out + err


def test_slim_discovery_protocol_option_keeps_default_strict():
    command = [PY, str(FIXTURES / "old_protocol_server.py")]
    import subprocess
    with pytest.raises(DiscoveryError, match="protocol 2025-11-25"):
        asyncio.run(run_discover(command, {}, request_timeout_ms=5000, server_stderr=subprocess.DEVNULL))
    snapshot = asyncio.run(run_discover(command, {}, request_timeout_ms=5000, server_stderr=subprocess.DEVNULL,
                                        protocol_versions=("2025-06-18",)))
    assert json.loads(snapshot)["tools"][0]["name"] == "ping_db"


def test_secrets_never_appear_in_output(setup, capsys):
    env = {"API_TOKEN": SECRET, "PLAIN": SECRET}
    setup.claude_json({
        "stdio": {**fake(), "env": env},
        "argsecret": {"command": PY, "args": [str(FAKE_SERVER), "static", "--api-key", SECRET, f"--token={SECRET}",
                                              SECRET, "--password", "hunter2plain", "--auth=plainvalue"]},
        "remote": {"type": "http", "url": f"https://user:{SECRET}@example.invalid/mcp?key={SECRET}",
                   "headers": {"Authorization": f"Bearer {SECRET}", "X-Custom": SECRET}},
    })
    setup.codex_config(f'[mcp_servers.cx]\ncommand = {json.dumps(PY)}\n'
                       f'args = [{json.dumps(str(FAKE_SERVER))}, "static"]\n'
                       f'env = {{ SECRET_VALUE = "{SECRET}" }}\n'
                       f'[mcp_servers.web]\nurl = "https://example.invalid/mcp"\n'
                       f'http_headers = {{ Authorization = "Bearer {SECRET}" }}\n')
    for extra in ((), ("--probe",)):
        for fmt in ((), ("--json",)):
            code, out, err = run(capsys, setup.args(*extra, *fmt, "--no-color"))
            assert code == 0
            assert SECRET not in out and SECRET not in err
            assert SECRET[3:15] not in out
    code, doc = run_json(capsys, setup.args("--probe"))
    assert by_name(doc, "claude-code", "stdio")["env_keys"] == ["API_TOKEN", "PLAIN"]
    assert by_name(doc, "claude-code", "remote")["header_keys"] == ["Authorization", "X-Custom"]
    assert by_name(doc, "claude-code", "remote")["url"] == "https://example.invalid/mcp"
    assert by_name(doc, "claude-code", "argsecret")["command"][-7:] == [
        "--api-key", "[REDACTED]", "[REDACTED]", "[REDACTED]", "--password", "[REDACTED]", "--auth=[REDACTED]"]
    assert "hunter2plain" not in json.dumps(doc) and "plainvalue" not in json.dumps(doc)
    # The probe still receives the real values: the server started and was measured.
    assert by_name(doc, "claude-code", "stdio")["status"] == "measured"
    fix = next(f for f in doc["findings"] if "stdio" in f["message"] or "argsecret" in f["message"])
    assert any('--server-env API_TOKEN="$API_TOKEN"' in c for f in doc["findings"] for c in f["command"])
    assert fix["command"]


def test_claude_env_references_are_expanded_for_probe_only(setup, capsys, monkeypatch):
    monkeypatch.setenv("SCAN_TEST_MODE", "static")
    setup.mcp_json({"t": {"command": PY, "args": [str(FAKE_SERVER), "${SCAN_TEST_MODE}"]}})
    code, doc = run_json(capsys, setup.args("--probe"))
    server = by_name(doc, "claude-code", "t")
    assert server["status"] == "measured" and server["command"][-1] == "${SCAN_TEST_MODE}"


def test_probe_uses_project_as_default_cwd_and_codex_cwd(setup, capsys):
    (setup.project / "srv").mkdir()
    (setup.project / "srv" / "server.py").write_text(FAKE_SERVER.read_text().replace(
        "os.path.dirname(os.path.abspath(__file__))", repr(str(FAKE_SERVER.parent))))
    setup.mcp_json({"rel": {"command": PY, "args": ["srv/server.py", "static"]}})
    setup.codex_config(f'[mcp_servers.rel]\ncommand = {json.dumps(PY)}\nargs = ["server.py", "static"]\ncwd = "srv"\n')
    code, doc = run_json(capsys, setup.args("--probe"))
    assert by_name(doc, "claude-code", "rel")["status"] == "measured"
    assert by_name(doc, "codex", "rel")["status"] == "measured"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only tool")
def test_large_claude_json_is_accepted(setup, capsys):
    padding = {f"/p/{i}": {"history": ["x" * 512] * 4} for i in range(800)}  # > 1 MiB mcpconfig limit
    setup.claude_json({"tracker": fake()}, projects=padding)
    assert (setup.home / ".claude.json").stat().st_size > 1024 * 1024
    code, doc = run_json(capsys, setup.args())
    assert by_name(doc, "claude-code", "tracker")["status"] == "listed"


def test_urls_and_opaque_args_are_redacted(setup, capsys):
    key = "a1B2c3D4e5F6g7H8i9J0k1L2m3"
    setup.claude_json({
        "zap": {"type": "http", "url": f"https://actions.example.invalid/mcp/{key}/sse"},
        "bridge": {"command": "npx", "args": ["-y", "mcp-remote", f"https://u:p@example.invalid/mcp/{key}?x=1", key]},
    })
    for fmt in ((), ("--json",)):
        code, out, _ = run(capsys, setup.args(*fmt, "--no-color"))
        assert key not in out and "u:p@" not in out
    code, doc = run_json(capsys, setup.args())
    assert by_name(doc, "claude-code", "zap")["url"] == "https://actions.example.invalid/mcp/[REDACTED]/sse"
    assert by_name(doc, "claude-code", "bridge")["command"] == ["npx", "-y", "mcp-remote", "[REDACTED]", "[REDACTED]"]
    assert by_name(doc, "claude-code", "bridge")["status"] == "listed"
