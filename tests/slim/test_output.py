"""Port of test/output.test.js (injected filesystem failures)."""

import errno
import os

import pytest

from agentwrench.slim.output import CatalogueOutputError, FileSystem, write_catalogue_file


def private_failure(detail):
    return OSError(errno.EIO, detail)


@pytest.mark.parametrize('phase', ['open', 'write', 'close', 'rename'])
def test_catalogue_publication_preserves_the_destination_through_partial_write_close_and_rename_errors(tmp_dir, phase):
    destination = str(tmp_dir / 'catalogue.json')
    snapshot = '{"tools":[]}\n'
    with open(destination, 'w') as handle:
        handle.write('sentinel')
    state = {'closed': False}

    def read(path):
        with open(path, encoding='utf-8') as handle:
            return handle.read()

    class Handle:
        def __init__(self, fd):
            self.fd = fd

        def write_file(self, data):
            assert read(destination) == 'sentinel'
            os.write(self.fd, (data[:4] if phase == 'write' else data).encode())
            if phase == 'write':
                raise private_failure('PRIVATE filesystem detail')

        def close(self):
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            state['closed'] = True
            if phase == 'close':
                raise private_failure('PRIVATE filesystem detail')

    class Io(FileSystem):
        def open(self, path, flags, mode):
            if phase == 'open':
                raise private_failure('PRIVATE filesystem detail')
            assert flags == 'wx'
            assert mode == 0o600
            return Handle(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode))

        def rename(self, source, target):
            assert phase == 'rename', 'failed writes/closes must not reach rename'
            assert state['closed'] is True
            assert read(source) == snapshot
            assert read(destination) == 'sentinel'
            raise private_failure('PRIVATE filesystem detail')

    with pytest.raises(CatalogueOutputError) as caught:
        write_catalogue_file(destination, snapshot, Io())
    assert str(caught.value) == 'discovery output failed (EIO)'
    assert state['closed'] == (phase != 'open')
    assert read(destination) == 'sentinel'
    assert os.listdir(tmp_dir) == ['catalogue.json']


def test_catalogue_temporary_file_shares_the_filesystem_resolved_parent_and_is_cleaned_on_rename_failure(tmp_dir):
    os.makedirs(tmp_dir / 'actual' / 'child')
    os.symlink(tmp_dir / 'actual' / 'child', tmp_dir / 'link', target_is_directory=True)
    (tmp_dir / 'catalogue.json').write_text('lexical sentinel')
    (tmp_dir / 'actual' / 'catalogue.json').write_text('destination sentinel')
    # Also exercise a relative destination; join/resolve here would erase /../.
    destination = f'{os.path.relpath(tmp_dir, os.getcwd())}/link/../catalogue.json'
    snapshot = '{"tools":[]}\n'
    renamed = []

    class Io(FileSystem):
        def rename(self, source, target):
            renamed.append(True)
            assert os.path.dirname(source) == os.path.realpath(tmp_dir / 'actual')
            assert target == destination
            with open(source) as handle:
                assert handle.read() == snapshot
            with open(target) as handle:
                assert handle.read() == 'destination sentinel'
            raise private_failure('PRIVATE rename failure')

    with pytest.raises(CatalogueOutputError, match=r'^discovery output failed \(EIO\)$'):
        write_catalogue_file(destination, snapshot, Io())
    assert renamed == [True]
    assert (tmp_dir / 'catalogue.json').read_text() == 'lexical sentinel'
    with open(destination) as handle:
        assert handle.read() == 'destination sentinel'
    assert sorted(os.listdir(tmp_dir)) == ['actual', 'catalogue.json', 'link']
    assert sorted(os.listdir(tmp_dir / 'actual')) == ['catalogue.json', 'child']
    assert os.listdir(tmp_dir / 'actual' / 'child') == []
