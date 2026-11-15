import json
from pathlib import Path

import pytest

from agentwrench.agentsmd.core import (
    MAX_FILE_BYTES, SEPARATOR, Reader, ScopeError, Settings, cut_point,
    find_root, inspect_codex, load_settings,
)
from .conftest import put


def test_order_global_trim_and_separators(tree, isolated_codex_home):
    root, cwd = tree
    put(isolated_codex_home / "AGENTS.md", " \nuser\n ")
    put(root / "AGENTS.md", "root\n")
    put(root / "pkg/AGENTS.md", "pkg")
    put(cwd / "AGENTS.md", "api")
    r = inspect_codex(cwd, isolated_codex_home, Settings())
    assert [Path(e['path']).parent for e in r.entries] == [isolated_codex_home, root, root/'pkg', cwd]
    assert r.text == 'user' + SEPARATOR + 'root\n\n\npkg\n\napi'
    assert r.project_bytes_used == 11
    assert r.entries[-1]['cumulative_bytes'] == 19
    assert r.entries[-1]['project_cumulative_bytes'] == 11


def test_global_is_outside_project_budget(tree, isolated_codex_home):
    root, cwd = tree
    put(isolated_codex_home / 'AGENTS.md', 'global' * 100)
    put(root / 'AGENTS.md', 'abcdef')
    r = inspect_codex(cwd, isolated_codex_home, Settings(4))
    assert r.text == 'global'*100 + SEPARATOR + 'abcd'
    assert r.project_bytes_used == 4
    assert r.truncations[0]['byte_offset'] == 4


def test_global_empty_override_falls_back_but_project_empty_override_shadows(tree, isolated_codex_home):
    root, cwd = tree
    put(isolated_codex_home/'AGENTS.override.md', ' \n')
    put(isolated_codex_home/'AGENTS.md', 'user')
    put(root/'AGENTS.override.md', '\n\t')
    put(root/'AGENTS.md', 'ignored')
    r = inspect_codex(cwd, isolated_codex_home, Settings())
    assert [e['status'] for e in r.entries] == ['skipped-empty', 'loaded', 'skipped-empty', 'shadowed-by-override']
    assert r.text == 'user'
    assert r.project_bytes_used == 0


def test_override_and_fallback_precedence(tree, isolated_codex_home):
    root, cwd = tree
    for name in ['AGENTS.override.md', 'AGENTS.md', 'RULES.md']:
        put(root/name, name)
    put(cwd/'RULES.md', 'fallback')
    r = inspect_codex(cwd, isolated_codex_home, Settings(fallback=['RULES.md']))
    assert r.text == 'AGENTS.override.md\n\nfallback'
    assert [e['status'] for e in r.entries] == ['loaded', 'shadowed-by-override', 'shadowed-by-override', 'loaded']


def test_cumulative_exact_cut_and_fully_dropped_headings(tree, isolated_codex_home):
    root, cwd = tree
    put(root/'AGENTS.md', '12345')
    put(root/'pkg/AGENTS.md', '# Title\nabcdef\n## Gone\nxyz')
    put(cwd/'AGENTS.md', '# API')
    r = inspect_codex(cwd, isolated_codex_home, Settings(16))
    assert [e['bytes_loaded'] for e in r.entries] == [5, 11, 0]
    assert [e['status'] for e in r.entries] == ['loaded', 'partial', 'dropped-over-budget']
    assert r.text == '12345\n\n# Title\nabc'
    point = r.truncations[0]
    assert point['byte_offset'] == 11 and point['line'] == 2
    assert point['nearest_preceding_heading'] == '# Title'
    assert point['headings_fully_dropped'] == [{'byte_offset': 15, 'line': 3, 'heading': '## Gone'}]
    assert r.truncations[1]['byte_offset'] == 0


def test_empty_files_do_not_charge_budget(tree, isolated_codex_home):
    root, cwd = tree
    put(root/'AGENTS.md', ' \n\t'*10)
    put(cwd/'AGENTS.md', 'ok')
    r = inspect_codex(cwd, isolated_codex_home, Settings(32))
    assert r.text == 'ok' and r.project_bytes_used == 2
    assert not r.truncations


def test_whitespace_truncated_prefix_is_uncharged(tree, isolated_codex_home):
    root, cwd = tree
    put(root/'AGENTS.md', '    x')
    put(cwd/'AGENTS.md', 'ok')
    r = inspect_codex(cwd, isolated_codex_home, Settings(2))
    assert r.text == 'ok' and r.project_bytes_used == 2
    assert r.entries[0]['status'] == 'skipped-empty'
    assert r.truncations[0]['byte_offset'] == 2


def test_non_utf8_and_cut_inside_character(tree, isolated_codex_home):
    root, cwd = tree
    put(root/'AGENTS.md', b'A\xff\xe2\x82\xacZ')
    r = inspect_codex(cwd, isolated_codex_home, Settings(4))
    assert r.project_bytes_used == 4
    assert r.text == 'A\ufffd\ufffd'
    assert r.entries[0]['text_bytes'] == 7
    assert r.truncations[0]['splits_utf8_character'] is True
    assert cut_point(root, b'a\xffb', 2)['splits_utf8_character'] is False


def test_rust_whitespace_not_python_control_separators(tree, isolated_codex_home):
    root, cwd = tree
    put(root/'AGENTS.md', '\x1c')
    assert inspect_codex(cwd, isolated_codex_home, Settings()).text == '\x1c'


def test_nearest_root_no_root_and_disabled_root(tree, isolated_codex_home):
    root, cwd = tree
    put(root/'AGENTS.md', 'outer')
    put(cwd/'AGENTS.md', 'cwd')
    (root/'pkg/.git').mkdir()
    assert find_root(cwd, ['.git']) == root/'pkg'
    assert inspect_codex(cwd, isolated_codex_home, Settings()).text == 'cwd'
    (root/'pkg/.git').rmdir()
    assert inspect_codex(cwd, isolated_codex_home, Settings(root_markers=[])).root is None
    outside = root.parent/'outside'
    outside.mkdir()
    put(outside/'AGENTS.md', 'standalone')
    r = inspect_codex(outside, isolated_codex_home, Settings())
    assert r.root is None and r.text == 'standalone'


def test_zero_budget_still_loads_global(tree, isolated_codex_home):
    root, cwd = tree
    put(root/'AGENTS.md', 'project')
    put(isolated_codex_home/'AGENTS.md', 'user')
    r = inspect_codex(cwd, isolated_codex_home, Settings(0))
    assert r.text == 'user' and r.usage == 0
    assert r.entries[-1]['status'] == 'dropped-over-budget'


def test_config_flags_and_sanitization(isolated_codex_home):
    put(isolated_codex_home/'config.toml', '''project_doc_max_bytes = 11
project_doc_fallback_filenames = [" RULES.md ", "RULES.md", "../secret", "", "AGENTS.md"]
project_root_markers = ["workspace.toml"]
''')
    settings, warnings = load_settings(isolated_codex_home)
    assert settings == Settings(11, ['RULES.md'], ['workspace.toml'])
    assert len(warnings) == 1
    assert load_settings(isolated_codex_home, 20, [], [])[0] == Settings(20, [], [])


@pytest.mark.parametrize('config', ['x = [', 'project_doc_max_bytes = -1', 'project_doc_max_bytes = true',
    'project_doc_fallback_filenames = [3]', 'project_root_markers = 5', 'project_root_markers = ["../bad"]'])
def test_invalid_config(isolated_codex_home, config):
    put(isolated_codex_home/'config.toml', config)
    with pytest.raises(ScopeError):
        load_settings(isolated_codex_home)


def test_symlink_files_and_loop(tree, isolated_codex_home):
    root, cwd = tree
    put(root/'shared.md', 'linked')
    (root/'AGENTS.md').symlink_to('shared.md')
    assert inspect_codex(cwd, isolated_codex_home, Settings()).text == 'linked'
    (cwd/'AGENTS.md').symlink_to('AGENTS.md')
    r = inspect_codex(cwd, isolated_codex_home, Settings())
    assert r.diagnostics[0]['error'] == 'OSError'
    assert r.entries[-1]['status'] == 'unreadable'


def test_unreadable_file_reported_and_skipped(tree, isolated_codex_home, monkeypatch):
    root, cwd = tree
    blocked = put(root/'AGENTS.md', 'blocked')
    put(cwd/'AGENTS.md', 'ok')
    real_read = Reader.read
    def denied(self, path):
        if path == blocked:
            raise PermissionError('synthetic permission error')
        return real_read(self, path)
    monkeypatch.setattr(Reader, 'read', denied)
    r = inspect_codex(cwd, isolated_codex_home, Settings())
    assert r.text == 'ok' and r.entries[0]['status'] == 'unreadable'
    assert r.diagnostics == [{'path': str(blocked), 'error': 'PermissionError'}]


def test_nonregular_and_resource_limit(tree, isolated_codex_home):
    import os
    root, cwd = tree
    os.mkfifo(root/'AGENTS.md')
    r = inspect_codex(cwd, isolated_codex_home, Settings())
    assert r.diagnostics and r.text == ''
    (root/'AGENTS.md').unlink()
    with (root/'AGENTS.md').open('wb') as f:
        f.truncate(MAX_FILE_BYTES + 1)
    assert inspect_codex(cwd, isolated_codex_home, Settings()).diagnostics


def test_json_never_includes_content(tree, isolated_codex_home):
    root, cwd = tree
    put(root/'AGENTS.md', 'SYNTHETIC_SECRET')
    assert 'SYNTHETIC_SECRET' not in json.dumps(inspect_codex(cwd, isolated_codex_home, Settings()).to_dict())


def test_shadowed_files_are_not_opened(tree, isolated_codex_home, monkeypatch):
    root, cwd = tree
    for folder in [root, isolated_codex_home]:
        put(folder/'AGENTS.override.md', 'override')
        put(folder/'AGENTS.md', 'hidden')
    real_read = Reader.read
    def guarded(self, path):
        assert path.name != 'AGENTS.md', 'shadowed file must not be opened'
        return real_read(self, path)
    monkeypatch.setattr(Reader, 'read', guarded)
    r = inspect_codex(cwd, isolated_codex_home, Settings())
    assert not r.diagnostics
    assert r.text == 'override' + SEPARATOR + 'override'


def test_heading_line_offsets_use_lf_not_control_characters(tmp_path):
    data = b'# First\nhello\vworld\n```md\n# Hidden\n```not a closer\n# Still hidden\n```\n## Visible\n'
    point = cut_point(tmp_path, data, len(data)-len(b'## Visible\n'))
    assert point['line'] == 8
    assert point['headings_fully_dropped'] == [{'byte_offset': 70, 'line': 8, 'heading': '## Visible'}]


def test_actual_linux_permission_failure(tree, isolated_codex_home):
    import os
    if os.geteuid() == 0:
        pytest.skip('root bypasses Unix file permission bits')
    root, cwd = tree
    blocked = put(root/'AGENTS.md', 'not readable')
    blocked.chmod(0)
    try:
        r = inspect_codex(cwd, isolated_codex_home, Settings())
        assert r.entries[0]['status'] == 'unreadable'
        assert r.diagnostics[0]['error'] == 'PermissionError'
    finally:
        blocked.chmod(0o600)
