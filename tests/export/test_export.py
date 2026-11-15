from copy import deepcopy
import json
from pathlib import Path
import pytest
from jsonschema import Draft202012Validator
from agentwrench.export import load_session
from agentwrench.export.export import prepare, to_atif, validate_atif, markdown, jsonl, handoff
from agentwrench.export.model import InputError
from .harbor_schema import upstream_model
from .conftest import SESSIONS, codex_item, user

SCHEMA = json.loads((Path(__file__).resolve().parent / 'atif-v1.8.schema.json').read_text())
UPSTREAM = upstream_model()


@pytest.mark.parametrize('fmt', SESSIONS)
@pytest.mark.parametrize('view', ['conversation', 'tools', 'full'])
@pytest.mark.parametrize('mode', ['default', 'strict', 'off'])
def test_atif_full_upstream_schema_and_model(fmt, view, mode):
    doc = to_atif(prepare(load_session(SESSIONS[fmt]), view=view, redact=mode))
    Draft202012Validator(SCHEMA).validate(doc)
    # Also exercises upstream Python validators (references, IDs, timestamps).
    UPSTREAM.model_validate(doc)
    assert [s['step_id'] for s in doc['steps']] == list(range(1, len(doc['steps']) + 1))


def test_schema_regeneration_matches_vendored():
    assert UPSTREAM.model_json_schema() == SCHEMA


@pytest.mark.parametrize('fmt', SESSIONS)
def test_views_remove_nonselected_content(fmt):
    raw = load_session(SESSIONS[fmt])
    conversation = prepare(raw, view='conversation')
    text = json.dumps(conversation)
    for forbidden in ('tool_calls', 'observation', 'raw_input', 'reasoning', 'call-test', 'python -m pytest'):
        assert forbidden not in text
    tools = prepare(raw, view='tools')
    assert all(s['message'] == '' for s in tools['steps'])
    assert 'Fix the failing test.' not in json.dumps(tools)
    assert 'return value was wrong' not in json.dumps(tools)
    assert all(s.get('tool_calls') or s.get('observation') for s in tools['steps'])
    assert 'record' not in json.dumps(tools)


@pytest.mark.parametrize('fmt', SESSIONS)
@pytest.mark.parametrize('renderer', [markdown, jsonl, lambda d: json.dumps(to_atif(d), sort_keys=True), handoff])
def test_byte_determinism_and_no_mutation(fmt, renderer):
    raw = load_session(SESSIONS[fmt])
    original = deepcopy(raw)
    assert renderer(prepare(raw)) == renderer(prepare(raw))
    assert raw == original


def test_jsonl_header_and_steps():
    doc = prepare(load_session(SESSIONS['codex']))
    rows = [json.loads(x) for x in jsonl(doc).splitlines()]
    assert rows[0]['type'] == 'trajectory'
    assert rows[0]['schema_version'] == 'trajport/v1'
    assert len(rows) == len(doc['steps']) + 1
    assert all(r['type'] == 'step' for r in rows[1:])


def test_result_bound_including_raw_records(make_jsonl):
    rows = [user(), codex_item({'type': 'function_call', 'call_id': 't', 'name': 'x', 'arguments': {}}),
            codex_item({'type': 'function_call_output', 'call_id': 't', 'output': '0123456789' * 10})]
    raw = load_session(make_jsonl(rows))
    doc = prepare(raw, max_output_chars=10)
    result = doc['steps'][1]['observation']['results'][0]
    assert result['content'] == '0123456789\n[truncated; original_chars=100]'
    assert result['extra']['original_chars'] == 100
    assert '0123456789012345' not in json.dumps(doc)
    assert doc['extra']['truncated_fields'] == 2  # normalized + original result record
    assert prepare(raw, max_output_chars=0)['extra']['truncated_fields'] == 0
    assert raw['steps'][1]['observation']['results'][0]['content'] == '0123456789' * 10


def test_redact_before_truncate(make_jsonl):
    rows = [codex_item({'type': 'function_call_output', 'call_id': 'orphan', 'output': 'sk-syntheticSECRET123456789'})]
    doc = prepare(load_session(make_jsonl(rows)), max_output_chars=10)
    assert 'sk-synthet' not in json.dumps(doc)
    assert doc['extra']['redaction']['total'] >= 1


def test_markdown_fences_html_and_terminal_controls(make_jsonl):
    payload = '```\n<script>alert(1)</script>\n``````\n\x1b]52;c;AAAA\x07'
    output = markdown(prepare(load_session(make_jsonl([user(payload)]))))
    assert '\n```````\n' in output
    assert '<script>' not in output and '&lt;script&gt;' in output
    assert '\x1b' not in output and '\\u001b' in output


@pytest.mark.parametrize('change', ['step_id', 'source', 'timestamp', 'call_ref', 'duplicate_call', 'metric', 'extra_field'])
def test_atif_runtime_validator_rejects_invalid(change):
    doc = to_atif(prepare(load_session(SESSIONS['codex'])))
    if change == 'step_id':
        doc['steps'][0]['step_id'] = 4
    elif change == 'source':
        doc['steps'][0]['source'] = 'tool'
    elif change == 'timestamp':
        doc['steps'][0]['timestamp'] = 'yesterday'
    elif change == 'call_ref':
        doc['steps'][2]['observation']['results'][0]['source_call_id'] = 'other'
    elif change == 'duplicate_call':
        doc['steps'][3]['tool_calls'][0]['tool_call_id'] = 'call-test'
    elif change == 'metric':
        doc['steps'][2]['metrics'] = {'prompt_tokens': -1}
    else:
        doc['unknown'] = 1
    with pytest.raises(InputError, match='ATIF validation'):
        validate_atif(doc)


def test_empty_tools_view_error(make_jsonl):
    with pytest.raises(InputError, match='no supported steps'):
        prepare(load_session(make_jsonl([user()])), view='tools')


@pytest.mark.parametrize('fmt', SESSIONS)
def test_atif_token_totals(fmt):
    doc = to_atif(prepare(load_session(SESSIONS[fmt])))
    assert doc['final_metrics'] == {'total_steps': len(doc['steps']), 'total_prompt_tokens': 120,
                                    'total_completion_tokens': 30, 'total_cached_tokens': 20}


def test_orphan_atif_passes_upstream(make_jsonl):
    path = make_jsonl([codex_item({'type': 'function_call_output', 'call_id': 'missing', 'output': 'kept'})])
    doc = to_atif(prepare(load_session(path)))
    Draft202012Validator(SCHEMA).validate(doc)
    UPSTREAM.model_validate(doc)
    assert doc['extra']['warnings']['orphan_tool_result'] == 1
