import json
from pathlib import Path

import pytest

from agentwrench.patch import PatchError, parse

FIXTURES = Path(__file__).parent / 'fixtures'


def envelope(body):
    return '*** Begin Patch\n' + body + '\n*** End Patch\n'


def test_parser_fixture_golden():
    p = parse((FIXTURES / 'simple.patch').read_text())
    assert [(f.kind, f.path, f.move_to) for f in p.files] == [
        ('update', 'hello.txt', None), ('add', 'nested/new.txt', None),
        ('delete', 'gone.txt', None), ('update', 'old-name.txt', 'new-name.txt')]
    assert p.files[0].hunks[0].old == ['Hello, world!', 'old value']
    assert p.files[0].hunks[0].new == ['Hello, world!', 'new value']
    assert parse(p.to_text()).to_dict() == p.to_dict()
    assert json.loads(json.dumps(p.to_dict()))['files'][0]['hunks'][0]['lines'][1] == ['-', 'old value']


@pytest.mark.parametrize('wrapper', ['<<EOF', "<<'EOF'", '<<"EOF"'])
def test_heredoc_wrappers(wrapper):
    assert parse(wrapper + '\n' + envelope('*** Add File: x\n+hello') + 'EOF\n').files[0].contents == ['hello']


def test_implicit_first_header_bare_blank_and_indented_marker():
    p = parse(envelope('*** Update File: x\n a\n\n *** Update File: literal\n+b'))
    assert p.files[0].hunks[0].old == ['a', '', '*** Update File: literal']
    assert len(p.files) == 1


def test_context_markers_and_eof():
    p = parse(envelope('*** Update File: x\n@@ function\n-old\n+new\n*** End of File\n\n@@ next\n a'))
    assert p.files[0].hunks[0].anchor == 'function'
    assert p.files[0].hunks[0].eof
    assert p.files[0].hunks[1].anchor == 'next'


@pytest.mark.parametrize('body', [
    'bad', '*** Update File: x', '*** Update File: x\n@@',
    '*** Update File: x\n@@\n@@\n+x', '*** Add File: x\nno plus',
    '*** Delete File: x\n+content', '*** Update File: x\n*** End of File',
    '*** Update File: x\n@@\n+x\n*** End of File\n+y',
    '*** Environment ID: remote', '*** Add File: x\n+\x00',
])
def test_malformed_with_line_number(body):
    with pytest.raises(PatchError, match='line [0-9]+:'):
        parse(envelope(body))


@pytest.mark.parametrize('text', ['', 'no envelope', '*** Begin Patch\n',
                                   "<<'EOF'\n*** Begin Patch\n*** End Patch"])
def test_bad_boundaries(text):
    with pytest.raises(PatchError):
        parse(text)


@pytest.mark.parametrize('path', ['../x', 'a/../../x', '/tmp/x', 'a//x', './x', '.git/config',
                                 'C:/x', 'a\\b', 'x\tname', 'x/..', ''])
def test_unsafe_paths(path):
    with pytest.raises(PatchError):
        parse(envelope('*** Add File: ' + path + '\n+content'))


def test_crlf_patch_and_empty_add():
    assert parse(envelope('*** Add File: x').replace('\n', '\r\n')).files[0].contents == []
    assert parse('  *** Begin Patch  \n*** End Patch   ').files == []
