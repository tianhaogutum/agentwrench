"""Top-level `agentwrench` dispatcher: overview, version, routing and error paths."""
from importlib.metadata import entry_points
import subprocess
import sys

import pytest

import agentwrench
from agentwrench import cli

TOOLS = ["doctor", "checkpoints", "agentsmd", "mcp-config", "export", "cassette", "gate", "patch"]
BLOCK_LANGGRAPH = """
import sys
class Block:
    def find_spec(self, name, path=None, target=None):
        if name == "langgraph" or name.startswith("langgraph."):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
sys.meta_path.insert(0, Block())
from agentwrench.cli import main
raise SystemExit(main(sys.argv[1:]))
"""


def run(*args, module=("-m", "agentwrench")):
    return subprocess.run([sys.executable, *module, *args], capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("args", [[], ["--help"], ["-h"], ["help"]])
def test_overview_lists_every_group_and_command(args, capsys):
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"agentwrench {agentwrench.__version__} ")
    for group in cli.GROUPS:
        assert f"\n{group}:\n" in out
    for name in [*TOOLS, "scan", "slim", "cache"]:
        assert f"\n  {name} " in out


def test_command_table_shape():
    assert [c.name for c in cli.COMMANDS if c.group == "Tokens & cost"] == ["scan", "slim", "cache"]
    assert {c.name: c.module for c in cli.COMMANDS if c.name in ("scan", "slim", "cache")} == {
        "scan": "agentwrench.scan.cli", "slim": "agentwrench.slim.cli", "cache": "agentwrench.cache.cli"}
    assert set(TOOLS) <= set(cli.BY_NAME)
    assert all(c.group in cli.GROUPS and c.summary for c in cli.COMMANDS)


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version(flag, capsys):
    assert cli.main([flag]) == 0
    assert capsys.readouterr().out == f"agentwrench {agentwrench.__version__}\n"


@pytest.mark.parametrize("tool", TOOLS)
def test_tool_help_uses_new_program_name(tool):
    result = run(tool, "--help")
    assert result.returncode == 0, result.stderr
    assert f"usage: agentwrench {tool}" in result.stdout


@pytest.mark.parametrize("tool", TOOLS)
def test_help_subcommand_routes_to_tool_help(tool, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["help", tool])
    assert exc.value.code == 0
    assert f"usage: agentwrench {tool}" in capsys.readouterr().out


def test_tool_exit_codes_pass_through(tmp_path):
    clean = tmp_path / "clean.jsonl"
    clean.write_text('{"type":"session_meta","payload":{"id":"x"}}\n')
    assert run("doctor", "check", clean).returncode == 0
    bad = run("gate", "no-such-subcommand")
    assert bad.returncode == 2 and "usage: agentwrench gate" in bad.stderr


def test_unknown_command_suggests_close_match(capsys):
    assert cli.main(["gat"]) == 2
    err = capsys.readouterr().err
    assert "unknown command 'gat'" in err and "did you mean 'gate'?" in err


def test_unknown_legacy_name_points_to_new_command(capsys):
    assert cli.main(["trajport"]) == 2
    assert "trajport is now 'agentwrench export'" in capsys.readouterr().err


def test_unknown_command_without_match(capsys):
    assert cli.main(["zzzzzz"]) == 2
    err = capsys.readouterr().err
    assert "unknown command 'zzzzzz'" in err and "did you mean" not in err


def test_unknown_option(capsys):
    assert cli.main(["--frobnicate"]) == 2
    assert "unknown option '--frobnicate'" in capsys.readouterr().err


def test_missing_module_reports_not_available(monkeypatch, capsys):
    ghost = cli.Command("ghost", "agentwrench.ghost_tool.cli", "not shipped", "Other")
    monkeypatch.setitem(cli.BY_NAME, "ghost", ghost)
    monkeypatch.setattr(cli, "COMMANDS", (*cli.COMMANDS, ghost))
    assert "ghost" in cli.overview() and "(coming soon)" in cli.overview()
    assert cli.main(["ghost", "--help"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("agentwrench: command 'ghost' is not available") and "Traceback" not in err


def test_checkpoints_without_langgraph_prints_install_hint():
    result = run("checkpoints", "threads", "x.db", module=("-c", BLOCK_LANGGRAPH))
    assert result.returncode == 2 and result.stdout == ""
    assert result.stderr == ("agentwrench checkpoints: missing optional dependencies; "
                             "install them with: pip install 'agentwrench[langgraph]'\n")
    overview = run("--help", module=("-c", BLOCK_LANGGRAPH))
    assert "(needs agentwrench[langgraph])" in overview.stdout


@pytest.mark.parametrize("module", ["agentwrench.doctor", "agentwrench.mcpconfig", "agentwrench.patch"])
def test_subpackage_main_routes_through_dispatcher(module):
    result = run("--help", module=("-m", module))
    assert result.returncode == 0 and "usage: agentwrench " in result.stdout


def test_cassette_pytest_plugin_entry_point():
    plugins = {ep.name: ep.value for ep in entry_points(group="pytest11")}
    assert plugins.get("agentwrench_cassette") == "agentwrench.cassette.pytest_plugin"
