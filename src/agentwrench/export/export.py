"""View projection, ATIF mapping, validation, and inert Markdown rendering."""
from copy import deepcopy
from datetime import datetime
import re
from .model import InputError, dumps, exit_status
from .redact import Redactor


def project_step(step, view):
    """Select normalized fields without mutating or renumbering the input.

    Callers validate the view and their own envelope before projection. None
    means the step has no content in this view; full retains the input fields.
    """
    if view == 'conversation':
        if step['source'] not in {'user', 'agent'} or not step['message']:
            return None
        return {k: v for k, v in step.items() if k in {'step_id', 'timestamp', 'source', 'message'}}
    if view == 'tools':
        if not (step.get('tool_calls') or step.get('observation')):
            return None
        step = deepcopy({k: v for k, v in step.items()
                         if k in {'step_id', 'timestamp', 'source', 'tool_calls', 'observation'}})
        step['message'] = ''
        # Results retain status and orphan flags, without embedded conversation.
        for result in step.get('observation', {}).get('results', []):
            result['extra'] = {k: v for k, v in result.get('extra', {}).items() if k != 'record'}
    return step


def prepare(doc, *, view='full', redact='default', max_output_chars=20000):
    doc = deepcopy(doc)
    if view not in {'full', 'tools', 'conversation'}:
        raise InputError('invalid view')
    if max_output_chars < 0:
        raise InputError('max-output-chars must be nonnegative')
    truncated = 0

    def shorten(value):
        nonlocal truncated
        if isinstance(value, str) and max_output_chars and len(value) > max_output_chars:
            truncated += 1
            return value[:max_output_chars] + f'\n[truncated; original_chars={len(value)}]'
        return value

    def bound_raw(value):
        if isinstance(value, dict):
            result = {k: bound_raw(v) for k, v in value.items()}
            t = value.get('type')
            key = 'output' if t in {'function_call_output', 'custom_tool_call_output'} else 'content' if t == 'tool_result' else None
            if key and key in result:
                # Lists/objects are represented as JSON before truncation.
                raw = result[key]
                result[key] = shorten(raw if isinstance(raw, str) else dumps(raw))
            return result
        if isinstance(value, list):
            return [bound_raw(v) for v in value]
        return value

    steps = []
    for step in doc['steps']:
        step = project_step(step, view)
        if step is None:
            continue
        step['step_id'] = len(steps) + 1
        steps.append(step)
    if not steps:
        raise InputError('selected view has no supported steps')
    doc['steps'] = steps
    diagnostics = doc['extra'].get('warnings', {})
    if view != 'full':
        doc['extra'] = {'warnings': diagnostics}
    doc['extra']['view'] = view
    redactor = Redactor(redact)
    doc = redactor.walk(doc)
    for step in doc['steps']:
        for r in step.get('observation', {}).get('results', []):
            original = r['content']
            r['content'] = shorten(original)
            if r['content'] != original:
                r.setdefault('extra', {})['original_chars'] = len(original)
    doc = bound_raw(doc)
    doc['extra']['truncated_fields'] = truncated
    doc['extra']['redaction'] = {'mode': redact, 'counts': dict(sorted(redactor.counts.items())),
                                'total': sum(redactor.counts.values())}
    return doc


def to_atif(doc):
    out = deepcopy(doc)
    out['schema_version'] = 'ATIF-v1.8'
    totals = {'total_steps': len(out['steps'])}
    raw_usage = out['extra'].get('token_usage')
    if isinstance(raw_usage, dict):
        for source, target in [('input_tokens', 'total_prompt_tokens'),
                               ('output_tokens', 'total_completion_tokens'),
                               ('cached_input_tokens', 'total_cached_tokens')]:
            if type(raw_usage.get(source)) is int and raw_usage[source] >= 0:
                totals[target] = raw_usage[source]
    else:
        for source in ('prompt_tokens', 'completion_tokens', 'cached_tokens'):
            values = [s['metrics'][source] for s in out['steps'] if source in s.get('metrics', {})]
            if values:
                totals['total_' + source] = sum(values)
    out['final_metrics'] = totals
    for step in out['steps']:
        for call in step.get('tool_calls', []):
            call['tool_call_id'] = call.pop('id')
            call['function_name'] = call.pop('name')
        extra = step.get('extra', {})
        if 'reasoning' in extra and step['source'] == 'agent':
            step['reasoning_content'] = extra.pop('reasoning')
        if 'model_name' in extra and step['source'] == 'agent':
            step['model_name'] = extra.pop('model_name')
        for r in step.get('observation', {}).get('results', []):
            if r.get('extra', {}).get('orphan'):
                r['extra']['unmatched_source_call_id'] = r['source_call_id']
                r['source_call_id'] = None
    validate_atif(out)
    return out


def validate_atif(doc):
    """Validate the emitted text-only ATIF-v1.8 subset and semantic invariants.

    Full upstream JSON Schema validation is also performed in offline tests.
    This intentionally does not claim to validate arbitrary ATIF documents.
    """
    def require(condition, message):
        if not condition:
            raise InputError('ATIF validation: ' + message)

    require(doc.get('schema_version') == 'ATIF-v1.8', 'unsupported version')
    require(set(doc) <= {'schema_version', 'session_id', 'agent', 'steps', 'extra', 'final_metrics'}, 'unknown root field')
    require(isinstance(doc.get('session_id'), str), 'session ID must be text')
    agent = doc.get('agent', {})
    require(set(agent) <= {'name', 'version'}, 'unknown agent field')
    require(all(isinstance(agent.get(k), str) for k in ('name', 'version')), 'agent name/version required')
    steps = doc.get('steps')
    require(isinstance(steps, list) and bool(steps), 'steps must be nonempty')
    for key, value in doc.get('final_metrics', {}).items():
        require(key in {'total_steps', 'total_prompt_tokens', 'total_completion_tokens', 'total_cached_tokens'}, 'unknown aggregate metric')
        require(type(value) is int and value >= 0, 'invalid aggregate metric')
    ids = set()
    for i, s in enumerate(steps, 1):
        require(set(s) <= {'step_id', 'source', 'message', 'timestamp', 'extra', 'tool_calls', 'observation', 'metrics', 'reasoning_content', 'model_name'}, 'unknown step field')
        require(type(s.get('step_id')) is int and s['step_id'] == i, 'nonsequential step IDs')
        require(s.get('source') in {'user', 'agent', 'system'}, 'invalid source')
        require(isinstance(s.get('message'), str), 'message must be text')
        if 'timestamp' in s:
            try:
                datetime.fromisoformat(s['timestamp'].replace('Z', '+00:00'))
            except (ValueError, TypeError, AttributeError):
                raise InputError('ATIF validation: invalid timestamp') from None
        if s['source'] != 'agent':
            require(not any(k in s for k in ('tool_calls', 'metrics', 'reasoning_content', 'model_name')), 'agent-only field')
        local = set()
        for c in s.get('tool_calls', []):
            require(set(c) <= {'tool_call_id', 'function_name', 'arguments', 'extra'}, 'unknown tool field')
            cid = c.get('tool_call_id')
            require(isinstance(cid, str) and bool(cid) and cid not in ids, 'invalid or duplicate call ID')
            require(isinstance(c.get('function_name'), str) and isinstance(c.get('arguments'), dict), 'invalid call')
            local.add(cid)
            ids.add(cid)
        for r in s.get('observation', {}).get('results', []):
            require(set(r) <= {'source_call_id', 'content', 'extra'}, 'unknown result field')
            require(r.get('source_call_id') is None or r['source_call_id'] in local, 'unmatched result')
            require(isinstance(r.get('content'), str), 'result must be text')
        for key, value in s.get('metrics', {}).items():
            require(key in {'prompt_tokens', 'completion_tokens', 'cached_tokens', 'extra'}, 'unknown metric')
            require(isinstance(value, dict) if key == 'extra' else type(value) is int and value >= 0, 'invalid metric')


def fence(value, language=''):
    # Untrusted messages cannot close our fence or inject executable HTML.
    value = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', lambda m: f'\\u{ord(m[0]):04x}', value)
    value = value.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    longest = max([len(m[0]) for m in re.finditer(r'`+', value)] or [0])
    mark = '`' * max(3, longest + 1)
    return f'{mark}{language}\n{value}\n{mark}'


def markdown_step(s):
    """Render one normalized step; callers validate their own input envelope."""
    parts = []
    label = {'user': 'User', 'agent': 'Assistant', 'system': 'System'}[s['source']]
    parts.append(f'## {label} · {s["step_id"]}')
    if s.get('timestamp'):
        parts.append(fence(s['timestamp']))
    if s['message']:
        parts.append(fence(s['message']))
    if s.get('extra', {}).get('reasoning'):
        parts.extend(['### Reasoning summary', fence(s['extra']['reasoning'])])
    for c in s.get('tool_calls', []):
        parts.extend(['### Tool call', fence(c['name'] + ' [' + c['id'] + ']'), fence(dumps(c['arguments'], pretty=True), 'json')])
    for r in s.get('observation', {}).get('results', []):
        parts.extend(['### Tool result' + (' (unmatched)' if r.get('extra', {}).get('orphan') else ''),
                      fence(str(r['source_call_id'])), fence(r['content'])])
    return parts


def markdown(doc):
    parts = ['# agentwrench export trajectory', '## Agent', fence(doc['agent']['name']),
             '## Session', fence(doc['session_id'])]
    for s in doc['steps']:
        parts.extend(markdown_step(s))
    parts.extend(['## Export diagnostics', fence(dumps({k: v for k, v in doc['extra'].items()
                 if k in {'warnings', 'redaction', 'view', 'truncated_fields'}}, pretty=True), 'json')])
    return '\n\n'.join(parts) + '\n'


def jsonl(doc):
    header = {k: v for k, v in doc.items() if k != 'steps'}
    return '\n'.join([dumps({'type': 'trajectory', **header})] +
                     [dumps({'type': 'step', **s}) for s in doc['steps']]) + '\n'


def handoff(doc):
    users = [s['message'] for s in doc['steps'] if s['source'] == 'user' and s['message']]
    agents = [s['message'] for s in doc['steps'] if s['source'] == 'agent' and s['message']]
    files, commands = set(), []
    for s in doc['steps']:
        for c in s.get('tool_calls', []):
            name, args = c['name'].lower().split('.')[-1], c['arguments']
            if any(word in name for word in ('patch', 'edit', 'write', 'replace', 'create_file')):
                for key in ('path', 'file_path', 'file', 'filename', 'target_file'):
                    if isinstance(args.get(key), str):
                        files.add(args[key])
                patch = args.get('patch', args.get('raw_input', args.get('input', '')))
                if isinstance(patch, str):
                    for m in re.finditer(r'^(?:\*\*\* (?:Add|Update|Delete) File: |\*\*\* Move to: |\+\+\+ b/|--- a/)(.+)$', patch, re.M):
                        files.add(m[1].strip())
            if name in {'bash', 'shell', 'exec_command', 'execute_command', 'run_command', 'terminal', 'shell_command'}:
                command = args.get('cmd', args.get('command', args.get('raw_input')))
                if isinstance(command, list):
                    command = dumps(command)
                if command is not None:
                    results = [r for r in s.get('observation', {}).get('results', []) if r['source_call_id'] == c['id']]
                    status = next((v for r in results if (v := exit_status(r['content'], r.get('extra', {}))) is not None), None)
                    commands.append({'command': command, 'exit_status': status})
    usage = doc['extra'].get('token_usage')
    if usage is None:
        metrics = [s['metrics'] for s in doc['steps'] if s.get('metrics')]
        usage = {k: sum(m.get(k, 0) for m in metrics) for k in ('prompt_tokens', 'completion_tokens', 'cached_tokens')} if metrics else None
    parts = ['# Hand-off brief', '## First user request', fence(users[0] if users else '(not recorded)'),
             '## Last user request', fence(users[-1] if users else '(not recorded)'),
             '## Last assistant response', fence(agents[-1] if agents else '(not recorded)'),
             '## Files touched (recorded tool arguments)', fence('\n'.join(sorted(files)) or '(none identified)'),
             '## Commands run (recorded calls)', fence(dumps(commands, pretty=True), 'json'),
             '## Token usage', fence(dumps(usage, pretty=True) if usage is not None else '(not recorded)'),
             '## Export diagnostics', fence(dumps({k: doc['extra'][k] for k in ('warnings', 'redaction', 'truncated_fields')}, pretty=True), 'json'),
             'This is an extractive brief. Tool calls describe recorded actions; they do not prove file changes succeeded.']
    return '\n\n'.join(parts) + '\n'
