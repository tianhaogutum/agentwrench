"""Strict merged-envelope validation and inert Markdown projection, never raw import."""
from contextlib import contextmanager
import os
import stat

from .adapters import MAX_FILE_BYTES, MAX_LINE_BYTES, MAX_RECORDS
from .export import fence, markdown_step, project_step
from .merge import MAX_SOURCES, timestamp_instant
from .model import InputError, dumps, loads


class _Reader:
    def __init__(self, stream, command='render-merge'):
        self.stream = stream
        self.command = command
        self.line = self.bytes = self.records = 0

    def fail(self, reason):
        raise InputError(f'{self.command} input line {self.line}: {reason}')

    def require(self, condition, reason):
        if not condition:
            self.fail(reason)

    def record(self):
        while True:
            raw = self.stream.readline(MAX_LINE_BYTES + 1)
            if not raw:
                return None
            self.line += 1
            self.bytes += len(raw)
            self.require(self.bytes <= MAX_FILE_BYTES, 'file byte limit exceeded')
            self.require(len(raw) <= MAX_LINE_BYTES, 'line byte limit exceeded')
            try:
                text = raw.decode('utf-8-sig' if self.line == 1 else 'utf-8')
                if not text.strip(' \t\r\n'):
                    continue
                row = loads(text)
            except (ValueError, RecursionError):
                self.fail('invalid UTF-8 or JSON record')
            self.records += 1
            self.require(self.records <= MAX_RECORDS, 'record limit exceeded')
            self.require(isinstance(row, dict), 'record must be an object')
            return row

    def sources(self):
        h = self.record()
        self.require(h is not None and h.get('type') == 'merge'
                     and h.get('schema_version') == 'trajport/merge-v1',
                     'expected trajport/merge-v1 header')
        sources = h.get('sources')
        self.require(isinstance(sources, list) and 2 <= len(sources) <= MAX_SOURCES,
                     'expected 2–64 source headers')
        result = {}
        for source in sources:
            self.require(isinstance(source, dict), 'invalid source header')
            number, trajectory = source.get('input'), source.get('trajectory')
            self.require(type(number) is int and 1 <= number <= len(sources)
                         and number not in result, 'invalid or duplicate source ordinal')
            self.require(isinstance(trajectory, dict), 'invalid trajectory header')
            self.require(trajectory.get('type') == 'trajectory'
                         and trajectory.get('schema_version') == 'trajport/v1'
                         and isinstance(trajectory.get('session_id'), str)
                         and trajectory['session_id'] not in ('', 'unknown')
                         and isinstance(trajectory.get('agent'), dict)
                         and all(isinstance(trajectory['agent'].get(k), str) for k in ('name', 'version'))
                         and isinstance(trajectory.get('extra', {}), dict),
                         'expected normalized trajectory header with known session ID and agent')
            result[number] = trajectory
        return result

    def step(self, s):
        self.require(isinstance(s, dict), 'expected normalized step')
        self.require(s.get('type') == 'step' and type(s.get('step_id')) is int
                     and s['step_id'] > 0 and s.get('source') in ('user', 'agent', 'system')
                     and isinstance(s.get('message'), str),
                     'expected step with positive integer ID, source and text message')
        try:
            instant = timestamp_instant(s.get('timestamp'))
        except ValueError:
            self.fail('timestamp requires RFC3339 with timezone and at most 6 fractional digits')
        extra = s.get('extra', {})
        self.require(isinstance(extra, dict), 'step extra must be an object')
        self.require('reasoning' not in extra or isinstance(extra['reasoning'], str),
                     'reasoning summary must be text')
        calls = s.get('tool_calls', [])
        self.require(isinstance(calls, list), 'tool calls must be a list')
        ids = set()
        for call in calls:
            self.require(isinstance(call, dict), 'invalid tool call')
            cid = call.get('id')
            self.require(isinstance(cid, str) and bool(cid) and cid not in ids
                         and isinstance(call.get('name'), str) and bool(call['name'])
                         and isinstance(call.get('arguments'), dict)
                         and isinstance(call.get('extra', {}), dict), 'invalid or duplicate tool call')
            ids.add(cid)
        observation = s.get('observation', {})
        self.require(isinstance(observation, dict), 'observation must be an object')
        results = observation.get('results', [])
        self.require(isinstance(results, list), 'tool results must be a list')
        for result in results:
            self.require(isinstance(result, dict), 'invalid tool result')
            self.require('source_call_id' in result
                         and (result['source_call_id'] is None or isinstance(result['source_call_id'], str))
                         and isinstance(result.get('content'), str)
                         and isinstance(result.get('extra', {}), dict), 'invalid tool result')
            extra = result.get('extra', {})
            self.require('orphan' not in extra or type(extra['orphan']) is bool, 'invalid orphan flag')
        self.require(isinstance(s.get('metrics', {}), dict), 'metrics must be an object')
        return instant

    def events(self, sources):
        previous, identities, locations = None, set(), set()
        while (event := self.record()) is not None:
            self.require(event.get('type') == 'event', 'expected event envelope')
            p = event.get('provenance')
            self.require(isinstance(p, dict), 'expected event provenance')
            number, line = p.get('input'), p.get('line')
            self.require(type(number) is int and number in sources,
                         'provenance references unknown source')
            self.require(type(line) is int and line > 1, 'invalid provenance line')
            s = event.get('step')
            instant = self.step(s)
            session = sources[number]['session_id']
            self.require(p.get('session_id') == session
                         and type(p.get('step_id')) is int and p['step_id'] == s['step_id'],
                         'provenance does not match source session and step ID')
            identity, location = (session, s['step_id']), (number, line)
            self.require(identity not in identities, 'duplicate event identity')
            self.require(location not in locations, 'duplicate source location')
            self.require(previous is None or instant >= previous, 'timestamps are not nondecreasing')
            identities.add(identity)
            locations.add(location)
            previous = instant
            yield s, p


@contextmanager
def _merged_input(path, command):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        reader = _Reader(source, command)
        info = os.fstat(source.fileno())
        reader.require(stat.S_ISREG(info.st_mode), 'input must be a regular file')
        reader.require(info.st_size <= MAX_FILE_BYTES, 'file byte limit exceeded')
        yield reader


def validate_merge(path):
    """Count one complete merged export using the renderer's strict subset.

    No projection, staging, temporary index or original sources. Counts describe
    only this export; retained metadata and provenance authenticity are not
    exhaustively validated. The same input bounds and identity/location sets
    apply as for rendering. Header-only exports are valid.
    """
    with _merged_input(path, 'validate-merge') as reader:
        sources = reader.sources()
        count = sum(1 for _ in reader.events(sources))
        return {'validated_events': count, 'sources': len(sources)}


def timestamp_window(since, until):
    """Validate optional endpoint strings before any input/output/staging I/O."""
    bounds = []
    for name, value in (('since', since), ('until', until)):
        try:
            bounds.append(None if value is None else timestamp_instant(value))
        except ValueError:
            raise InputError(f'--{name} requires RFC3339 with timezone and at most 6 fractional digits') from None
    lower, upper = bounds
    if lower is not None and upper is not None and lower > upper:
        raise InputError('--since must be before or equal to --until')
    return lower, upper


def render_merge(path, stream, *, max_output_bytes=None, selected_sources=None,
                 since=None, until=None, view='full'):
    """Write validated fragments to a private output/stage; caller owns publication.

    This one bounded input uses an in-memory identity/location set. It neither
    loads raw sessions nor renumbers, redacts, truncates, sorts or joins events.
    selected_sources is an optional nonempty sequence of positive ordinals;
    selection changes presentation only, after full header/event validation.
    since/until are optional timestamp strings: inclusive lower/exclusive upper
    UTC instants, intersected with source selection. Omitted bounds are unbounded.
    view uses normalized prepare retention rules without renumbering. A nonfull
    view may be empty; hidden fields still validate before projection.
    """
    if view not in ('conversation', 'tools', 'full'):
        raise InputError('invalid view')
    lower, upper = timestamp_window(since, until)
    timed = lower is not None or upper is not None
    if max_output_bytes is not None and (type(max_output_bytes) is not int or max_output_bytes <= 0):
        raise InputError('max output bytes must be a positive integer')
    if selected_sources is not None:
        if not selected_sources or any(type(n) is not int or n <= 0 for n in selected_sources):
            raise InputError('source ordinal must be a positive integer')
        selected_sources = sorted(set(selected_sources))
    used = 0

    def write(parts):
        nonlocal used
        text = '\n\n'.join(parts) + '\n\n'
        size = len(text.encode('utf-8'))
        if max_output_bytes is not None and used + size > max_output_bytes:
            raise InputError('render-merge output byte limit exceeded')
        used += size
        stream.write(text)

    with _merged_input(path, 'render-merge') as reader:
        sources = reader.sources()
        if selected_sources is not None:
            reader.require(all(n in sources for n in selected_sources),
                           'selected source ordinal is not declared')
        write(['# agentwrench export merged trajectory', '## Sources', fence(dumps([
            {'input': number, 'session_id': h['session_id'], 'agent': h['agent']}
            for number, h in sources.items()], pretty=True), 'json')])
        if selected_sources is not None:
            write(['## Source selection', fence(dumps({'selected_sources': selected_sources}, pretty=True), 'json'),
                   'Only events from these declared input ordinals are shown; all events are validated.'])
        if timed:
            context = ['## Timestamp selection', fence(dumps({'since': since, 'until': until}, pretty=True), 'json'),
                       'Since is inclusive; until is exclusive. A null endpoint is unbounded. '
                       'Time and source selections are combined; all events are validated.']
            if selected_sources is None:
                context.append('All declared sources are selected.')
            write(context)
        if view != 'full':
            semantics = ('Keeps nonempty user/assistant messages only; hides reasoning, tool activity and step metadata.'
                         if view == 'conversation' else
                         'Keeps steps with tool calls or nonempty observations from any role; hides messages, reasoning and step metadata.')
            context = ['## View', fence(dumps({'view': view}, pretty=True), 'json'), semantics,
                       'View and source/time selections are combined; all events are validated.']
            if selected_sources is None:
                context.append('All declared sources are selected.')
            if not timed:
                context.append('No timestamp bounds are active.')
            write(context)
        count = excluded = 0
        for s, p in reader.events(sources):
            # Consume every validated event, including the excluded tail.
            instant = timestamp_instant(s['timestamp']) if timed else None
            if ((selected_sources is not None and p['input'] not in selected_sources)
                    or (lower is not None and instant < lower)
                    or (upper is not None and instant >= upper)):
                excluded += 1
                continue
            s = project_step(s, view)
            if s is None:
                excluded += 1
                continue
            parts = markdown_step(s)
            # Only fixed labels and validated integers occur outside fences.
            label = {key: p[key] for key in ('input', 'line', 'session_id', 'step_id')}
            write([parts[0], '### Source / session', fence(dumps(label, pretty=True), 'json'), *parts[1:]])
            count += 1
        if not count:
            write(['No events matching the selected view and source/time selection recorded in this merged export.'
                   if view != 'full' else
                   'No events matching the active selection recorded in this merged export.' if timed else
                   'No events recorded in this merged export.' if selected_sources is None else
                   'No events from the selected sources recorded in this merged export.'])
        diagnostics = {'rendered_events': count, 'sources': len(sources)}
        if selected_sources is not None:
            diagnostics.update(selected_sources=selected_sources, excluded_events=excluded)
        if timed:
            diagnostics.update(since=since, until=until, excluded_events=excluded)
        if view != 'full':
            diagnostics.update(view=view, excluded_events=excluded)
        # Finish with exactly one LF, including it in the output-byte bound.
        tail = '## Render diagnostics\n\n' + fence(dumps(diagnostics, pretty=True), 'json') + '\n'
        if max_output_bytes is not None and used + len(tail.encode('utf-8')) > max_output_bytes:
            raise InputError('render-merge output byte limit exceeded')
        stream.write(tail)
        return diagnostics
