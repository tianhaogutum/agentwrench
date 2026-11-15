"""Git-compatible unified text diffs without invoking git or any shell."""
from dataclasses import dataclass, field
import difflib
import re

from pathlib import Path

from .engine import MAX_FILE_BYTES, MAX_TOTAL_BYTES, Plan, TextFile, safe_path
from .model import (ApplyError, FilePatch, Hunk, Patch, PatchError,
                    validate_patch_paths, validate_patch_size, validate_path)


def quote(path: str) -> str:
    # Git's C quoted UTF-8 paths; encode non-ASCII as octal bytes.
    raw = path.encode("utf-8")
    return '"' + ''.join(chr(b) if 32 <= b < 127 and b not in (34, 92)
                         else '\\' + chr(b) if b in (34, 92) else f'\\{b:03o}' for b in raw) + '"'


def unquote(value: str, line: int) -> str:
    if not value.startswith('"'):
        if '"' in value or "\t" in value:
            raise PatchError("timestamps and embedded quotes require Git C-quoted paths", line)
        return value
    if not value.endswith('"'):
        raise PatchError("unclosed quoted path", line)
    data = bytearray()
    i = 1
    escapes = {'a': 7, 'b': 8, 't': 9, 'n': 10, 'v': 11, 'f': 12, 'r': 13, '"': 34, '\\': 92}
    while i < len(value)-1:
        c = value[i]
        if c != '\\':
            data.extend(c.encode('utf-8'))
            i += 1
            continue
        i += 1
        if i >= len(value)-1:
            raise PatchError("invalid C path escape", line)
        if value[i] in '01234567':
            octal = value[i:i+3]
            if len(octal) != 3 or any(c not in '01234567' for c in octal) or int(octal, 8) > 255:
                raise PatchError("invalid octal path escape", line)
            data.append(int(octal, 8))
            i += 3
        elif value[i] in escapes:
            data.append(escapes[value[i]])
            i += 1
        else:
            raise PatchError("unsupported C path escape", line)
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise PatchError("paths must be UTF-8", line) from exc


def to_unified(prepared: Plan, context: int = 3) -> str:
    if context < 1 or context > 1000:
        raise PatchError("context must be between 1 and 1000")
    if not prepared.ok:
        raise ApplyError("cannot convert: patch does not apply to root", prepared.diagnostics)
    output = []
    for change in prepared.changes:
        old = change.source
        new = change.destination or old
        a, b = change.before or b"", change.after or b""
        if change.kind == 'update' and old == new and a == b:
            continue
        output.append(f"diff --git {quote('a/'+old)} {quote('b/'+new)}\n")
        if change.kind == 'add':
            output.append("new file mode 100644\n")
        elif change.kind == 'delete':
            output.append(f"deleted file mode {0o100755 if change.mode & 0o111 else 0o100644:o}\n")
        elif old != new:
            if a == b:
                output.append("similarity index 100%\n")
            output.extend([f"rename from {quote(old)}\n", f"rename to {quote(new)}\n"])
        if a == b:
            continue
        lines = difflib.unified_diff(re.findall(r'[^\n]*\n|[^\n]+$', a.decode('utf-8')),
                                     re.findall(r'[^\n]*\n|[^\n]+$', b.decode('utf-8')),
                                     fromfile='/dev/null' if change.before is None else quote('a/'+old),
                                     tofile='/dev/null' if change.after is None else quote('b/'+new), n=context)
        for line in lines:
            if not line.endswith('\n'):
                output.append(line + '\n\\ No newline at end of file\n')
            else:
                output.append(line)
    text = ''.join(output)
    validate_patch_size(text, label="exported unified diff")
    return text


RANGE = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$')


@dataclass
class UnifiedHunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    line: int
    end_line: int
    lines: list[tuple[str, str]]  # Includes exact CRLF/LF endings and BOM.

    @property
    def old_pos(self) -> int:
        return self.old_start - (1 if self.old_count else 0)


@dataclass
class UnifiedFile:
    operation: FilePatch
    old_path: str | None
    new_path: str | None
    line: int
    end_line: int = 0
    hunks: list[UnifiedHunk] = field(default_factory=list)


def _parse_unified(text: str) -> list["UnifiedFile"]:
    """Shared strict syntax/representation parser; retain raw source lines and ranges."""
    if '\x00' in text:
        raise PatchError("binary/NUL input unsupported")
    validate_patch_size(text, label="diff")
    lines = text.split('\n')
    if lines[-1] == '':
        lines.pop()
    result = []
    i = 0
    while i < len(lines):
        start = i
        metadata = {}
        if lines[i].startswith('diff --git '):
            # File headers or rename metadata below are authoritative. Validate the
            # diff header too so misleading/unsafe alternate names cannot hide.
            header = lines[i][11:]
            # Unquoted Git headers may contain spaces: defer exact validation until paths known.
            metadata['diff_header'] = header
            i += 1
            while i < len(lines) and not lines[i].startswith(('--- ', 'diff --git ', '@@')):
                line = lines[i]
                if line.startswith(('new file mode ', 'deleted file mode ')):
                    key = 'new file mode' if line.startswith('new file mode ') else 'deleted file mode'
                    if key in metadata:
                        raise PatchError(f"duplicate git metadata: {key}", i+1)
                    value = line[len(key)+1:]
                    if value not in ('100644', '100755') or (key == 'new file mode' and value != '100644'):
                        raise PatchError("unsupported file mode (V4A cannot express new executable files)", i+1)
                    metadata[key] = value
                elif line.startswith(('rename from ', 'rename to ')):
                    key = 'rename from' if line.startswith('rename from ') else 'rename to'
                    if key in metadata:
                        raise PatchError(f"duplicate git metadata: {key}", i+1)
                    metadata[key] = unquote(line[len(key)+1:], i+1)
                elif re.fullmatch(r'index [0-9a-f]+\.\.[0-9a-f]+(?: 100(?:644|755))?', line):
                    pass
                elif re.fullmatch(r'similarity index \d+%', line):
                    pass
                else:
                    raise PatchError(f"unsupported git metadata: {line!r}", i+1)
                i += 1
        old = new = None
        if i < len(lines) and lines[i].startswith('--- '):
            old = unquote(lines[i][4:].removesuffix('\t'), i+1)
            i += 1
            if i >= len(lines) or not lines[i].startswith('+++ '):
                raise PatchError("expected +++ file header", i+1)
            new = unquote(lines[i][4:].removesuffix('\t'), i+1)
            i += 1
            for name, prefix in ((old, 'a/'), (new, 'b/')):
                if name != '/dev/null' and not name.startswith(prefix):
                    raise PatchError("file headers require a/ and b/ prefixes", i)
            old = None if old == '/dev/null' else old[2:]
            new = None if new == '/dev/null' else new[2:]
        elif 'rename from' in metadata and 'rename to' in metadata:
            old, new = metadata['rename from'], metadata['rename to']
        elif ('new file mode' in metadata) != ('deleted file mode' in metadata):
            # Git represents empty files using mode metadata alone.
            header = metadata['diff_header']
            tokens = re.findall(r'"(?:\\.|[^"\\])*"|\S+', header)
            if header.startswith('a/'):
                pairs = [(header[:j], header[j+1:]) for j in range(len(header))
                         if header[j:j+3] == ' b/' and header[2:j] == header[j+3:]]
                if len(pairs) != 1:
                    raise PatchError("invalid empty-file diff paths", start+1)
                a, b = pairs[0]
            elif len(tokens) == 2:
                a, b = [unquote(token, start+1) for token in tokens]
            else:
                raise PatchError("invalid empty-file quoted paths", start+1)
            if not a.startswith('a/') or not b.startswith('b/') or a[2:] != b[2:]:
                raise PatchError("invalid empty-file diff paths", start+1)
            old = None if 'new file mode' in metadata else a[2:]
            new = b[2:] if 'new file mode' in metadata else None
        else:
            raise PatchError("expected unified ---/+++ headers", i+1)
        if old is None and new is None:
            raise PatchError("both sides cannot be /dev/null", i)
        for path in (old, new):
            if path is not None:
                validate_path(path, i)
        if ('rename from' in metadata) != ('rename to' in metadata):
            raise PatchError("incomplete rename metadata", start+1)
        if 'rename from' in metadata and (metadata['rename from'], metadata['rename to']) != (old, new):
            raise PatchError("rename headers disagree with file headers", start+1)
        if 'diff_header' in metadata:
            a, b = old or new, new or old
            permitted = {f'{quote("a/"+a)} {quote("b/"+b)}', f'a/{a} b/{b}'}
            if metadata['diff_header'] not in permitted:
                raise PatchError("diff --git paths disagree with file headers", start+1)
        if ('new file mode' in metadata and old is not None) or ('deleted file mode' in metadata and new is not None):
            raise PatchError("mode metadata disagrees with file operation", start+1)
        if old and new and old != new and 'rename from' not in metadata:
            raise PatchError("renames require explicit git rename headers", start+1)
        f = FilePatch('add' if old is None else 'delete' if new is None else 'update', old or new,
                      move_to=new if old and new and old != new else None)
        record = UnifiedFile(f, old, new, start+1)
        old_end = new_end = 0
        while i < len(lines) and lines[i].startswith('@@'):
            m = RANGE.fullmatch(lines[i].rstrip('\r'))
            if not m:
                raise PatchError("malformed unified hunk range", i+1)
            # Bound encoded fields, including leading zeroes, before int().
            # Stay below Python's minimum configurable decimal digit guard.
            if any(value is not None and len(value) > 10 for value in m.groups()):
                raise PatchError("unified hunk range fields support at most 10 decimal digits", i+1)
            os_, oc, ns, nc = (int(m[1]), int(m[2] or 1), int(m[3]), int(m[4] or 1))
            old_pos = os_ - (1 if oc else 0)
            new_pos = ns - (1 if nc else 0)
            if old_pos < old_end or new_pos < new_end or old_pos-old_end != new_pos-new_end:
                raise PatchError("inconsistent or overlapping unified hunk coordinates", i+1)
            if (oc and os_ == 0) or (nc and ns == 0):
                raise PatchError("nonempty ranges must start at line 1 or later", i+1)
            old_end, new_end = old_pos+oc, new_pos+nc
            i += 1
            h = Hunk(line=i)
            raw = []
            old_count = new_count = 0
            ending_styles = set()
            while old_count < oc or new_count < nc:
                if i >= len(lines) or not lines[i] or lines[i][0] not in ' +-':
                    raise PatchError("truncated unified hunk", i+1)
                op, value = lines[i][0], lines[i][1:]
                raw.append((op, value + '\n'))
                ending_styles.add(value.endswith('\r'))
                if value.endswith('\r'):
                    if f.kind == 'add':
                        raise PatchError("V4A additions cannot preserve CRLF from unified input", i+1)
                    value = value[:-1]
                h.lines.append((op, value))
                old_count += op != '+'
                new_count += op != '-'
                if old_count > oc or new_count > nc:
                    raise PatchError("unified hunk line counts exceeded", i+1)
                i += 1
            if i < len(lines) and lines[i].startswith('\\'):
                raise PatchError("missing-final-newline diffs cannot be represented in V4A", i+1)
            if f.kind == 'update':
                if len(ending_styles) > 1:
                    raise PatchError("mixed/newline-changing unified hunks cannot be represented safely", h.line)
                if old_pos == 0 and new_pos == 0 and old_count and new_count:
                    old_first = next(j for j, (op, _) in enumerate(h.lines) if op != '+')
                    new_first = next(j for j, (op, _) in enumerate(h.lines) if op != '-')
                    if h.lines[old_first][1].startswith('\ufeff') != h.lines[new_first][1].startswith('\ufeff'):
                        raise PatchError("BOM-changing diffs cannot be represented in V4A", h.line)
                if not any(op != ' ' for op, _ in h.lines):
                    raise PatchError("hunk contains no changes", h.line)
            elif f.kind == 'add':
                if old_count or any(op != '+' for op, _ in h.lines) or old_pos or new_pos:
                    raise PatchError("invalid added-file hunk", h.line)
                f.contents.extend(value for _, value in h.lines)
            elif new_count or any(op != '-' for op, _ in h.lines) or old_pos or new_pos:
                raise PatchError("invalid deleted-file hunk", h.line)
            record.hunks.append(UnifiedHunk(os_, oc, ns, nc, h.line, i+1, raw))
        record.end_line = i+1
        if i < len(lines) and not lines[i].startswith(('diff --git ', '--- ')):
            raise PatchError("unexpected content after unified hunk", i+1)
        if f.kind == 'update' and not record.hunks and not f.move_to:
            raise PatchError("pure rename requires --root to obtain V4A context", start+1)
        result.append(record)
    if not result:
        raise PatchError("empty unified diff")
    validate_patch_paths(Patch([record.operation for record in result]))
    return result


def from_unified(text: str, context: int = 3, *, root: str | Path | None = None) -> Patch:
    """Translate strict unified text into V4A's textual matching representation."""
    if not 1 <= context <= 1000:
        raise PatchError("context must be between 1 and 1000")
    records = _parse_unified(text)
    result = Patch()
    total_source_bytes = 0
    for record in records:
        f = record.operation
        hunks = []
        for raw in record.hunks:
            h = Hunk(line=raw.line, lines=[(op, value[:-1].removesuffix('\r'))
                                          for op, value in raw.lines])
            if f.kind == 'update':
                if not raw.old_count:
                    raise PatchError("zero-context insertion unsupported; regenerate with git diff -U3", h.line)
                if raw.old_start == raw.new_start == 1 and raw.new_count:
                    old_first = next(j for j, (op, _) in enumerate(h.lines) if op != '+')
                    new_first = next(j for j, (op, _) in enumerate(h.lines) if op != '-')
                    for j in {old_first, new_first}:
                        op, value = h.lines[j]
                        h.lines[j] = (op, value.removeprefix('\ufeff'))
                first_change = next(j for j, (op, _) in enumerate(h.lines) if op != ' ')
                last_change = max(j for j, (op, _) in enumerate(h.lines) if op != ' ')
                h.lines = h.lines[max(0, first_change-context):last_change+context+1]
                hunks.append(h)
        start = record.line-1
        if f.kind == 'update' and not hunks:
            if not f.move_to or root is None:
                raise PatchError("pure rename requires --root to obtain V4A context", start+1)
            base = Path(root).resolve()
            source = safe_path(base, f.path)
            if not source.is_file():
                raise PatchError("pure rename requires an existing source of at most 8 MiB", start+1)
            source_stat = source.stat()
            if source_stat.st_size > MAX_FILE_BYTES:
                raise PatchError("pure rename requires an existing source of at most 8 MiB", start+1)
            remaining = MAX_TOTAL_BYTES - total_source_bytes
            if source_stat.st_size > remaining:
                raise PatchError("pure rename exceeds aggregate 64 MiB source byte budget", start+1)
            # Stat is only an early refusal: cap the actual read too, with one
            # sentinel byte to detect growth before decoding/building context.
            with source.open('rb') as handle:
                data = handle.read(min(MAX_FILE_BYTES, remaining) + 1)
            if len(data) > MAX_FILE_BYTES:
                raise PatchError("pure rename source exceeds 8 MiB byte budget", start+1)
            if len(data) > remaining:
                raise PatchError("pure rename exceeds aggregate 64 MiB source byte budget", start+1)
            total_source_bytes += len(data)
            source_text = TextFile.read(data, f.path)
            if not source_text.lines:
                if data or source_stat.st_mode & 0o111:
                    raise PatchError("empty BOM/executable rename cannot be represented in V4A", start+1)
                result.files.extend([FilePatch('delete', f.path), FilePatch('add', f.move_to)])
                continue
            if not source_text.endings[-1]:
                raise PatchError("unterminated pure rename cannot be represented in V4A", start+1)
            hunks = [Hunk(lines=[(' ', s) for s in source_text.lines])]
        if f.kind == 'update':
            f.hunks = hunks
        result.files.append(f)
    if not result.files:
        raise PatchError("empty unified diff")
    validate_patch_paths(result)
    # Small pure-rename diffs expand to full source context. Include canonical
    # envelope/headers/prefixes so every returned patch fits parse()'s budget.
    validate_patch_size(result.to_text(), label="converted V4A patch")
    return result


def unified_check_report(files: list[dict], diagnostics: list[dict]) -> dict:
    return {'format': 'v4a-unified-check', 'version': 1,
            'ok': not diagnostics and all(f['ok'] for f in files),
            'files': files, 'diagnostics': diagnostics}


def check_unified(text: str, root: str | Path) -> dict:
    """Check exact old-side locations, without searching, converting or writing.

    Ranges refer to the original LF-delimited source (CRLF/BOM are significant).
    An empty -N,0 range is the gap after N lines, including BOF and EOF.
    Invalid syntax, unsafe paths and resource limits raise PatchError; ordinary
    source conflicts and per-file I/O failures are returned in the v1 report.
    """
    records = _parse_unified(text)
    base = Path(root).resolve()
    if not base.is_dir():
        return unified_check_report([], [{'code': 'ROOT_UNAVAILABLE', 'patch_line': None}])
    # Validate every touched path before reading sources, including destinations.
    paths = {}
    for record in records:
        f = record.operation
        for relative in ([f.path, f.move_to] if f.move_to else [f.path]):
            try:
                paths[relative] = safe_path(base, relative)
            except ApplyError as exc:
                raise PatchError('unsafe non-directory parent in unified path') from exc
    files = []
    total = 0
    for number, record in enumerate(records, 1):
        f = record.operation
        source = paths[f.path]
        source_lines = []
        problem = None
        try:
            exists = source.exists()
            if f.kind == 'add':
                if exists:
                    problem = 'ADD_TARGET_EXISTS'
            elif not exists:
                problem = 'SOURCE_MISSING'
            else:
                with source.open('rb') as handle:
                    data = handle.read(min(MAX_FILE_BYTES, MAX_TOTAL_BYTES-total)+1)
                if len(data) > MAX_FILE_BYTES or total+len(data) > MAX_TOTAL_BYTES:
                    raise PatchError('unified source exceeds per-file or total byte budget')
                total += len(data)
                TextFile.read(data, f.path)  # Shared encoding/NUL/logical-line limits.
                source_lines = re.findall(r'[^\n]*\n|[^\n]+$', data.decode('utf-8'))
        except OSError:
            problem = 'IO_ERROR'
        hunks = []
        for hunk_number, h in enumerate(record.hunks, 1):
            expected = [value for op, value in h.lines if op != '+']
            pos = h.old_pos
            code = problem
            mismatch = None
            if code is None:
                if pos > len(source_lines) or pos+h.old_count > len(source_lines):
                    code = 'LOCATION_OUT_OF_RANGE'
                else:
                    mismatch = next((pos+j+1 for j, value in enumerate(expected)
                                     if source_lines[pos+j] != value), None)
                    code = 'LOCATION_MISMATCH' if mismatch is not None else 'HUNK_MATCHED'
            hunks.append({'hunk': hunk_number, 'code': code,
                          'status': 'matched' if code == 'HUNK_MATCHED' else 'failed',
                          'old_range': {'start': h.old_start, 'count': h.old_count},
                          'new_range': {'start': h.new_start, 'count': h.new_count},
                          'patch_span': {'start_line': h.line, 'end_line': h.end_line},
                          'source_span': {'start_line': pos+1, 'end_line': pos+h.old_count+1},
                          'mismatch_line': mismatch})
        failure = problem or next((h['code'] for h in hunks if h['status'] == 'failed'), None)
        files.append({'file': number, 'kind': f.kind, 'path': f.path,
                      'old_path': record.old_path, 'new_path': record.new_path,
                      'ok': failure is None,
                      'code': failure or ('LOCATIONS_MATCHED' if hunks else
                                         'SOURCE_ABSENT' if f.kind == 'add' else 'SOURCE_PRESENT'),
                      'patch_span': {'start_line': record.line, 'end_line': record.end_line},
                      'hunks': hunks})
    return unified_check_report(files, [])
