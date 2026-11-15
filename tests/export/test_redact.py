import json
import pytest
from agentwrench.export import load_session
from agentwrench.export.export import prepare, markdown, jsonl, to_atif, handoff
from agentwrench.export.redact import Redactor
from .conftest import FIXTURES


@pytest.mark.parametrize('secret', [
    'sk-synthetic123456789', 'ghp_synthetic123456', 'github_pat_synthetic123456',
    'AKIA1234567890ABCDEF', 'Bearer ey.synthetic-token',
    'API_KEY=secretvalue', 'MY_TOKEN="value with spaces"', "DB_SECRET='my secret'",
    'PASSWORD=somevalue', 'DB_PASSWORD="synthetic password"',
    'alice@example.test', '/home/alice/private', '/Users/bob/private',
])
def test_positive_redaction(secret):
    r = Redactor()
    output = r.walk({'value': secret})['value']
    assert output != secret
    assert sum(r.counts.values()) >= 1
    assert output == Redactor().walk({'value': secret})['value']


@pytest.mark.parametrize('text', ['monkey=banana', 'KEYBOARD=us', 'sk-short', 'ghp_short',
                                  'user@localhost', 'MY_TOKEN_COUNT=3', 'public_key_algorithm=RSA',
                                  '/homework/alice/demo', 'tokenize(text)', 'the password is unknown'])
def test_false_positive_guards(text):
    r = Redactor()
    assert r.text(text) == text
    assert not r.counts


@pytest.mark.parametrize('key', ['API_KEY', 'API_TOKEN', 'CLIENT_SECRET', 'PASSWORD', 'DB_PASSWORD', 'access-token', 'secret', 'apiKey'])
def test_nested_sensitive_fields(key):
    r = Redactor()
    output = r.walk({'outer': [{key: 'sensitive value'}]})
    assert 'sensitive value' not in json.dumps(output)
    assert r.counts == {'field': 1}


@pytest.mark.parametrize('render', [markdown, jsonl, lambda d: json.dumps(to_atif(d)), handoff])
def test_no_secret_in_any_export(render):
    doc = prepare(load_session(FIXTURES / 'codex/rollout-secrets.jsonl'))
    output = render(doc)
    for secret in ['sk-', 'ghp_', 'github_pat_', 'AKIA1234567890ABCDEF', 'synthetic-value',
                   'synthetic password', 'synthetic-json-secret', 'synthetic-tool-token',
                   'synthetic-output-secret', 'synthetic-metadata-secret', 'alice@example.test',
                   '/home/alice', '/Users/alice', 'synthetic.jwt.token']:
        assert secret not in output
    assert doc['extra']['redaction']['total'] >= 10


def test_strict_mode_extra_patterns():
    text = 'https://alice:secret@example.test/path?access_token=hidden /work/project/file.py eyJabcdef.abcdef.abcdef -----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----'
    r = Redactor('strict')
    output = r.text(text)
    assert 'alice:secret' not in output and 'hidden' not in output and 'AAAA' not in output
    assert '/work/project/file.py' not in output
    assert 'eyJabcdef' not in output


def test_off_preserves_values():
    value = {'API_KEY': 'secret', 'message': 'sk-synthetic123456789 /home/alice'}
    r = Redactor('off')
    assert r.walk(value) == value
    assert not r.counts


def test_counts_are_occurrences_and_same_secret_same_marker():
    r = Redactor()
    text = r.text('sk-synthetic123456789 sk-synthetic123456789')
    a, b = text.split()
    assert a == b
    assert r.counts == {'api-key': 2}
