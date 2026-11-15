"""JSONL framing uses LF bytes, never Unicode's broader line boundaries."""
import json

import pytest

from agentwrench.export import adapters
from agentwrench.export.model import InputError


TEXT = 'alpha\u2028beta\u0085gamma\u2029delta'
NON_LF_BOUNDARIES = ['\r', '\v', '\f', '\x1c', '\x1d', '\x1e', '\u0085', '\u2028', '\u2029']


@pytest.mark.parametrize('ending', ['\n', '\r\n', ''])
def test_literal_and_escaped_unicode_records_are_equivalent(tmp_path, ending):
    rows = [{TEXT: {'message': TEXT, 'escaped_controls': '\n\r\v\f'}}, {'message': TEXT}]
    path = tmp_path / 'unicode.jsonl'
    for escaped in (False, True):
        path.write_bytes(('\n'.join(json.dumps(row, ensure_ascii=escaped) for row in rows)
                          + ending).encode('utf-8'))
        assert adapters.read_jsonl(path) == (rows, False)


@pytest.mark.parametrize('separator', NON_LF_BOUNDARIES)
def test_only_lf_separates_records(tmp_path, separator):
    path = tmp_path / 'invalid.jsonl'
    path.write_bytes(('{}' + separator + '{}\n').encode('utf-8'))
    with pytest.raises(InputError, match='invalid JSON at line 1'):
        adapters.read_jsonl(path)


@pytest.mark.parametrize('separator', NON_LF_BOUNDARIES[1:])
def test_non_json_whitespace_lines_are_not_silently_ignored(tmp_path, separator):
    path = tmp_path / 'invalid.jsonl'
    path.write_bytes(('{}\n' + separator + '\n').encode('utf-8'))
    with pytest.raises(InputError, match='invalid JSON at line 2'):
        adapters.read_jsonl(path)


def test_error_line_numbers_count_only_lf(tmp_path):
    path = tmp_path / 'invalid.jsonl'
    path.write_bytes((json.dumps({'message': TEXT}, ensure_ascii=False)
                      + '\r\n \t\r\n{broken}\n').encode('utf-8'))
    with pytest.raises(InputError, match='invalid JSON at line 3'):
        adapters.read_jsonl(path)


@pytest.mark.parametrize('ending', ['\n', '\r\n', '', '\r'])
def test_truncation_requires_a_final_record_without_lf(tmp_path, ending):
    path = tmp_path / 'truncated.jsonl'
    path.write_bytes(('{}\n{"message":"' + TEXT + ending).encode('utf-8'))
    if ending.endswith('\n'):
        with pytest.raises(InputError, match='invalid JSON at line 2'):
            adapters.read_jsonl(path)
    else:
        assert adapters.read_jsonl(path) == ([{}], True)


@pytest.mark.parametrize('ending', ['\n', '\r\n', ''])
def test_limits_apply_to_physical_records_and_utf8_bytes(tmp_path, monkeypatch, ending):
    path = tmp_path / 'bounded.jsonl'
    data = (json.dumps({'message': TEXT}, ensure_ascii=False) + ending).encode('utf-8')
    path.write_bytes(data)
    monkeypatch.setattr(adapters, 'MAX_RECORDS', 1)
    monkeypatch.setattr(adapters, 'MAX_LINE_BYTES', len(data))
    assert adapters.read_jsonl(path) == ([{'message': TEXT}], False)
    monkeypatch.setattr(adapters, 'MAX_LINE_BYTES', len(data) - 1)
    with pytest.raises(InputError, match='JSONL line 1 exceeds'):
        adapters.read_jsonl(path)
