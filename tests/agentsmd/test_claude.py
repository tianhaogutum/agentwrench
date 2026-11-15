from pathlib import Path

import pytest

from agentwrench.agentsmd.claude import imports, inspect_claude
from .conftest import put


def test_import_parser_skips_code_comments_email_and_quotes():
    text = '''See @README.md and @Design\\ Docs/api.md
`@inline` ``@double ` code``
```md
@fenced
```
~~~
@tilde
~~~
<!-- @comment -->
user@example.org @"quoted" @'single' \\@escaped
'''
    assert imports(text) == [('README.md', 1), ('Design Docs/api.md', 1)]


def test_multiline_spans_and_longer_fences():
    assert imports('``code\n@hidden``\n@shown\n') == [('shown', 3)]
    assert imports('````\n```\n@hidden\n````\n@shown') == [('shown', 5)]


def test_ancestor_files_and_relative_nested_imports(tree):
    root, cwd = tree
    put(root/'CLAUDE.md', 'See @docs/rules.md')
    put(root/'docs/rules.md', '@more.md')
    put(root/'docs/more.md', 'done')
    put(root/'.claude/CLAUDE.md', 'dot dir')
    put(root/'CLAUDE.local.md', 'local')
    put(cwd/'CLAUDE.md', 'api')
    r = inspect_claude(cwd)
    # Tests cannot assume no unrelated ancestor files exist on the host.
    rows = [e for e in r.entries if Path(e['path']).is_relative_to(root)]
    assert [Path(e['path']).relative_to(root).as_posix() for e in rows] == [
        'CLAUDE.md', 'docs/rules.md', 'docs/more.md', '.claude/CLAUDE.md', 'CLAUDE.local.md', 'pkg/api/CLAUDE.md']
    assert [edge['depth'] for edge in r.edges if Path(edge['source']).is_relative_to(root)] == [1, 2]
    assert r.to_dict()['semantics'] == 'docs-derived'


def test_cycle_missing_duplicate_and_symlink_alias(tree):
    root, cwd = tree
    put(root/'CLAUDE.md', '@one.md @one.md @missing.md @alias.md')
    put(root/'one.md', '@CLAUDE.md')
    (root/'alias.md').symlink_to('one.md')
    r = inspect_claude(cwd)
    edges = [e for e in r.edges if Path(e['source']).is_relative_to(root)]
    assert [e['status'] for e in edges] == ['loaded', 'error', 'already-seen', 'error', 'already-seen']
    assert any(d['error'] == 'import cycle' for d in r.diagnostics)
    assert any(d['error'] == 'FileNotFoundError' for d in r.diagnostics)


def test_four_hop_limit(tree):
    root, cwd = tree
    put(root/'CLAUDE.md', '@1.md')
    for n in range(1, 6):
        put(root/f'{n}.md', f'@{n+1}.md' if n < 5 else 'end')
    r = inspect_claude(cwd)
    assert any(d['error'] == 'import exceeds four-hop depth limit' for d in r.diagnostics)
    assert not any(e['path'] == str(root/'5.md') and e['status'] == 'loaded' for e in r.entries)


def test_import_boundary_canonicalizes_symlinks(tree, tmp_path):
    root, cwd = tree
    external = put(tmp_path/'elsewhere/private.md', 'DO_NOT_READ')
    (root/'linked.md').symlink_to(external)
    put(root/'CLAUDE.md', f'@linked.md @{external}')
    r = inspect_claude(cwd)
    assert len([d for d in r.diagnostics if 'outside allowed roots' in d['error']]) == 2
    assert not any(e['bytes_loaded'] == len('DO_NOT_READ') for e in r.entries)
    allowed = inspect_claude(cwd, [external.parent])
    assert not any('outside allowed roots' in d['error'] for d in allowed.diagnostics)


def test_unsupported_home_and_no_script_execution(tree):
    root, cwd = tree
    put(root/'CLAUDE.md', '@~/secret.md @script.sh')
    put(root/'script.sh', 'touch should-not-exist')
    r = inspect_claude(cwd)
    assert any('unsupported import syntax' in d['error'] for d in r.diagnostics)
    assert not (cwd/'should-not-exist').exists()
