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


def test_checkpoints_help_without_langgraph_prints_usage_and_hint():
    for args in (["checkpoints", "--help"], ["checkpoints", "stats", "--help"], ["help", "checkpoints"]):
        result = run(*args, module=("-c", BLOCK_LANGGRAPH))
        assert result.returncode == 0, (args, result.stderr)
        assert result.stdout.startswith("usage: agentwrench checkpoints <subcommand>")
        assert "\n  stats " in result.stdout and "\n  prune " in result.stdout
        assert result.stdout.endswith("install them with: pip install 'agentwrench[langgraph]'\n")


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


SUBCOMMANDS = {
    "scan": [],
    "slim": ["measure", "plan", "discover", "proxy"],
    "cache": ["analyze", "canonicalize", "compare", "sessions"],
    "doctor": ["check", "repair", "preview", "apply", "recover", "verify-backup", "verify-output", "audit",
               "migrate-audit", "verify-audit"],
    "export": ["convert", "summary", "validate", "merge", "validate-merge", "render-merge"],
    "gate": ["normalize", "export-agentevals", "check", "compare", "inspect-sidecar", "restore", "verify-sidecar"],
    "mcp-config": ["convert", "report", "check", "relocate"],
    "cassette": ["record", "replay", "inspect", "validate"],
    "agentsmd": ["explain", "check", "render", "snapshot", "impact", "diff"],
    "patch": ["parse", "apply", "to-unified", "from-unified", "explain", "preview", "apply-plan", "check-plan",
              "check-unified"],
    "checkpoints": ["threads", "stats", "diff", "prune", "snapshot", "plan-status", "budget", "compare-plans"],
}
ALL_HELP = [(name, None) for name in SUBCOMMANDS] + [(name, sub) for name, subs in SUBCOMMANDS.items() for sub in subs]


def _main(argv, capsys):
    try:
        code = cli.main(argv)
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_subcommand_table_covers_every_command():
    assert set(SUBCOMMANDS) == set(cli.BY_NAME)


@pytest.mark.parametrize("flag", ["--help", "-h"])
@pytest.mark.parametrize("command,sub", ALL_HELP, ids=lambda v: v or "-")
def test_every_command_and_subcommand_help_exits_0(command, sub, flag, capsys):
    argv = [command, *([sub] if sub else []), flag]
    code, out, err = _main(argv, capsys)
    assert code == 0, (argv, err)
    assert "usage" in out.lower(), (argv, out)
    if sub:
        assert f"{command} {sub}" in out, (argv, out)


@pytest.mark.parametrize("command", [c for c, subs in SUBCOMMANDS.items() if subs and c not in ("slim", "cache")])
def test_parent_help_describes_every_subcommand(command, capsys):
    code, out, _ = _main([command, "--help"], capsys)
    lines = out.splitlines()
    for sub in SUBCOMMANDS[command]:
        index = next(i for i, line in enumerate(lines) if line.strip().split(" ")[0] == sub
                     and line.startswith("    "))
        rest = lines[index].strip()[len(sub):].strip() or lines[index + 1].strip()
        assert rest, (command, sub)
