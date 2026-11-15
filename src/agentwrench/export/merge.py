"""Streaming merge of normalized exports; events remain scoped to their sessions."""
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import heapq
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile

from .adapters import MAX_FILE_BYTES, MAX_LINE_BYTES, MAX_RECORDS
from .model import InputError, dumps, loads

MAX_SOURCES = 64
DEFAULT_SORT_BUFFER_BYTES = 8 * 1024 * 1024
MAX_SORT_BUFFER_BYTES = 64 * 1024 * 1024
SORT_FAN_IN = 32
# Explicit precision avoids silently rounding distinct instants into a tie.
INSTANT = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})', re.ASCII)


def timestamp_instant(value):
    """Parse the merge timestamp syntax to UTC; invalid values raise ValueError."""
    if not isinstance(value, str) or not INSTANT.fullmatch(value):
        raise ValueError
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)
    except OverflowError:
        raise ValueError from None


class _Source:
    def __init__(self, stream, number):
        self.stream, self.number = stream, number
        self.line = self.bytes = self.records = 0
        self.previous = None
        self.header = self.record()
        h = self.header
        if (not h or h.get('type') != 'trajectory' or h.get('schema_version') != 'trajport/v1'
                or not isinstance(h.get('session_id'), str) or not h['session_id']
                or h['session_id'] == 'unknown'
                or not isinstance(h.get('agent'), dict)
                or not all(isinstance(h['agent'].get(k), str) for k in ('name', 'version'))
                or not isinstance(h.get('extra', {}), dict)):
            self.fail('expected trajport/v1 header with a known session ID and agent')

    def fail(self, reason):
        raise InputError(f'merge input {self.number} line {self.line}: {reason}')

    def record(self):
        while True:
            raw = self.stream.readline(MAX_LINE_BYTES + 1)
            if not raw:
                return None
            self.line += 1
            self.bytes += len(raw)
            if self.bytes > MAX_FILE_BYTES:
                self.fail('file byte limit exceeded')
            if len(raw) > MAX_LINE_BYTES:
                self.fail('line byte limit exceeded')
            try:
                text = raw.decode('utf-8-sig' if self.line == 1 else 'utf-8')
                if not text.strip(' \t\r\n'):
                    continue
                row = loads(text)
            except (ValueError, RecursionError):
                self.fail('invalid UTF-8 or JSON record')
            self.records += 1
            if self.records > MAX_RECORDS:
                self.fail('record limit exceeded')
            if not isinstance(row, dict):
                self.fail('record must be an object')
            return row

    def event(self, *, allow_unsorted=False):
        row = self.record()
        if row is None:
            if self.previous is None:
                self.fail('trajectory has no steps')
            return None
        if (row.get('type') != 'step' or type(row.get('step_id')) is not int
                or row['step_id'] <= 0 or row.get('source') not in ('user', 'agent', 'system')
                or not isinstance(row.get('message'), str)):
            self.fail('expected step with positive integer ID, source and text message')
        ts = row.get('timestamp')
        try:
            instant = timestamp_instant(ts)
        except ValueError:
            self.fail('timestamp requires RFC3339 with timezone and at most 6 fractional digits')
        if not allow_unsorted and self.previous is not None and instant < self.previous:
            self.fail('timestamps are not nondecreasing')
        self.previous = instant
        event = {'type': 'event', 'step': row, 'provenance': {
            'input': self.number, 'line': self.line, 'session_id': self.header['session_id'],
            'step_id': row['step_id']}}
        return instant, self.number, event


def _ordered_events(sources, heap):
    while heap:
        _, number, event = heapq.heappop(heap)
        yield event
        following = sources[number - 1].event()
        if following is not None:
            heapq.heappush(heap, following)


def _run_path(directory, generation, number):
    return Path(directory) / f'run-{generation}-{number}.jsonl'


def _private_run(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb')


def _read_runs(paths):
    """Merge at most SORT_FAN_IN trusted temporary runs, one event per run."""
    with ExitStack() as stack:
        streams = [stack.enter_context(path.open('rb')) for path in paths]
        heap = []

        def advance(number):
            raw = streams[number].readline()
            if raw:
                event = loads(raw)
                p = event['provenance']
                instant = datetime.fromisoformat(event['step']['timestamp'].replace('Z', '+00:00'))
                heapq.heappush(heap, (instant, p['input'], p['line'], number, event))

        for number in range(len(streams)):
            advance(number)
        while heap:
            _, _, _, number, event = heapq.heappop(heap)
            yield event
            advance(number)


class _RunQuota:
    """Count logical bytes in live runs, including buffered replacement writes."""
    def __init__(self, limit):
        self.limit = limit
        self.used = 0

    def write(self, stream, raw):
        if self.limit is not None and self.used + len(raw) > self.limit:
            raise InputError('sort temporary-run byte quota exceeded')
        stream.write(raw)
        self.used += len(raw)

    def remove(self, path):
        # Runs are closed before removal; release bytes only after unlinking.
        size = path.stat().st_size
        path.unlink()
        self.used -= size


def _external_events(sources, directory, budget, max_temp_bytes):
    # Store encoded envelopes, not a second collection of decoded step objects.
    # A single large event may exceed the budget and gets its own run. At most
    # one newly read event exists alongside the current buffer before spilling.
    chunk, used, count = [], 0, 0
    quota = _RunQuota(max_temp_bytes)

    def spill():
        nonlocal used, count
        chunk.sort(key=lambda entry: entry[:3])
        with _private_run(_run_path(directory, 0, count)) as stream:
            for _, _, _, raw in chunk:
                quota.write(stream, raw)
        count += 1
        chunk.clear()
        used = 0

    for source in sources:
        while (item := source.event(allow_unsorted=True)) is not None:
            instant, number, event = item
            raw = (dumps(event) + '\n').encode('utf-8')
            if chunk and used + len(raw) > budget:
                spill()
            chunk.append((instant, number, event['provenance']['line'], raw))
            used += len(raw)
            if used >= budget:
                spill()
    if chunk:
        spill()

    # Numbered generations avoid an event-count-sized list of run paths. Each
    # pass opens only a fixed-size group and deletes that group after merging.
    generation = 0
    while count > SORT_FAN_IN:
        following_count = 0
        for start in range(0, count, SORT_FAN_IN):
            paths = [_run_path(directory, generation, n)
                     for n in range(start, min(start + SORT_FAN_IN, count))]
            with _private_run(_run_path(directory, generation + 1, following_count)) as stream:
                with closing(_read_runs(paths)) as events:
                    for event in events:
                        quota.write(stream, (dumps(event) + '\n').encode('utf-8'))
            for path in paths:
                quota.remove(path)
            following_count += 1
        generation += 1
        count = following_count
    yield from _read_runs([_run_path(directory, generation, n) for n in range(count)])


def merge_jsonl(paths, *, external_sort=False, sort_buffer_bytes=None, max_sort_temp_bytes=None,
                sort_temp_dir=None):
    """Yield a merge header and events with disk-backed identity tracking.

    Inputs must be ordered unless external_sort=True. External sorting uses
    bounded encoded-byte runs (default 8 MiB, one oversized event allowed) and
    at most 32 pending run events during repeated disk merge passes.
    Optional max_sort_temp_bytes is a positive integer limiting live run bytes,
    including old/new pass overlap, excluding the identity index and output.
    Optional sort_temp_dir selects an existing writable root for a private child
    holding the identity index and, with external_sort, runs. None keeps the
    platform temporary directory. Only the child is removed on close/error.
    This iterator may raise after yielding; callers must stage output if they
    need failures to leave no partial results (as the CLI does).
    """
    paths = list(paths)
    if not 2 <= len(paths) <= MAX_SOURCES:
        raise InputError(f'merge requires 2 to {MAX_SOURCES} input files')
    if sort_buffer_bytes is not None and not external_sort:
        raise InputError('sort buffer requires --external-sort')
    budget = DEFAULT_SORT_BUFFER_BYTES if sort_buffer_bytes is None else sort_buffer_bytes
    if type(budget) is not int or not 1 <= budget <= MAX_SORT_BUFFER_BYTES:
        raise InputError(f'sort buffer bytes must be between 1 and {MAX_SORT_BUFFER_BYTES}')
    if max_sort_temp_bytes is not None:
        if not external_sort:
            raise InputError('sort temporary-run quota requires --external-sort')
        if type(max_sort_temp_bytes) is not int or max_sort_temp_bytes <= 0:
            raise InputError('max sort temp bytes must be a positive integer')
    root_error = 'sort temporary directory must be an existing writable directory'
    if sort_temp_dir is not None:
        try:
            sort_temp_dir = os.fspath(sort_temp_dir)
        except TypeError:
            raise InputError(root_error) from None
        if not isinstance(sort_temp_dir, str) or not sort_temp_dir or '\0' in sort_temp_dir:
            raise InputError(root_error)
    with ExitStack() as stack:
        sources = []
        for number, path in enumerate(paths, 1):
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            stream = stack.enter_context(os.fdopen(fd, 'rb'))
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise InputError(f'merge input {number}: input must be a regular file')
            if info.st_size > MAX_FILE_BYTES:
                raise InputError(f'merge input {number}: file byte limit exceeded')
            sources.append(_Source(stream, number))
        if sort_temp_dir is None:
            temporary = tempfile.TemporaryDirectory(prefix='trajport-merge-')
        else:
            # Creation checks actual usability, without an access-check race or
            # tempfile's default-root fallback. Never create/remove the root.
            try:
                temporary = tempfile.TemporaryDirectory(prefix='trajport-merge-', dir=sort_temp_dir)
            except (OSError, ValueError):
                raise InputError(root_error) from None
        directory = stack.enter_context(temporary)
        index = Path(directory) / 'identity.sqlite'
        os.close(os.open(index, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        db = sqlite3.connect(index)
        stack.callback(db.close)
        db.execute('PRAGMA cache_size = -1024')
        db.execute('PRAGMA journal_mode = OFF')
        db.execute('CREATE TABLE identities (session TEXT, step TEXT, input INTEGER, line INTEGER, '
                   'PRIMARY KEY (session, step)) WITHOUT ROWID')
        if external_sort:
            events = _external_events(sources, directory, budget, max_sort_temp_bytes)
        else:
            heap = [source.event() for source in sources]
            heapq.heapify(heap)
            events = _ordered_events(sources, heap)
        yield {'type': 'merge', 'schema_version': 'trajport/merge-v1',
               'sources': [{'input': source.number, 'trajectory': source.header} for source in sources]}
        with closing(events):
            for event in events:
                p = event['provenance']
                key = (p['session_id'], str(p['step_id']))
                previous = db.execute('SELECT input, line FROM identities WHERE session=? AND step=?', key).fetchone()
                if previous:
                    raise InputError('duplicate event identity: '
                                     f'input {previous[0]} line {previous[1]} conflicts with '
                                     f'input {p["input"]} line {p["line"]}')
                db.execute('INSERT INTO identities VALUES (?, ?, ?, ?)', (*key, p['input'], p['line']))
                yield event
