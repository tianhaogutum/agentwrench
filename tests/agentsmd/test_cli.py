import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import put

FIXTURES = Path(__file__).parent/'fixtures'


def cli(*args, cwd=None):
    return subprocess.run([sys.executable, '-m', 'agentwrench', 'agentsmd', *map(str, args)],
                          capture_output=True, cwd=cwd, timeout=10)


def fixture_args():
    return ['--cwd', FIXTURES/'big/pkg/api', '--codex-home', FIXTURES/'codex_home']


def test_e2e_fixture_exact_sizes_and_cut():
    result = cli('explain', *fixture_args(), '--json')
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert [e['size'] for e in report['entries']] == [1204, 23040, 12000, 900]
    assert [e['bytes_loaded'] for e in report['entries']] == [1204, 23040, 9728, 0]
    assert report['project_bytes_used'] == 32768
    assert report['truncations'][0]['byte_offset'] == 9728
    assert report['truncations'][0]['nearest_preceding_heading'] == '## Testing'
    assert report['truncations'][0]['headings_fully_dropped'][0]['heading'] == '## Final checks'
    assert cli('check', *fixture_args()).returncode == 1
    assert cli('check', *fixture_args(), '--max-bytes', 65536, '--max-usage', 1.0).returncode == 0


def test_render_is_byte_exact_without_added_newline(tree, isolated_codex_home):
    root, cwd = tree
    put(root/'AGENTS.md', b'a\r\nb\xff')
    result = cli('render', '--cwd', cwd)
    assert result.returncode == 0
    assert result.stdout == b'a\r\nb\xef\xbf\xbd'


def test_cli_reads_environment_home_and_threshold(tree, isolated_codex_home):
    root, cwd = tree
    put(root/'AGENTS.md', '123456789')
    put(isolated_codex_home/'config.toml', 'project_doc_max_bytes = 10')
    result = cli('check', '--cwd', cwd)
    assert result.returncode == 1 and b'exceeds' in result.stdout
    assert cli('check', '--cwd', cwd, '--max-usage', '.9').returncode == 0
    assert cli('check', '--cwd', cwd, '--max-bytes', '20').returncode == 0


@pytest.mark.parametrize('args', [ ['--max-usage', 'nan'], ['--max-usage', 'inf'],
    ['--max-usage', '-.1'], ['--max-usage', '1.1'], ['--max-bytes', '-1'],
    ['--no-root-markers', '--root-marker', '.git'] ])
def test_invalid_options_exit_two(tree, args):
    _, cwd = tree
    assert cli('check', '--cwd', cwd, *args).returncode == 2


def test_malformed_config_does_not_echo_secrets(tree, isolated_codex_home):
    _, cwd = tree
    put(isolated_codex_home/'config.toml', 'secret_key = "TOP_SECRET" broken syntax')
    result = cli('explain', '--cwd', cwd, '--json')
    assert result.returncode == 2
    assert b'TOP_SECRET' not in result.stderr
    assert b'malformed config.toml' in result.stderr


def test_missing_cwd_exit_two(tmp_path):
    assert cli('explain', '--cwd', tmp_path/'absent').returncode == 2


def test_io_failure_fail_closed(tree):
    root, cwd = tree
    (root/'AGENTS.md').symlink_to('AGENTS.md')
    result = cli('explain', '--cwd', cwd, '--json')
    assert result.returncode == 1 and json.loads(result.stdout)['diagnostics']
    assert cli('check', '--cwd', cwd).returncode == 1
    result = cli('render', '--cwd', cwd)
    assert result.returncode == 2 and not result.stdout


def test_claude_cli_and_unsupported_render(tree):
    root, cwd = tree
    put(root/'CLAUDE.md', '@missing.md')
    result = cli('explain', '--cwd', cwd, '--agent', 'claude', '--json')
    assert result.returncode == 1
    assert json.loads(result.stdout)['edges'][-1]['error'] == 'FileNotFoundError'
    assert cli('check', '--cwd', cwd, '--agent', 'claude').returncode == 1
    assert cli('render', '--cwd', cwd, '--agent', 'claude').returncode == 2
    assert cli('explain', '--cwd', cwd, '--agent', 'claude', '--max-bytes', 5).returncode == 2


def test_cli_explicit_fallback_replaces_config(tree, isolated_codex_home):
    root, cwd = tree
    put(root/'RULES.md', 'rules')
    put(isolated_codex_home/'config.toml', 'project_doc_fallback_filenames = ["RULES.md"]')
    assert cli('render', '--cwd', cwd).stdout == b'rules'
    assert cli('render', '--cwd', cwd, '--fallback').stdout == b''


def test_budget_comparison_experiment():
    result = subprocess.run([sys.executable, str(Path(__file__).with_name('compare_budgets.py'))], capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    small, large = json.loads(result.stdout)
    assert small['project_source_bytes'] == large['project_source_bytes'] == 35940
    assert small['project_dropped_bytes'] == 3172 and small['dropped_files'] == 1
    assert small['partial_files'] == 1 and small['fully_dropped_headings'] == 3
    assert large['project_dropped_bytes'] == 0 and large['fully_dropped_headings'] == 0
