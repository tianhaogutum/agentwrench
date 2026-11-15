"""Port of test/cli.test.js."""

import json
import re

import pytest

from agentwrench.slim.config import InputError
from agentwrench.slim.jsonio import read_json

from .conftest import run


def test_measure_cli_json_report_plan_to_config_to_measure_acceptance_pipeline(tmp_dir):
    before = run('measure', 'fixtures/tools-40.json', '--json')
    assert before.returncode == 0, before.stderr
    assert json.loads(before.stdout)['tools'] == 40
    p = run('plan', 'fixtures/tools-40.json', '--budget', '6000')
    assert p.returncode == 0, p.stderr
    infeasible = run('plan', 'fixtures/tools-40.json', '--budget', '4000')
    assert infeasible.returncode == 0
    assert re.search('Budget NOT met', infeasible.stderr)
    assert re.search('Deny suggestion', infeasible.stderr)
    assert 'deny' not in json.loads(infeasible.stdout)
    config = json.loads(p.stdout)
    assert 'deny' not in config
    (tmp_dir / 'slim.json').write_text(p.stdout)
    after = run('measure', 'fixtures/tools-40.json', '--config', str(tmp_dir / 'slim.json'), '--json')
    assert after.returncode == 0, after.stderr
    result = json.loads(after.stdout)
    assert result['tools'] == 40
    assert result['estimatedTokens'] <= 6000
    assert result['savedBytes'] > 0
    assert result['before']['bytes'] == json.loads(before.stdout)['bytes']
    text = run('measure', 'fixtures/tools-40.json', '--config', str(tmp_dir / 'slim.json'))
    assert text.returncode == 0
    assert re.search('before.*tools: 40', text.stdout)
    assert re.search('saved:', text.stdout)


def test_invalid_config_exits_2_with_key_before_attempting_to_spawn_server(tmp_dir):
    (tmp_dir / 'bad.json').write_text('{"allow":"*"}')
    result = run('proxy', '--config', str(tmp_dir / 'bad.json'), '--', '/does/not/exist')
    assert result.returncode == 2
    assert re.search(r'config\.allow', result.stderr)
    assert not re.search('cannot start server', result.stderr)


def test_bad_cli_paths_json_options_and_budget_fail_with_exit_2(tmp_dir):
    (tmp_dir / 'bad.json').write_text('{secret bad json')
    (tmp_dir / 'bad-utf8.json').write_bytes(b'\xff')
    (tmp_dir / 'array.json').write_text('[{"name":"test","inputSchema":{"type":"object"}}]')
    assert run('measure', str(tmp_dir / 'array.json')).returncode == 0
    for args in [[], ['measure'], ['measure', 'missing.json'], ['measure', str(tmp_dir)], ['measure', str(tmp_dir / 'bad.json')],
                 ['measure', str(tmp_dir / 'bad-utf8.json')], ['measure', 'fixtures/tools-40.json', '--wat'],
                 ['measure', 'fixtures/tools-40.json', '--config'], ['plan', 'fixtures/tools-40.json', '--budget', '0'],
                 ['plan', 'fixtures/tools-40.json', '--budget', '1.5'], ['proxy', '--config', 'fixtures/slim.json'],
                 ['plan', 'fixtures/tools-40.json', '--budget', '5', '--budget', '6']]:
        result = run(*args)
        assert result.returncode == 2, f'{args}: {result.stderr}'
        assert result.stdout == ''
        assert not re.search('secret bad', result.stderr)
    assert run('--help').returncode == 0
    assert run('--version').stdout.strip() == '0.1.0'


def test_file_size_limits_prevent_large_input_allocation(tmp_dir):
    (tmp_dir / 'large.json').write_bytes(b' ' * (16 * 1024 * 1024 + 1))
    with pytest.raises(InputError, match='16 MiB'):
        read_json(str(tmp_dir / 'large.json'))


def test_unsafe_numeric_and_deeply_nested_json_fail_explicitly_before_transformation(tmp_dir):
    (tmp_dir / 'unsafe.json').write_text('[{"name":"tool","inputSchema":{"minimum":9007199254740993}}]')
    (tmp_dir / 'deep.json').write_text('[' * 140 + '0' + ']' * 140)
    unsafe = run('measure', str(tmp_dir / 'unsafe.json'))
    assert unsafe.returncode == 2
    assert re.search('safe-integer', unsafe.stderr)
    deep = run('measure', str(tmp_dir / 'deep.json'))
    assert deep.returncode == 2
    assert re.search('nesting exceeds 128', deep.stderr)


def test_request_timeout_cli_rejects_invalid_values_before_spawning_and_excludes_offline_commands():
    for value in ['0', '-1', '1.5', 'NaN', 'Infinity', '1e3', '2147483648', '9007199254740993', '']:
        result = run('proxy', '--config', 'fixtures/slim.json', '--request-timeout-ms', value, '--', '/does/not/exist')
        assert result.returncode == 2, value
        assert re.search('--request-timeout-ms', result.stderr)
        assert not re.search('cannot start server', result.stderr)
        assert result.stdout == ''
    assert run('proxy', '--config', 'fixtures/slim.json', '--request-timeout-ms', '100', '--request-timeout-ms', '200', '--', '/does/not/exist').returncode == 2
    assert run('measure', 'fixtures/tools-40.json', '--request-timeout-ms', '100').returncode == 2
    for value in ['1', '2147483647']:
        result = run('proxy', '--config', 'fixtures/slim.json', '--request-timeout-ms', value, '--', '/does/not/exist')
        assert result.returncode == 1
        assert re.search('cannot start server', result.stderr)
    assert re.search('--request-timeout-ms', run('--help').stdout)


def test_plan_warns_on_stderr_when_descriptions_are_gutted_or_budget_is_unmet():
    unmet = run('plan', 'fixtures/tools-40.json', '--budget', '4000')
    assert unmet.returncode == 0
    assert json.loads(unmet.stdout)['maxDescriptionChars'] == 0
    assert 'Warning: description cap of 0 code points is below 40' in unmet.stderr
    assert 'Warning: budget 4000 is not met even by this config (~4618 tokens).' in unmet.stderr
    assert ('Warning: consider strip-only (remove maxDescriptionChars from the config: ~9340 tokens) '
            'or a higher budget (--budget 4885 keeps up to 40 characters).') in unmet.stderr
    tight = run('plan', 'fixtures/tools-40.json', '--budget', '4650')
    assert json.loads(tight.stdout)['maxDescriptionChars'] == 3
    assert 'Warning: description cap of 3 code points is below 40' in tight.stderr
    assert 'not met' not in tight.stderr
    for budget in ('6000', '20000'):  # a useful cap, or strip-only
        assert 'Warning' not in run('plan', 'fixtures/tools-40.json', '--budget', budget).stderr
