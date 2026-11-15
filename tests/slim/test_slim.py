"""Port of test/slim.test.js."""

import copy
import json
import math
import re

import pytest

from agentwrench.slim.config import InputError
from agentwrench.slim.jsjson import parse, stringify
from agentwrench.slim.jsonio import read_json
from agentwrench.slim.plan import plan
from agentwrench.slim.slim import (
    apply_config, extract_tools, glob_match, is_visible, measure, truncate_description, validate_config,
)

from .conftest import TOOLS_40
from agentwrench.slim import collation

tools = extract_tools(read_json(str(TOOLS_40)))
annotations = ['description', 'title', 'examples', '$comment']


def utf8(text: str) -> int:
    return len(text.encode('utf-8'))


def locale_sorted(entries):
    return sorted(entries, key=lambda t: (-t['bytes'], collation.sort_key(t['name'])))


def test_synthetic_fixture_exact_byte_measurement_and_totals():
    assert len(tools) == 40
    one = [{'name': '你好', 'description': '🙂', 'inputSchema': {'type': 'object'}}]
    size = utf8(stringify(one[0]))
    assert measure(one)['bytes'] == size
    assert measure(one)['estimatedTokens'] == math.ceil(size / 4)
    assert sum(t['bytes'] for t in measure(tools)['perTool']) == measure(tools)['bytes']
    assert measure([])['estimatedTokens'] == 0


def test_identity_config_does_not_mutate_input_and_filtering_retains_order():
    assert apply_config(tools, {}) == tools
    snapshot = stringify(tools)
    result = apply_config(tools, {'allow': ['read_*', 'search_*'], 'deny': ['*_issue'], 'stripSchemaAnnotations': annotations})
    assert len(result) == 14
    assert stringify(tools) == snapshot
    assert [t['name'] for t in result] == [t['name'] for t in tools if re.match(r'^(read|search)_', t['name']) and not t['name'].endswith('_issue')]


def test_only_annotations_at_schema_nodes_removed_structural_keywords_and_data_survive():
    schema = {
        'type': 'object', 'description': 'root', 'title': 'root', 'examples': [{'description': 'data'}],
        'properties': {
            'description': {'type': 'string', 'description': 'remove', 'enum': ['description', 'title'], 'default': 'keep', 'minLength': 1, 'maxLength': 4, 'format': 'date'},
            'title': {'const': {'title': 'literal data', 'description': 'keep'}, 'description': 'remove'},
            'examples': {'type': 'array', 'items': {'type': 'number', 'minimum': 1, 'maximum': 4, 'multipleOf': 0.5, 'title': 'remove'}},
            '__proto__': False,
        },
        'required': ['description', 'title'], 'additionalProperties': False,
        '$defs': {'title': {'type': 'string', 'title': 'remove'}},
        'anyOf': [{'required': ['title'], '$comment': 'remove'}], 'oneOf': [False, {'enum': [1, 2], 'description': 'remove'}],
        'allOf': [{'properties': {'value': {'$ref': '#/$defs/title'}}}],
        'xUnknown': {'description': 'extension data kept'},
    }
    original = [{'name': 'edge', 'inputSchema': schema, 'outputSchema': schema, 'annotations': {'title': 'keep'}}]
    slim = apply_config(original, {'stripSchemaAnnotations': annotations})[0]
    expected = copy.deepcopy(schema)
    del expected['description'], expected['title'], expected['examples']
    del expected['properties']['description']['description'], expected['properties']['title']['description']
    del expected['properties']['examples']['items']['title'], expected['$defs']['title']['title']
    del expected['anyOf'][0]['$comment'], expected['oneOf'][1]['description']
    assert slim['inputSchema'] == expected
    assert slim['outputSchema'] == schema
    assert slim['annotations'] == {'title': 'keep'}
    assert 'default' not in apply_config(original, {'stripSchemaAnnotations': ['default']})[0]['inputSchema']['properties']['description']


def test_schema_valued_keywords_legacy_tuple_items_and_dependencies_handled():
    leaf = {'type': 'string', 'description': 'remove'}
    schema = {'properties': {'default': leaf}, 'patternProperties': {'^x': leaf}, 'definitions': {'description': leaf},
              'dependentSchemas': {'x': leaf}, 'items': [leaf, False], 'prefixItems': [leaf],
              'dependencies': {'description': ['title'], 'x': leaf}}
    keys = ['not', 'if', 'then', 'else', 'contains', 'propertyNames', 'unevaluatedProperties', 'additionalItems', 'contentSchema']
    for key in keys:
        schema[key] = leaf
    got = apply_config([{'name': 'test', 'inputSchema': schema}], {'stripSchemaAnnotations': ['description']})[0]['inputSchema']
    assert 'description' not in got['properties']['default']
    assert 'description' not in got['definitions']['description']
    assert got['dependencies']['description'] == ['title']
    assert got['items'] == [{'type': 'string'}, False]
    for key in keys:
        assert got[key] == {'type': 'string'}


def test_globs_are_case_sensitive_anchored_literal_except_star_and_question_deny_wins():
    for p, s, expected in [('*', '', True), ('read_*', 'read_a', True), ('read_*', 'xread_a', False), ('a?c', 'abc', True),
                           ('a?c', 'ac', False), ('[a]', 'a', False), ('a.b', 'a.b', True), ('a.b', 'axb', False),
                           ('*a*b', 'xaaab', True), ('?', '🙂', True)]:
        assert glob_match(p, s) == expected, f'{p} {s}'
    assert is_visible('read_admin', {'allow': ['read_*'], 'deny': ['*_admin']}) is False
    assert is_visible('Read_x', {'allow': ['read_*']}) is False
    assert is_visible('x', {'allow': []}) is False


def test_description_caps_respect_sentence_word_boundaries_and_unicode_code_points():
    assert truncate_description('First sentence. A longer sentence.', 23) == 'First sentence.'
    assert truncate_description('hello world', 8) == 'hello'
    assert truncate_description('hello world', 5) == 'hello'
    assert truncate_description('你好🙂世界', 3) == '你好🙂'
    assert truncate_description('longword', 4) == 'long'
    assert truncate_description('abc', 0) == ''
    for text in ['a. b?c longer word', 'hello 🙂 world. End!', '你好。 下一句！ 最后', '  a  b\nc.', 'foo?bar. baz']:
        prev = ''
        for cap in range(len(text) + 1):
            result = truncate_description(text, cap)
            assert len(result) <= cap
            assert utf8(result) >= utf8(prev), f'{text}: cap {cap}'
            prev = result


def test_config_errors_identify_key_prototype_like_schema_field_remains_own_data():
    for config in [{'wat': True}, {'allow': '*'}, {'deny': ['']}, {'maxDescriptionChars': -1}, {'stripSchemaAnnotations': ['type']}]:
        with pytest.raises(InputError, match=r'config\.'):
            validate_config(config)
    schema = parse('{"type":"object","properties":{"__proto__":{"type":"string","description":"remove"},"constructor":{"type":"string"}}}')
    got = apply_config([{'name': 'safe', 'inputSchema': schema}], {'stripSchemaAnnotations': annotations})[0]
    assert '__proto__' in got['inputSchema']['properties']
    assert got['inputSchema']['properties']['__proto__']['type'] == 'string'
    assert not hasattr({}, 'polluted')


def test_invalid_tool_snapshots_rejected_and_deep_schema_fails_explicitly():
    for value in [{'result': {'tools': []}}, [None], [{'name': 'a'}], [{'name': '', 'inputSchema': {}}], [tools[0], tools[0]]]:
        with pytest.raises(InputError):
            extract_tools(value)
    schema = {'type': 'string'}
    for _ in range(130):
        schema = {'items': schema}
    with pytest.raises(InputError, match='128'):
        apply_config([{'name': 'deep', 'inputSchema': schema}])


def test_allow_list_with_zero_matches_warns_deny_only_empty_page_does_not_warn():
    warnings = []
    assert apply_config(tools, {'allow': ['absent*']}, warnings.append) == []
    assert re.search('zero tools', warnings[0])
    apply_config(tools, {'deny': ['*']}, warnings.append)
    assert len(warnings) == 1


def test_planner_greedily_strips_then_caps_meets_feasible_budgets_and_never_auto_denies():
    p = plan(tools, 6000)
    assert p['meetsBudget']
    assert p['after']['estimatedTokens'] <= 6000
    assert 'deny' not in p['config']
    assert p['after']['tools'] == 40
    assert p['config']['stripSchemaAnnotations'] == annotations
    assert 'maxDescriptionChars' not in plan(tools, measure(tools)['estimatedTokens'])['config']
    assert plan(tools, measure(tools)['estimatedTokens'])['config'] == {}
    assert plan(tools, 4000)['meetsBudget'] is False
    impossible = plan(tools, 1)
    assert impossible['meetsBudget'] is False
    assert impossible['config']['maxDescriptionChars'] == 0
    assert impossible['denySuggestions']
    assert 'deny' not in impossible['config']
    assert impossible['denySuggestions'] == locale_sorted(impossible['denySuggestions'])


def test_budget_planner_results_and_stage_costs_are_monotone_input_unchanged():
    snapshot = stringify(tools)
    previous = math.inf
    for budget in [50000, 20000, 10000, 5000, 4000, 3000, 1000, 1]:
        p = plan(tools, budget)
        assert p['after']['estimatedTokens'] <= previous
        previous = p['after']['estimatedTokens']
        for i in range(1, len(p['stages'])):
            assert p['stages'][i]['bytes'] <= p['stages'][i - 1]['bytes']
    assert stringify(tools) == snapshot
    assert plan([], 1)['config'] == {}
    for budget in [0, -1, 1.5, math.nan, math.inf]:
        with pytest.raises(InputError, match='budget'):
            plan(tools, budget)


def test_annotation_referenced_through_a_json_pointer_remains_resolvable():
    input_schema = {
        'default': {'type': 'string', 'minLength': 3, 'description': 'literal target'},
        'examples': [{'type': 'number', 'minimum': 2}],
        'description': 'remove',
        'anyOf': [{'$ref': '#/default'}, {'$ref': '#/examples/0'}],
    }
    slim = apply_config([{'name': 'ref', 'inputSchema': input_schema}], {'stripSchemaAnnotations': ['default', 'examples', 'description']})[0]['inputSchema']
    assert slim['default'] == input_schema['default']
    assert slim['examples'] == input_schema['examples']
    assert 'description' not in slim
    assert slim['anyOf'] == input_schema['anyOf']


def test_property_reference_names_are_distinguished_from_removable_annotation_keys():
    input_schema = {
        'type': 'object', 'default': {}, 'properties': {'default': {'type': 'string', 'default': 'remove'}},
        'allOf': [{'$ref': '#/properties/default'}],
    }
    slim = apply_config([{'name': 'ref', 'inputSchema': input_schema}], {'stripSchemaAnnotations': ['default']})[0]['inputSchema']
    assert 'default' not in slim
    assert 'default' not in slim['properties']['default']
    assert slim['properties']['default'] == {'type': 'string'}
    definitions = {'$defs': {'properties': {'default': {'type': 'string'}}}, '$ref': '#/$defs/properties/default'}
    safe = apply_config([{'name': 'nested', 'inputSchema': definitions}], {'stripSchemaAnnotations': ['default']})[0]['inputSchema']
    assert safe == definitions


# --- Python-port specific: JavaScript serialization semantics -------------------

def test_js_json_serialization_matches_javascript():
    assert stringify([1e21, 1e-7, -0.0, 0.1, 123456789012345680000.0, 1.5e300, 5e-324, 100, 1.0, 2**53, 0.000001]) == \
        '[1e+21,1e-7,0,0.1,123456789012345680000,1.5e+300,5e-324,100,1,9007199254740992,0.000001]'
    assert stringify('\ud800\u2028\x7f\x00\x1f"\\') == '"\\ud800\u2028\x7f\\u0000\\u001f\\"\\\\"'
    assert stringify({'b': 1, '2': 1, '1': 1, '01': 1, '4294967294': 1, '4294967295': 1, '-1': 1}) == \
        '{"1":1,"2":1,"4294967294":1,"b":1,"01":1,"4294967295":1,"-1":1}'
    assert stringify({'a': [], 'b': {}, 'c': [1]}, 2) == '{\n  "a": [],\n  "b": {},\n  "c": [\n    1\n  ]\n}'
    deep = '[' * 100000 + ']' * 100000
    value = parse(deep)
    for _ in range(5):
        value = value[0]
    with pytest.raises(ValueError):
        parse('NaN')
    with pytest.raises(ValueError):
        parse('[' * 100000 + ']' * 99999)
    assert json.loads(stringify(parse('{"a":1,"a":2}'))) == {'a': 2}
