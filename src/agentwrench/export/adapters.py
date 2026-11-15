"""Readers for public on-disk formats; synthetic fixtures document supported shapes."""
import os
from pathlib import Path
import re
import stat

from .model import Builder, InputError, dumps, loads, media_placeholder, safe_data

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_LINE_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 200_000
TEXT_TYPES = {'text', 'input_text', 'output_text', 'summary_text'}
MEDIA_TYPES = {'image', 'input_image', 'image_url', 'audio', 'input_audio', 'document', 'binary'}
XML_TOOLS = {'execute_command', 'read_file', 'write_to_file', 'replace_in_file',
             'apply_patch', 'list_files', 'search_files', 'attempt_completion',
             'use_mcp_tool', 'access_mcp_resource', 'browser_action'}


def read_text(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                raise InputError('input must be a regular file')
            data = f.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise InputError('input exceeds 64 MiB limit')
        return data.decode('utf-8-sig')
    except UnicodeError:
        raise InputError('input is not UTF-8') from None


def parse_json(text):
    try:
        return loads(text)
    except (ValueError, RecursionError):
        raise InputError('invalid JSON document') from None


def read_jsonl(path, *, strict_jsonl=False):
    """Read LF-framed objects; strict mode rejects even a malformed final tail."""
    text = read_text(path)
    # Unicode line separators are valid JSON string data, not record boundaries.
    lines = text.split('\n')
    nonempty = [i for i, line in enumerate(lines) if line.strip(' \t\r')]
    if len(nonempty) > MAX_RECORDS:
        raise InputError('too many records (limit 200000)')
    rows, truncated = [], False
    for i in nonempty:
        line = lines[i]
        if i < len(lines) - 1:
            # Keep physical terminators in the byte limit; CRLF's CR is retained.
            line += '\n'
        if len(line.encode('utf-8')) > MAX_LINE_BYTES:
            raise InputError(f'JSONL line {i + 1} exceeds 8 MiB limit')
        try:
            obj = parse_json(line)
        except InputError:
            if not strict_jsonl and i == len(lines) - 1:
                truncated = True
                continue
            raise InputError(f'invalid JSON at line {i + 1}') from None
        if not isinstance(obj, dict):
            raise InputError(f'JSONL line {i + 1} must be an object')
        rows.append(obj)
    return rows, truncated


def content_text(content, builder):
    if isinstance(content, str):
        return content
    if content is None:
        return ''
    if not isinstance(content, list):
        builder.warn('unknown_content')
        return dumps(safe_data(content))
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            kind = block.get('type')
            if kind in TEXT_TYPES:
                text = block.get('text', '')
                if not isinstance(text, str):
                    raise InputError('text content must be a string')
                parts.append(text)
            elif kind in MEDIA_TYPES:
                parts.append(media_placeholder(block))
            else:
                builder.warn('unknown_content')
                parts.append('[unsupported content block]')
                builder.record({'content_block': block})
        else:
            raise InputError('content blocks must be objects or strings')
    return '\n'.join(parts)


def codex(rows, b):
    for row in rows:
        kind, p = row.get('type'), row.get('payload', {})
        ts = row.get('timestamp')
        if not isinstance(p, dict):
            b.record(row, 'unknown_record')
            continue
        if kind == 'session_meta':
            b.doc['session_id'] = str(p.get('id', 'unknown'))
            b.doc['agent']['version'] = str(p.get('cli_version', 'unknown'))
            b.record(row)
        elif kind == 'response_item':
            t = p.get('type')
            if t in {'message', 'agent_message'}:
                role = p.get('role', 'assistant' if t == 'agent_message' else None)
                source = {'user': 'user', 'assistant': 'agent', 'system': 'system', 'developer': 'system'}.get(role)
                if not source:
                    b.record(row, 'unknown_role')
                    continue
                s = b.step(source, content_text(p.get('content', p.get('text', '')), b), ts, {'record': row})
                if p.get('phase'):
                    s['extra']['phase'] = p['phase']
            elif t in {'function_call', 'custom_tool_call'}:
                s = b.step('agent', ts=ts, extra={'record': row})
                b.call(s, p.get('name'), p.get('arguments', p.get('input', {})), p.get('call_id'))
            elif t in {'function_call_output', 'custom_tool_call_output'}:
                output = p.get('output', '')
                result_extra = {'record': row}
                for candidate in (p, output if isinstance(output, dict) else {}):
                    for key in ('exit_code', 'exitCode', 'exit_status'):
                        if type(candidate.get(key)) is int:
                            result_extra[key] = candidate[key]
                output = content_text(output, b) if isinstance(output, list) else output if isinstance(output, str) else dumps(safe_data(output))
                b.result(p.get('call_id'), output, ts, result_extra)
            elif t == 'reasoning':
                s = b.step('agent', ts=ts, extra={'record': row})
                s['extra']['reasoning'] = content_text(p.get('summary', []), b)
            else:
                b.record(row, 'unknown_record')
        elif kind == 'event_msg':
            t = p.get('type')
            if t == 'token_count':
                info = p.get('info') or {}
                if not isinstance(info, dict):
                    raise InputError('token usage info must be an object')
                if isinstance(info.get('total_token_usage'), dict):
                    # Codex cumulative snapshots replace, never sum.
                    b.doc['extra']['token_usage'] = safe_data(info['total_token_usage'])
                if isinstance(info.get('last_token_usage'), dict):
                    agents = [s for s in b.doc['steps'] if s['source'] == 'agent']
                    if agents:
                        b.metrics(agents[-1], info['last_token_usage'])
                b.record(row)
            elif t in {'task_started', 'task_complete', 'turn_aborted', 'user_message', 'agent_message', 'agent_reasoning'}:
                # Event messages commonly mirror response_items. Keep metadata only.
                b.record(row)
            else:
                b.record(row, 'unknown_record')
        elif kind in {'turn_context', 'compacted'}:
            b.record(row)
        else:
            b.record(row, 'unknown_record')


def anthropic_message(message, row, b, *, ts=None, cline=False):
    if not isinstance(message, dict):
        raise InputError('message must be an object')
    role = message.get('role', row.get('type'))
    source = {'assistant': 'agent', 'user': 'user', 'system': 'system'}.get(role)
    if source is None:
        b.record(row, 'unknown_role')
        return
    content = message.get('content', '')
    if isinstance(content, str):
        blocks = [{'type': 'text', 'text': content}]
    elif isinstance(content, list):
        blocks = content
    else:
        raise InputError('message content must be text or an array')
    texts, thinking, calls, results, unknown = [], [], [], [], []
    for block in blocks:
        if not isinstance(block, dict):
            raise InputError('message block must be an object')
        t = block.get('type')
        if t == 'tool_use':
            calls.append(block)
        elif t == 'tool_result':
            results.append(block)
        elif t == 'thinking':
            thinking.append(str(block.get('thinking', '')))
        elif t == 'redacted_thinking':
            thinking.append('[encrypted reasoning omitted]')
        elif t in TEXT_TYPES or t in MEDIA_TYPES:
            texts.append(content_text([block], b))
        else:
            unknown.append(block)
            b.warn('unknown_content')
    text = '\n'.join(texts)
    # Older Cline stores tool invocations as literal XML inside assistant text.
    xml_calls = []
    if cline and source == 'agent':
        pattern = r'<(' + '|'.join(sorted(XML_TOOLS)) + r')>(.*?)</\1>'
        for match in re.finditer(pattern, text, re.S):
            arguments = {m[1]: m[2] for m in re.finditer(r'<([a-z_]+)>(.*?)</\1>', match[2], re.S)}
            if arguments:
                xml_calls.append((match[1], arguments))
        if xml_calls:
            text = re.sub(pattern, '', text, flags=re.S).strip()
    if cline and source == 'user' and not results:
        match = re.match(r"^\[([a-z_]+)(?:\s+for\s+[^\n]*)?\]\s*Result:\s*([\s\S]*)$", text)
        if match and match[1] in XML_TOOLS:
            pending = [c['id'] for s in b.doc['steps'] for c in s.get('tool_calls', [])
                       if c['name'] == match[1] and not any(r.get('source_call_id') == c['id'] for r in s.get('observation', {}).get('results', []))]
            b.result(pending[-1] if pending else None, match[2], ts, {'record': row, 'legacy_cline_result': True})
            return
    s = None
    if text or calls or thinking or unknown or xml_calls:
        s = b.step(source, text, ts, {'record': row})
        if calls and source != 'agent':
            raise InputError('tool_use requires an assistant message')
        for c in calls:
            b.call(s, c.get('name'), c.get('input', {}), c.get('id'))
        for name, args in xml_calls:
            b.call(s, name, args)
        if thinking:
            s['extra']['reasoning'] = '\n'.join(thinking)
        usage = message.get('usage')
        if usage is not None and source == 'agent':
            # Multiple Claude log lines can represent blocks of one response.
            # Keep the last usage snapshot for that response, never sum snapshots.
            message_id = message.get('id')
            if message_id:
                for prior in b.doc['steps'][:-1]:
                    if prior.get('extra', {}).get('usage_message_id') == message_id:
                        prior.pop('metrics', None)
                s['extra']['usage_message_id'] = message_id
            b.metrics(s, usage, anthropic=True)
        if message.get('model') and source == 'agent':
            s['extra']['model_name'] = str(message['model'])
    elif not results:
        s = b.step(source, '', ts, {'record': row})
    for result in results:
        result_extra = {'is_error': result.get('is_error', False), 'record': row}
        native_result = row.get('toolUseResult')
        for candidate in (result, native_result if isinstance(native_result, dict) else {}):
            for key in ('exit_code', 'exitCode', 'exit_status', 'returnCode'):
                if type(candidate.get(key)) is int:
                    result_extra['exit_code'] = candidate[key]
        b.result(result.get('tool_use_id'), content_text(result.get('content', ''), b), ts,
                 result_extra)


def claude(rows, b, include_sidechains):
    for row in rows:
        if row.get('isSidechain') and not include_sidechains:
            b.doc['extra']['skipped_sidechain_records'] = b.doc['extra'].get('skipped_sidechain_records', 0) + 1
            continue
        if row.get('sessionId'):
            b.doc['session_id'] = str(row['sessionId'])
        if row.get('version'):
            b.doc['agent']['version'] = str(row['version'])
        kind = row.get('type')
        if kind in {'user', 'assistant'}:
            anthropic_message(row.get('message'), row, b, ts=row.get('timestamp'))
        elif kind in {'progress', 'file-history-snapshot', 'queue-operation', 'summary', 'system', 'last-prompt'}:
            b.doc['extra']['ignored_nonconversation_records'] = b.doc['extra'].get('ignored_nonconversation_records', 0) + 1
        else:
            b.record(row, 'unknown_record')


def task_child(root, name):
    child = root / name
    if not child.resolve().is_relative_to(root.resolve()):
        raise InputError('Cline task file escapes task directory')
    return child


def cline(root, b):
    rows = parse_json(read_text(task_child(root, 'api_conversation_history.json')))
    if not isinstance(rows, list) or len(rows) > MAX_RECORDS:
        raise InputError('Cline history must be an array of at most 200000 messages')
    b.doc['session_id'] = root.name
    ui_path = task_child(root, 'ui_messages.json')
    ui = []
    if ui_path.exists():
        ui = parse_json(read_text(ui_path))
        if not isinstance(ui, list) or len(ui) > MAX_RECORDS or not all(isinstance(x, dict) for x in ui):
            raise InputError('Cline UI history must be an array of at most 200000 objects')
        b.doc['extra']['ui_messages'] = safe_data(ui)
        usage_rows = []
        for item in ui:
            if item.get('say') == 'api_req_started' and isinstance(item.get('text'), str):
                try:
                    usage = parse_json(item['text'])
                except InputError:
                    b.warn('unparsed_ui_usage')
                    continue
                if isinstance(usage, dict) and any(k in usage for k in ('tokensIn', 'tokensOut', 'cacheReads', 'cacheWrites')):
                    usage_rows.append(usage)
        if usage_rows:
            def total(key):
                return sum(v for u in usage_rows if type(v := u.get(key)) is int and v >= 0)
            b.doc['extra']['token_usage'] = {
                'input_tokens': total('tokensIn') + total('cacheReads') + total('cacheWrites'),
                'output_tokens': total('tokensOut'), 'cached_input_tokens': total('cacheReads'),
                'cache_creation_input_tokens': total('cacheWrites')}
    # Only exact text matches establish timestamps; never assume index alignment.
    for row in rows:
        if not isinstance(row, dict):
            raise InputError('Cline message must be an object')
        ts = row.get('ts', row.get('timestamp'))
        raw_content = row.get('content')
        comparable = raw_content
        if isinstance(raw_content, list) and all(isinstance(x, dict) and x.get('type') == 'text' for x in raw_content):
            comparable = '\n'.join(str(x.get('text', '')) for x in raw_content)
        matches = [x.get('ts') for x in ui if isinstance(comparable, str) and x.get('text') == comparable]
        if ts is None and len(matches) == 1:
            ts = matches[0]
        anthropic_message(row, row, b, ts=ts, cline=True)


def _jsonl_session(rows, fmt, include_sidechains, truncated=False):
    if fmt == 'auto':
        candidates = set()
        for row in rows[:100]:
            if row.get('type') in {'session_meta', 'response_item', 'event_msg', 'turn_context'} and 'payload' in row:
                candidates.add('codex')
            if row.get('type') in {'user', 'assistant'} and isinstance(row.get('message'), dict):
                candidates.add('claude')
        if len(candidates) != 1:
            raise InputError('format not detectable or ambiguous; use --from')
        fmt = candidates.pop()
    b = Builder(fmt)
    if truncated:
        b.warn('truncated_last_line')
    if fmt == 'codex':
        codex(rows, b)
    else:
        claude(rows, b, include_sidechains)
    return b.finish()


def validate_session(path, fmt='auto', *, include_sidechains=False):
    """Strictly parse raw Codex/Claude JSONL and return counts, without export.

    Records count every nonblank JSONL object, including metadata and excluded
    sidechains. Steps and warnings follow the same adapter rules as load_session.
    No redaction, view filtering, export serialization or file writing is done.
    This checks the supported adapter subset, not an exhaustive vendor schema.
    """
    if fmt not in {'auto', 'codex', 'claude'}:
        raise InputError('unsupported validation format')
    path = Path(path)
    if path.is_dir():
        raise InputError('validation requires a Codex or Claude JSONL file')
    rows, _ = read_jsonl(path, strict_jsonl=True)
    doc = _jsonl_session(rows, fmt, include_sidechains)
    warnings = doc['extra']['warnings']
    return {'format': doc['agent']['name'], 'records': len(rows), 'steps': len(doc['steps']),
            'warnings': warnings, 'warning_count': sum(warnings.values())}


def load_session(path, fmt='auto', *, include_sidechains=False, strict_jsonl=False):
    """Load an agent export; strict_jsonl disables malformed-tail recovery.

    The option applies to Codex/Claude JSONL. Cline JSON documents are always
    parsed in full, so their validation is unchanged.
    """
    path = Path(path)
    if fmt not in {'auto', 'codex', 'claude', 'cline'}:
        raise InputError('unsupported input format')
    if path.is_dir():
        if fmt not in {'auto', 'cline'}:
            raise InputError('this format requires a JSONL file')
        fmt = 'cline'
        b = Builder(fmt)
        cline(path, b)
    else:
        if fmt == 'cline':
            raise InputError('Cline input must be a task directory')
        rows, truncated = read_jsonl(path, strict_jsonl=strict_jsonl)
        return _jsonl_session(rows, fmt, include_sidechains, truncated)
    return b.finish()
