"""Authoritative Git metadata cannot silently replace an earlier declaration."""
import io
import json
from pathlib import Path

import pytest

from agentwrench.patch import PatchError, check_unified, from_unified, plan
from agentwrench.patch.cli import main
import agentwrench.patch.unified as unified


HUNK = '--- a/old\n+++ b/new\n@@ -1 +1 @@\n-old\n+new\n'
UNSAFE = ('diff --git a/old b/new\nrename from ../outside\n'
          'rename from old\nrename to new\n')


def snapshot(root):
    return {str(p.relative_to(root)): (p.stat().st_mode,
            p.read_bytes() if p.is_file() else None) for p in [root, *root.rglob('*')]}


def invoke(route, text, root):
    if route == 'root-free':
        return from_unified(text)
    if route == 'import':
        return from_unified(text, root=root)
    return check_unified(text, root)


@pytest.mark.parametrize('route,pure', [('root-free', False), ('import', False),
                                      ('check', False), ('import', True), ('check', True)])
def test_overwritten_unsafe_rename_rejects(tmp_path, monkeypatch, route, pure):
    (tmp_path / 'old').write_bytes(b'old\n')
    (tmp_path / 'old').chmod(0o751)
    before = snapshot(tmp_path)
    opens = []
    original = Path.open
    def opened(path, *args, **kwargs):
        opens.append(str(path))
        return original(path, *args, **kwargs)
    with monkeypatch.context() as guard:
        guard.setattr(Path, 'open', opened)
        with pytest.raises(PatchError) as exc:
            invoke(route, UNSAFE + ('' if pure else HUNK), tmp_path)
    assert str(exc.value) == 'line 3: duplicate git metadata: rename from'
    assert exc.value.line == 3 and opens == []
    assert snapshot(tmp_path) == before


# Each case tests physical second-declaration location, including a hint between
# declarations. Replacement values never reach decoding/mode validation.
RENAME_PAIRS = [('old', 'old'), ('other', 'old'), ('../outside', 'old'),
                ('old', '../outside'), ('"\\157ld"', 'old'),
                ('"../outside"', '"old"'), ('old', '"\\q"'), ('old', '')]
MODE_PAIRS = [('100644', '100644'), ('100755', '100644'),
              ('100644', '100755'), ('100644', '120000'),
              ('100644', '"100644"'), ('100644', 'junk 100644')]
CASES = []
for key in ('rename from', 'rename to'):
    for first, second in RENAME_PAIRS:
        if key == 'rename to':
            first, second = first.replace('old', 'new'), second.replace('old', 'new')
        other = 'rename to new' if key == 'rename from' else 'rename from old'
        text = f'diff --git a/old b/new\n{key} {first}\nindex ab..cd 100644\n{key} {second}\n{other}\n'
        CASES.append((key, text))
for key in ('new file mode', 'deleted file mode'):
    for first, second in MODE_PAIRS:
        if key == 'new file mode' and first == '100755':
            continue  # Unsupported first values have their existing earlier error.
        CASES.append((key, f'diff --git a/old b/old\n{key} {first}\nindex ab..cd\n{key} {second}\n'))


@pytest.mark.parametrize('key,text', CASES)
@pytest.mark.parametrize('route', ['root-free', 'import', 'check'])
def test_authoritative_keys_are_single_assignment(tmp_path, monkeypatch, key, text, route):
    def forbidden(*args, **kwargs):
        pytest.fail('metadata must reject before root resolution or source access')
    monkeypatch.setattr(Path, 'resolve', forbidden)
    monkeypatch.setattr(Path, 'open', forbidden)
    monkeypatch.setattr(unified, 'safe_path', forbidden)
    with pytest.raises(PatchError) as exc:
        invoke(route, text, tmp_path)
    assert str(exc.value) == f'line 4: duplicate git metadata: {key}'
    assert exc.value.line == 4


@pytest.mark.parametrize('route', ['root-free', 'import', 'check'])
@pytest.mark.parametrize('key', ['rename from', 'rename to', 'new file mode', 'deleted file mode'])
def test_duplicate_in_later_record_precedes_all_source_access(tmp_path, monkeypatch, key, route):
    # This first pure rename would read source (or require --root) if conversion
    # began before all input metadata was validated.
    prefix = 'diff --git a/first b/moved\nrename from first\nrename to moved\n'
    text = next(text for k, text in CASES if k == key)
    def forbidden(*args, **kwargs):
        pytest.fail('later invalid metadata must prevent earlier source access')
    monkeypatch.setattr(Path, 'resolve', forbidden)
    monkeypatch.setattr(Path, 'open', forbidden)
    with pytest.raises(PatchError) as exc:
        invoke(route, prefix + text, tmp_path)
    assert str(exc.value) == f'line 7: duplicate git metadata: {key}'


@pytest.mark.parametrize('text,error,line', [
    ('diff --git a/old b/new\nrename from "\\q"\nrename from old\nrename to new\n',
     'unsupported C path escape', 2),
    ('diff --git a/old b/old\nnew file mode 100755\nnew file mode 100644\n',
     'unsupported file mode (V4A cannot express new executable files)', 2),
    ('diff --git a/old b/old\ndeleted file mode 120000\ndeleted file mode 100644\n',
     'unsupported file mode (V4A cannot express new executable files)', 2),
    ('diff --git a/old b/old\nnew file mode 100644\ndeleted file mode 100644\n'
     '--- /dev/null\n+++ b/old\n', 'mode metadata disagrees with file operation', 1),
    ('diff --git a/old b/new\nrename from ../outside\nrename to new\n',
     "unsafe or unsupported relative path: '../outside'", 3),
])
def test_existing_earlier_errors_retained(tmp_path, text, error, line):
    for route in ('root-free', 'import', 'check'):
        with pytest.raises(PatchError) as exc:
            invoke(route, text, tmp_path)
        assert str(exc.value) == f'line {line}: {error}'
        assert exc.value.line == line


@pytest.mark.parametrize('kind', ['hunk', 'pure', 'empty', 'add', 'delete'])
def test_single_declarations_and_hint_repetition_still_work(tmp_path, kind):
    (tmp_path / 'old').write_bytes(b'' if kind == 'empty' else b'old\n')
    (tmp_path / 'old').chmod(0o644 if kind == 'empty' else 0o751)
    hints = 'index ab..cd 100644\nsimilarity index 100%\n' * 2
    if kind in ('add', 'delete'):
        text = ('diff --git a/old b/old\ndeleted file mode 100755\n' if kind == 'delete'
                else 'diff --git a/added b/added\nnew file mode 100644\n') + hints
    else:
        # Alternate key order, decoded C quoting and repeated informational hints.
        text = ('diff --git a/old b/new\nrename to "new"\n' + hints
                + 'rename from "\\157ld"\n' + (HUNK if kind == 'hunk' else ''))
    before = snapshot(tmp_path)
    converted = from_unified(text, root=tmp_path)
    assert plan(converted, tmp_path).ok
    if kind in ('hunk', 'add', 'delete'):
        assert from_unified(text).to_text() == converted.to_text()
    result = check_unified(text, tmp_path)
    assert result['ok'] and not result['diagnostics']
    assert result['files'][0]['code'] == ('LOCATIONS_MATCHED' if kind == 'hunk' else
                                         'SOURCE_ABSENT' if kind == 'add' else 'SOURCE_PRESENT')
    if kind == 'hunk':
        start = text.splitlines().index('@@ -1 +1 @@') + 1
        assert result['files'][0]['hunks'][0]['patch_span'] == {'start_line': start, 'end_line': start+3}
    assert snapshot(tmp_path) == before


def test_metadata_state_resets_for_each_file(tmp_path):
    text = ''.join(f'diff --git a/{p} b/{p}\nnew file mode 100644\n' for p in ['one', 'two'])
    assert [f.path for f in from_unified(text).files] == ['one', 'two']
    assert check_unified(text, tmp_path)['ok']


@pytest.mark.parametrize('key,headers', [
    ('new file mode', '--- /dev/null\n+++ b/old\n@@ -0,0 +1 @@\n+new\n'),
    ('deleted file mode', '--- a/old\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n'),
])
def test_duplicate_modes_with_hunks_reject_before_headers(tmp_path, key, headers):
    text = f'diff --git a/old b/old\n{key} 100644\n{key} 100644\n' + headers
    for route in ('root-free', 'import', 'check'):
        with pytest.raises(PatchError) as exc:
            invoke(route, text, tmp_path)
        assert str(exc.value) == f'line 3: duplicate git metadata: {key}'


@pytest.mark.parametrize('command', ['from-unified', 'check-unified'])
def test_cli_exact_duplicate_errors(tmp_path, monkeypatch, capsys, command):
    monkeypatch.setattr('sys.stdin', io.TextIOWrapper(io.BytesIO((UNSAFE + HUNK).encode())))
    def forbidden(*args, **kwargs):
        pytest.fail('invalid CLI metadata must not access root')
    monkeypatch.setattr(unified, 'safe_path', forbidden)
    assert main([command, '-', '--root', str(tmp_path)]) == 2
    output = capsys.readouterr()
    if command == 'from-unified':
        assert output.out == ''
        assert output.err == 'invalid patch: line 3: duplicate git metadata: rename from\n'
    else:
        assert output.err == ''
        assert json.loads(output.out) == {'format': 'v4a-unified-check', 'version': 1, 'ok': False,
            'files': [], 'diagnostics': [{'code': 'INVALID_DIFF', 'patch_line': 3}]}
