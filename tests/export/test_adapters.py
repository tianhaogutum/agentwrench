import json
from pathlib import Path
import pytest
from agentwrench.export import load_session
from agentwrench.export.export import prepare, to_atif, handoff
from agentwrench.export.model import InputError
from .conftest import SESSIONS, FIXTURES, codex_item, user

GOLDEN = Path(__file__).parent / 'golden'


@pytest.mark.parametrize('fmt', SESSIONS)
def test_adapter_goldens(fmt):
    doc = load_session(SESSIONS[fmt])
    observed = {'session_id': doc['session_id'], 'version': doc['agent']['version'],
                'step_count': len(doc['steps']), 'sources': [s['source'] for s in doc['steps']],
                'timestamps': [s.get('timestamp') for s in doc['steps']],
                'warnings': doc['extra']['warnings']}
    assert observed == json.loads((GOLDEN / 'adapters.json').read_text())[fmt]
    assert any(s.get('extra', {}).get('reasoning') == 'Inspect the failing assertion.' for s in doc['steps'])


@pytest.mark.parametrize('fmt', SESSIONS)
def test_cross_agent_conversation_and_tools(fmt):
    doc = load_session(SESSIONS[fmt])
    conversation = prepare(doc, view='conversation')
    assert [{k: s[k] for k in ('source', 'message')} for s in conversation['steps']] == json.loads((GOLDEN / 'conversation.json').read_text())
    tools = []
    for s in doc['steps']:
        for c in s.get('tool_calls', []):
            tools.append({**c, 'result': s['observation']['results'][0]['content']})
    assert tools == json.loads((GOLDEN / 'tools.json').read_text())


@pytest.mark.parametrize('fmt', SESSIONS)
def test_summary_real_mapping(fmt):
    doc = prepare(load_session(SESSIONS[fmt]))
    result = handoff(doc)
    assert 'src/math.py' in result
    assert 'python -m pytest -q' in result
    assert '"exit_status": 1' in result
    assert '120' in result and '30' in result
    assert '(not recorded)' not in result


def test_cline_legacy_xml():
    doc = load_session(FIXTURES / 'cline/legacy')
    calls = [c for s in doc['steps'] for c in s.get('tool_calls', [])]
    assert [(c['name'], c['arguments']) for c in calls] == [
        ('write_to_file', {'path': 'hello.txt', 'content': 'Hello & goodbye'}),
        ('execute_command', {'command': 'printf hello', 'requires_approval': 'false'})]
    assert sum(len(s.get('observation', {}).get('results', [])) for s in doc['steps']) == 2
    brief = handoff(prepare(doc))
    assert 'hello.txt' in brief and '"exit_status": 0' in brief
    assert not (FIXTURES / 'cline/legacy/hello.txt').exists()


def test_sidechain_default_and_include():
    default = load_session(SESSIONS['claude'])
    assert default['extra']['skipped_sidechain_records'] == 1
    assert all(s['message'] != 'Sidechain only' for s in default['steps'])
    included = load_session(SESSIONS['claude'], include_sidechains=True)
    assert included['steps'][-1]['message'] == 'Sidechain only'


def test_unknowns_orphan_and_invalid_timestamp(make_jsonl):
    p = make_jsonl([user(), {'type': 'future', 'payload': {'opaque': 42}},
                    codex_item({'type': 'new_item', 'value': True}),
                    codex_item({'type': 'function_call_output', 'call_id': 'missing', 'output': 'kept'}, timestamp='not-a-time')])
    doc = load_session(p)
    assert doc['extra']['warnings'] == {'unknown_record': 2, 'invalid_timestamp': 1, 'orphan_tool_result': 1}
    assert doc['extra']['records'][0]['payload']['opaque'] == 42
    result = doc['steps'][-1]['observation']['results'][0]
    assert result['source_call_id'] == 'missing' and result['content'] == 'kept'
    atif = to_atif(prepare(doc))
    result = atif['steps'][-1]['observation']['results'][0]
    assert result['source_call_id'] is None
    assert result['extra']['unmatched_source_call_id'] == 'missing'


def test_truncated_last_line_continue(make_jsonl):
    p = make_jsonl([user()], '{"type":"response_item","payload":')
    doc = load_session(p)
    assert len(doc['steps']) == 1
    assert doc['extra']['warnings']['truncated_last_line'] == 1


@pytest.mark.parametrize('bad', ['{broken}\n', '[1,2]\n', 'NaN\n'])
def test_malformed_line_fails(make_jsonl, bad):
    with pytest.raises(InputError):
        load_session(make_jsonl([user()], bad))


def test_malformed_middle_fails(make_jsonl):
    p = make_jsonl([user()], '{bad}\n' + json.dumps(user()) + '\n')
    with pytest.raises(InputError, match='line 2'):
        load_session(p)


@pytest.mark.parametrize('media', [
    {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'aGVsbG8='}},
    {'type': 'input_image', 'image_url': 'data:image/jpeg;base64,aGVsbG8='},
    {'type': 'document', 'source': {'media_type': 'application/pdf', 'data': 'aGVsbG8='}},
    {'type': 'audio', 'source': {'media_type': 'audio/wav', 'data': 'aGVsbG8='}},
])
def test_media_placeholders_no_binary(make_jsonl, media):
    p = make_jsonl([codex_item({'type': 'message', 'role': 'user', 'content': [media]})])
    doc = load_session(p)
    assert 'encoded_chars=8' in doc['steps'][0]['message']
    assert 'aGVsbG8=' not in json.dumps(doc)
    assert 'omitted' in doc['steps'][0]['message']


def test_claude_result_mixed_text_image(make_jsonl):
    p = make_jsonl([
        {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'name': 'read', 'id': 't1', 'input': {}}]}},
        {'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'text', 'text': 'Also explain it'}, {'type': 'tool_result', 'tool_use_id': 't1', 'content': [{'type': 'text', 'text': 'result'}, {'type': 'image', 'source': {'media_type': 'image/png', 'data': 'AAAA'}}]}]}}
    ])
    doc = load_session(p)
    assert doc['steps'][1]['message'] == 'Also explain it'
    assert 'result\n[media: image/png' in doc['steps'][0]['observation']['results'][0]['content']


def test_codex_cumulative_usage_not_summed(make_jsonl):
    rows = [user()]
    for n in (100, 200):
        rows.append({'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'total_token_usage': {'input_tokens': n, 'output_tokens': 5}}}})
    doc = load_session(make_jsonl(rows))
    assert doc['extra']['token_usage']['input_tokens'] == 200


def test_claude_usage_repeated_response_id(make_jsonl):
    rows = [{'type': 'assistant', 'message': {'id': 'response1', 'role': 'assistant', 'content': [{'type': 'text', 'text': str(n)}], 'usage': {'input_tokens': 10, 'cache_read_input_tokens': 20, 'cache_creation_input_tokens': 30, 'output_tokens': n}}} for n in (1, 2)]
    doc = load_session(make_jsonl(rows))
    assert 'metrics' not in doc['steps'][0]
    assert doc['steps'][1]['metrics']['prompt_tokens'] == 60
    assert doc['steps'][1]['metrics']['completion_tokens'] == 2


def test_cline_child_symlink_escape(tmp_path):
    outside = tmp_path / 'outside.json'
    outside.write_text('[{"role":"user","content":"sensitive"}]')
    task = tmp_path / 'task'
    task.mkdir()
    (task / 'api_conversation_history.json').symlink_to(outside)
    with pytest.raises(InputError, match='escapes'):
        load_session(task)


def test_cline_ambiguous_timestamp_not_guessed(tmp_path):
    (tmp_path / 'api_conversation_history.json').write_text('[{"role":"user","content":"same"}]')
    (tmp_path / 'ui_messages.json').write_text('[{"text":"same","ts":1000},{"text":"same","ts":2000}]')
    assert 'timestamp' not in load_session(tmp_path)['steps'][0]


def test_unknown_claude_record_and_content(make_jsonl):
    p = make_jsonl([{'type': 'future', 'data': {'a': 1}}, {'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'future', 'value': 9}]}}])
    doc = load_session(p)
    assert doc['extra']['warnings'] == {'unknown_record': 1, 'unknown_content': 1}
    assert doc['steps'][0]['extra']['record']['message']['content'][0]['value'] == 9


def test_duplicate_call_fails(make_jsonl):
    row = codex_item({'type': 'function_call', 'name': 'x', 'arguments': {}, 'call_id': 'duplicate'})
    with pytest.raises(InputError, match='duplicate'):
        load_session(make_jsonl([row, row]))


def test_raw_custom_input_and_generated_id(make_jsonl):
    doc = load_session(make_jsonl([codex_item({'type': 'custom_tool_call', 'name': 'apply_patch', 'input': '*** patch'})]))
    call = doc['steps'][0]['tool_calls'][0]
    assert call['arguments'] == {'raw_input': '*** patch'}
    assert call['extra']['generated_id'] is True


def test_structured_exit_code(make_jsonl):
    rows = [codex_item({'type': 'function_call', 'name': 'exec_command', 'call_id': 't', 'arguments': {'cmd': 'true'}}),
            codex_item({'type': 'function_call_output', 'call_id': 't', 'output': {'stdout': 'done', 'exit_code': 0}})]
    assert '"exit_status": 0' in handoff(prepare(load_session(make_jsonl(rows))))


def test_claude_structured_exit_code(make_jsonl):
    rows = [{'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'name': 'Bash', 'id': 't', 'input': {'command': 'false'}}]}},
            {'type': 'user', 'toolUseResult': {'exitCode': 7}, 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't', 'content': 'failed'}]}}]
    assert '"exit_status": 7' in handoff(prepare(load_session(make_jsonl(rows))))


@pytest.mark.parametrize('limit', ['MAX_FILE_BYTES', 'MAX_LINE_BYTES', 'MAX_RECORDS'])
def test_size_limits(monkeypatch, make_jsonl, limit):
    from agentwrench.export import adapters
    path = make_jsonl([user(), user()])
    monkeypatch.setattr(adapters, limit, 1)
    with pytest.raises(InputError, match='limit'):
        load_session(path)


@pytest.mark.parametrize('bad', ['{}', '[null]', '[{"role":"user","content":{}}]', '[{"role":"user","content":[null]}]'])
def test_cline_invalid_history(tmp_path, bad):
    (tmp_path / 'api_conversation_history.json').write_text(bad)
    with pytest.raises(InputError):
        load_session(tmp_path)


def test_cline_ui_symlink_outside_rejected(tmp_path):
    task = tmp_path / 'task'
    task.mkdir()
    (task / 'api_conversation_history.json').write_text('[{"role":"user","content":"hello"}]')
    external = tmp_path / 'ui.json'
    external.write_text('[]')
    (task / 'ui_messages.json').symlink_to(external)
    with pytest.raises(InputError, match='escapes'):
        load_session(task)


def test_encrypted_payloads_omitted(make_jsonl):
    rows = [user(), codex_item({'type': 'reasoning', 'summary': [], 'encrypted_content': 'DO_NOT_EXPORT_ENCRYPTED'}),
            {'type': 'future', 'data': {'type': 'redacted_thinking', 'data': 'DO_NOT_EXPORT_BINARY'}}]
    doc = load_session(make_jsonl(rows))
    assert 'DO_NOT_EXPORT' not in json.dumps(doc)


def test_unknown_block_kept_but_warned(make_jsonl):
    path = make_jsonl([codex_item({'type': 'message', 'role': 'user', 'content': [{'type': 'future_content', 'x': 42}]})])
    doc = load_session(path)
    assert doc['extra']['warnings']['unknown_content'] == 1
    assert doc['extra']['records'][0]['content_block']['x'] == 42
