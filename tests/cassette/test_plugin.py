"""Exercise installed pytest entry point in real isolated pytest subprocesses."""
import json
from pathlib import Path
import sys

from .conftest import cassette, request, response


CLIENT_TEST = '''
import json, subprocess, sys
from pathlib import Path
import pytest

COMMAND = [sys.executable, "-c", 'import json,sys; from pathlib import Path; Path("started").write_text("yes"); [(print(json.dumps({"jsonrpc":"2.0", "id":m["id"], "result":{}}), flush=True)) for m in map(json.loads,sys.stdin)]']

@pytest.mark.mcp_cassette("modes", command=COMMAND)
def test_client(mcp_cassette):
    result = subprocess.run(mcp_cassette.argv, input=b'{"jsonrpc":"2.0","id":77,"method":"ping"}\\n', capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"jsonrpc":"2.0","id":77,"result":{}}
'''


def test_record_modes_once_none_all(pytester):
    pytester.makepyfile(CLIENT_TEST)
    first = pytester.runpytest_subprocess("--mcp-record=once", "-q", "-p", "no:cacheprovider")
    first.assert_outcomes(passed=1)
    target = pytester.path / "cassettes/modes.json"
    initial = target.read_bytes()
    started = pytester.path / "started"
    assert started.exists()
    started.unlink()
    for mode in ("none", "once"):
        replay = pytester.runpytest_subprocess(f"--mcp-record={mode}", "-q", "-p", "no:cacheprovider")
        replay.assert_outcomes(passed=1)
        assert not started.exists()  # real server was never executed
        assert target.read_bytes() == initial
    rerecord = pytester.runpytest_subprocess("--mcp-record=all", "-q", "-p", "no:cacheprovider")
    rerecord.assert_outcomes(passed=1)
    assert started.exists()


def test_none_missing_never_starts_server(pytester):
    pytester.makepyfile(CLIENT_TEST)
    result = pytester.runpytest_subprocess("-q", "-p", "no:cacheprovider")
    result.assert_outcomes(errors=1)
    assert not (pytester.path / "started").exists()
    result.stdout.fnmatch_lines(["*missing cassette*--mcp-record=once*"])


def test_teardown_detects_unused_even_when_client_ignores_exit(pytester):
    directory = pytester.path / "cassettes"
    directory.mkdir()
    (directory / "strict.json").write_text(json.dumps(cassette(
        ("client", request("a")), ("server", response()), ("client", request("b")), ("server", response()))))
    pytester.makepyfile('''
import subprocess
import pytest
@pytest.mark.mcp_cassette("strict")
def test_client(mcp_cassette):
    subprocess.run(mcp_cassette.argv, input=b'{"jsonrpc":"2.0","id":5,"method":"a"}\\n', capture_output=True, timeout=10)
''')
    result = pytester.runpytest_subprocess("-q", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*MCP cassette process failed*'unused': 1*"])


def test_paths_cannot_escape_and_no_command_is_executed(pytester):
    pytester.makepyfile(CLIENT_TEST.replace('"modes", command=COMMAND', '"../escape", command=COMMAND'))
    result = pytester.runpytest_subprocess("--mcp-record=all", "-q", "-p", "no:cacheprovider")
    result.assert_outcomes(errors=1)
    assert not (pytester.path / "started").exists()
    assert not (pytester.path / "escape.json").exists()


def test_symlinked_directory_rejected(pytester):
    outside = pytester.path / "elsewhere"
    outside.mkdir()
    (pytester.path / "cassettes").symlink_to(outside, target_is_directory=True)
    pytester.makepyfile(CLIENT_TEST)
    result = pytester.runpytest_subprocess("--mcp-record=all", "-q", "-p", "no:cacheprovider")
    result.assert_outcomes(errors=1)
    assert list(outside.iterdir()) == []


def test_missing_report_fails_teardown(pytester):
    pytester.makepyfile(CLIENT_TEST.replace('result = subprocess.run', 'return\n    result = subprocess.run'))
    result = pytester.runpytest_subprocess("--mcp-record=once", "-q", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*MCP process did not finalize*"])


def test_shell_command_string_rejected(pytester):
    pytester.makepyfile(CLIENT_TEST.replace('command=COMMAND', 'command="echo not-a-shell"'))
    result = pytester.runpytest_subprocess("--mcp-record=once", "-q", "-p", "no:cacheprovider")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*shell strings are not accepted*"])
