"""Normalized trajectory builder. No input is ever evaluated or executed."""
from datetime import datetime, timezone
import json
import math
import re


class InputError(ValueError):
    """A safe-to-display input error; never include input values in messages."""


def dumps(value, *, pretty=False):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      indent=2 if pretty else None, allow_nan=False)


def loads(text):
    """Strict JSON: reject duplicate fields and nonfinite numeric values."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON field')
            result[key] = value
        return result

    def number(text):
        value = float(text)
        if not math.isfinite(value):
            raise ValueError('nonfinite JSON number')
        return value

    def constant(_):
        raise ValueError('nonfinite JSON constant')

    return json.loads(text, object_pairs_hook=pairs, parse_float=number, parse_constant=constant)


def timestamp(value):
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat().replace('+00:00', 'Z')
        if isinstance(value, str):
            datetime.fromisoformat(value.replace('Z', '+00:00'))
            return value
    except (ValueError, OverflowError, OSError):
        pass
    return None


def media_placeholder(block):
    source = block.get('source') or block.get('image_url') or {}
    if isinstance(source, str):
        source = {'url': source}
    if not isinstance(source, dict):
        source = {}
    data = source.get('data', '')
    url = source.get('url', block.get('image_url', ''))
    mime = source.get('media_type', block.get('mime_type', block.get('media_type', block.get('type', 'binary'))))
    if isinstance(url, str) and url.startswith('data:'):
        header, _, data = url.partition(',')
        mime = header[5:].split(';')[0]
    if isinstance(data, str) and data:
        # Encoded length only: never decode untrusted media or access URLs/files.
        return f'[media: {mime}; encoded_chars={len(data)}; omitted]'
    return f'[media: {mime}; size=unknown; omitted]'


def safe_data(value):
    """Remove binary/encrypted payloads even when buried in unknown records."""
    if isinstance(value, dict):
        if value.get('type') == 'redacted_thinking':
            return '[encrypted reasoning omitted]'
        if value.get('type') in {'image', 'input_image', 'image_url', 'audio', 'input_audio', 'document', 'binary'}:
            return media_placeholder(value)
        return {k: ('[encrypted content omitted]' if k == 'encrypted_content' else safe_data(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [safe_data(x) for x in value]
    return value


class Builder:
    def __init__(self, fmt):
        self.doc = {'schema_version': 'trajport/v1', 'session_id': 'unknown',
                    'agent': {'name': fmt, 'version': 'unknown'}, 'steps': [],
                    'extra': {'warnings': {}, 'records': []}}
        self.calls = {}

    def warn(self, code):
        counts = self.doc['extra']['warnings']
        counts[code] = counts.get(code, 0) + 1

    def record(self, raw, code=None):
        self.doc['extra']['records'].append(safe_data(raw))
        if code:
            self.warn(code)

    def step(self, source, message='', ts=None, extra=None):
        step = {'step_id': len(self.doc['steps']) + 1, 'source': source,
                'message': message, 'extra': safe_data(extra or {})}
        if ts is not None:
            clean = timestamp(ts)
            if clean:
                step['timestamp'] = clean
            else:
                step['extra']['invalid_timestamp'] = safe_data(ts)
                self.warn('invalid_timestamp')
        self.doc['steps'].append(step)
        return step

    def call(self, step, name, arguments, call_id=None):
        if not isinstance(name, str) or not name:
            raise InputError('tool call requires a nonempty name')
        original_id = call_id
        if not isinstance(call_id, str) or not call_id:
            call_id = f'generated-{step["step_id"]}-{len(step.get("tool_calls", [])) + 1}'
            self.warn('missing_call_id')
        if call_id in self.calls:
            # Ambiguous references cannot be safely matched to one invocation.
            raise InputError('duplicate tool call ID in included session')
        if isinstance(arguments, str):
            try:
                parsed = loads(arguments)
            except (ValueError, RecursionError):
                parsed = None
            arguments = parsed if isinstance(parsed, dict) else {'raw_input': arguments}
        elif not isinstance(arguments, dict):
            arguments = {'raw_input': arguments}
        call = {'id': call_id, 'name': name, 'arguments': safe_data(arguments)}
        if original_id is None:
            call['extra'] = {'generated_id': True}
        step.setdefault('tool_calls', []).append(call)
        self.calls[call_id] = step
        return call_id

    def result(self, call_id, content, ts=None, extra=None):
        step = self.calls.get(call_id) if isinstance(call_id, str) else None
        result = {'source_call_id': call_id if isinstance(call_id, str) else None,
                  'content': content, 'extra': safe_data(extra or {})}
        if ts is not None:
            result['extra']['timestamp'] = safe_data(ts)
        if step is None:
            step = self.step('system', ts=ts)
            result['extra']['orphan'] = True
            self.warn('orphan_tool_result')
        step.setdefault('observation', {'results': []})['results'].append(result)

    def metrics(self, step, usage, *, anthropic=False):
        if not isinstance(usage, dict):
            raise InputError('usage must be an object')
        if anthropic:
            keys = ['input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens']
            prompt = sum(v for k in keys if isinstance((v := usage.get(k)), int) and not isinstance(v, bool) and v >= 0)
            mapped = {'prompt_tokens': prompt} if any(k in usage for k in keys) else {}
            names = {'output_tokens': 'completion_tokens', 'cache_read_input_tokens': 'cached_tokens'}
        else:
            mapped = {}
            names = {'input_tokens': 'prompt_tokens', 'output_tokens': 'completion_tokens',
                     'cached_input_tokens': 'cached_tokens'}
        for src, dst in names.items():
            v = usage.get(src)
            if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                mapped[dst] = v
        mapped['extra'] = {'raw_usage': safe_data(usage)}
        step['metrics'] = mapped

    def finish(self):
        if not self.doc['steps']:
            raise InputError('no supported conversation or tool events in session')
        return self.doc


def exit_status(content, extra=None):
    extra = extra or {}
    for key in ('exit_code', 'exitCode', 'exit_status'):
        if isinstance(extra.get(key), int):
            return extra[key]
    match = re.search(r'(?:Process exited with code|exit(?:[ _]code| status)?[:=]?)\s*(-?\d+)', content, re.I)
    return int(match[1]) if match else None
