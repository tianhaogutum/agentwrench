"""Portable, versioned Codex discovery snapshots and historical CI comparisons."""
from __future__ import annotations

import hashlib
import json
import re
import stat
from dataclasses import asdict
from pathlib import Path

from .core import (MAX_FILE_BYTES, MAX_FILES, MAX_TOTAL_BYTES, SEPARATOR, Reader, ScopeError, Settings,
                   UPSTREAM_SHA, absolute, directory, filename, inspect_codex)
from .impact import MAX_WORKDIRS, _changes, normalize_cwds
from .relocation import project_manifest

FORMAT = 'agentsmd-scope.snapshot'
MANIFEST_FORMAT = 'agentsmd-scope.manifest'
STATUSES = {'loaded', 'partial', 'skipped-empty', 'dropped-over-budget',
            'shadowed-by-override', 'shadowed-by-precedence'}


def create_snapshot(cwd: Path, home: Path, settings: Settings,
                    warnings: list[str] | None = None, *, snapshot_version: int = 1) -> dict:
    _require(type(snapshot_version) is int and snapshot_version in (1, 2))
    home = absolute(home)
    report = inspect_codex(cwd, home, settings, warnings, hash_files=True)
    if report.diagnostics:
        raise ScopeError('cannot snapshot effective instructions after file errors; run explain')
    anchor = Path(report.root or report.cwd)
    rows = list(report.entries)
    # Ordinary inspection stops global discovery after a nonempty override.
    # Snapshot auditing also fingerprints the optional lower-priority file.
    if any(row['scope'] == 'global' and row['status'] == 'loaded'
           and Path(row['path']).name == 'AGENTS.override.md' for row in rows):
        path = home/'AGENTS.md'
        try:
            meta = path.stat()
        except OSError:
            pass  # Irrelevant metadata must preserve the reviewed early-return fix.
        else:
            if stat.S_ISREG(meta.st_mode):
                index = next(i for i, row in enumerate(rows) if row['scope'] == 'global') + 1
                rows.insert(index, dict(path=str(path), scope='global', size=meta.st_size,
                                        status='shadowed-by-override', bytes_loaded=0, text_bytes=0))
    if len(rows) > MAX_FILES:
        raise ScopeError(f'snapshot exceeds {MAX_FILES} instruction entries')
    # Share the inspection accounting limit with optional audit reads. Selected
    # hashes were taken from the exact bytes used by the loader, without rereads.
    reader = Reader(total=sum(row['size'] for row in rows if 'sha256' in row),
                    files=sum('sha256' in row for row in rows))
    cuts = {point['path']: {key: point[key] for key in (
        'byte_offset', 'line', 'splits_utf8_character')} for point in report.truncations}
    entries = []
    for order, row in enumerate(rows):
        shadow = row['status'].startswith('shadowed-')
        size, sha, error = row['size'], row.get('sha256'), None
        if shadow:
            try:
                if reader.total >= MAX_TOTAL_BYTES or reader.files >= MAX_FILES:
                    raise ScopeError('optional hash inspection limit reached')
                data = reader.read(Path(row['path']))
            except (OSError, ScopeError) as exc:
                error = 'inspection limit or nonregular file' if isinstance(exc, ScopeError) else type(exc).__name__
            else:
                size, sha = len(data), hashlib.sha256(data).hexdigest()
        entries.append(dict(
            scope=row['scope'], path=Path(row['path']).relative_to(home if row['scope'] == 'global' else anchor).as_posix(),
            order=order, selected=not shadow and (row['scope'] == 'project' or row['status'] == 'loaded'),
            status=row['status'], size=size, bytes_loaded=row['bytes_loaded'],
            text_bytes=row['text_bytes'], sha256=sha, hash_error=error))
        if snapshot_version == 2:
            entries[-1]['cut'] = cuts.get(row['path']) if row['scope'] == 'project' else None
    rendered = report.text.encode('utf-8')
    return validate_snapshot(dict(format=FORMAT, schema_version=snapshot_version, agent='codex', semantics=UPSTREAM_SHA,
                cwd=Path(report.cwd).relative_to(anchor).as_posix(), root_found=report.root is not None,
                settings=asdict(settings),
                effective=dict(sha256=hashlib.sha256(rendered).hexdigest(), rendered_bytes=len(rendered)),
                entries=entries))


def create_manifest(cwds: list[Path], base: Path, home: Path, settings: Settings,
                    warnings: list[str] | None = None, *, snapshot_version: int = 1) -> dict:
    _require(type(snapshot_version) is int and snapshot_version in (1, 2))
    base = directory(base)
    folders = normalize_cwds(cwds, 'manifest')
    if any(not folder.is_relative_to(base) for folder in folders):
        raise ScopeError('manifest working directories must be within --base-dir lexically')
    doc = dict(format=MANIFEST_FORMAT, schema_version=snapshot_version, agent='codex', semantics=UPSTREAM_SHA,
               directories=[dict(cwd=folder.relative_to(base).as_posix(),
                                 snapshot=create_snapshot(folder, home, settings, warnings,
                                                          snapshot_version=snapshot_version))
                            for folder in folders])
    # Match the CLI's exact UTF-8 JSON output (including its final newline), so
    # every successful capture can be read by the bounded artifact reader.
    if len(json.dumps(doc, ensure_ascii=True, indent=2).encode('utf-8')) + 1 > MAX_FILE_BYTES:
        raise ScopeError(f'manifest exceeds {MAX_FILE_BYTES}-byte artifact limit')
    return validate_manifest(doc)


def _require(condition: bool) -> None:
    if not condition:
        raise ScopeError('invalid snapshot: unsupported format or malformed fields')


def _keys(value: object, names: str) -> bool:
    return isinstance(value, dict) and set(value) == set(names.split())


def _integer(value: object) -> bool:
    return type(value) is int and value >= 0


def _digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _relative(value: object, *, cwd: bool = False) -> bool:
    return (isinstance(value, str) and bool(value) and '\0' not in value
            and not Path(value).is_absolute() and '..' not in Path(value).parts
            and Path(value).as_posix() == value and (cwd or value != '.'))


def _validate_v2_entry(row: dict, remaining: int) -> None:
    # Necessary loader invariants only: bodies/hashes cannot be authenticated.
    loaded, text, status = row['bytes_loaded'], row['text_bytes'], row['status']
    if status in ('skipped-empty', 'dropped-over-budget') or not row['selected']:
        _require(loaded == 0 and text == 0)
    else:
        # Lossy UTF-8 decoding can expand each source byte to three UTF-8 bytes.
        # Globals trim whitespace; project prefixes retain it when nonempty.
        _require(loaded > 0 and 0 < text <= 3 * loaded)
        if row['scope'] == 'project':
            _require(text >= loaded)
    if row['scope'] == 'global':
        _require(status in ('loaded', 'skipped-empty', 'shadowed-by-override',
                            'shadowed-by-precedence'))
        if status == 'loaded':
            _require(loaded == row['size'])
    cut = row['cut']
    if row['scope'] != 'project' or not row['selected']:
        _require(cut is None)
        return
    _require(remaining >= 0 and row['bytes_loaded'] <= remaining)
    if row['size'] <= remaining:
        _require(cut is None and loaded in (0, row['size']))
        # Zero-budget empty files are dropped despite having no cut. Empty
        # prefixes at positive budget are uncharged, even if the file fits.
        _require(status == ('dropped-over-budget' if remaining == 0 else
                            'skipped-empty' if loaded == 0 else 'loaded'))
        return
    _require(_keys(cut, 'byte_offset line splits_utf8_character'))
    _require(_integer(cut['byte_offset']) and cut['byte_offset'] == remaining)
    _require(type(cut['line']) is int and 1 <= cut['line'] <= remaining + 1)
    _require(type(cut['splits_utf8_character']) is bool)
    if remaining == 0:
        _require(cut['line'] == 1 and not cut['splits_utf8_character'])
    _require(row['bytes_loaded'] in (0, remaining))
    _require(row['status'] == ('dropped-over-budget' if remaining == 0 else
                              'skipped-empty' if row['bytes_loaded'] == 0 else 'partial'))


def validate_snapshot(doc: object) -> dict:
    """Reject incomplete/unrecognized documents rather than silently passing CI.

    Hashes are assertions from a trusted snapshot artifact, not signatures.
    Instruction bodies are intentionally unavailable for recomputing them here.
    """
    _require(_keys(doc, 'format schema_version agent semantics cwd root_found settings effective entries'))
    _require(doc['format'] == FORMAT and type(doc['schema_version']) is int
             and doc['schema_version'] in (1, 2) and doc['agent'] == 'codex' and doc['semantics'] == UPSTREAM_SHA)
    _require(_relative(doc['cwd'], cwd=True) and type(doc['root_found']) is bool)
    config = doc['settings']
    _require(_keys(config, 'max_bytes fallback root_markers'))
    _require(_integer(config['max_bytes']) and config['max_bytes'] <= 2**63 - 1)
    for key in ('fallback', 'root_markers'):
        _require(isinstance(config[key], list) and len(config[key]) <= 128
                 and all(isinstance(name, str) and filename(name) for name in config[key]))
    effective = doc['effective']
    _require(_keys(effective, 'sha256 rendered_bytes'))
    _require(_digest(effective['sha256']) and _integer(effective['rendered_bytes']))
    _require(isinstance(doc['entries'], list) and len(doc['entries']) <= MAX_FILES)
    seen = set()
    project_used = 0
    for order, row in enumerate(doc['entries']):
        _require(_keys(row, 'scope path order selected status size bytes_loaded text_bytes sha256 hash_error'
                       + (' cut' if doc['schema_version'] == 2 else '')))
        _require(row['scope'] in ('global', 'project') and _relative(row['path']))
        _require(type(row['order']) is int and row['order'] == order and type(row['selected']) is bool)
        _require(isinstance(row['status'], str) and row['status'] in STATUSES)
        shadow = row['status'].startswith('shadowed-')
        _require(_integer(row['bytes_loaded']) and _integer(row['text_bytes']))
        _require((_integer(row['size']) and row['bytes_loaded'] <= row['size'])
                 or (shadow and row['size'] is None and row['bytes_loaded'] == 0))
        if shadow:
            _require(row['bytes_loaded'] == 0 and row['text_bytes'] == 0)
        _require(row['selected'] == (not shadow and (row['scope'] == 'project' or row['status'] == 'loaded')))
        _require((_digest(row['sha256']) and row['hash_error'] is None)
                 or (shadow and row['sha256'] is None and isinstance(row['hash_error'], str)
                     and bool(row['hash_error'])))
        if doc['schema_version'] == 2:
            _validate_v2_entry(row, config['max_bytes'] - project_used)
            if row['scope'] == 'project' and row['selected']:
                # Saved candidates constrain the winner even when empty or
                # over budget; optional audits cannot prove existence/precedence.
                _require(Path(row['path']).name in
                         ('AGENTS.override.md', 'AGENTS.md', *config['fallback']))
        if row['scope'] == 'project':
            project_used += row['bytes_loaded']
        key = row['scope'], row['path']
        _require(key not in seen)
        seen.add(key)
    if doc['schema_version'] == 2:
        # The pinned global provider returns the first nonempty body. Optional
        # shadow audit rows cannot make a second global participate in loading.
        _require(sum(row['selected'] for row in doc['entries'] if row['scope'] == 'global') <= 1)
        # Project discovery picks the first regular candidate per directory,
        # before loading; empty and over-budget winners also block fallback.
        project_directories = [Path(row['path']).parent for row in doc['entries']
                               if row['scope'] == 'project' and row['selected']]
        _require(len(project_directories) == len(set(project_directories)))
        # Discovery walks lexical root-to-cwd ancestors, or only cwd without a
        # root. Never resolve paths: symlink aliases keep distinct coordinates.
        cwd = Path(doc['cwd'])
        _require(all(cwd.is_relative_to(parent) if doc['root_found'] else parent == cwd
                     for parent in project_directories))
        # On this chain, increasing lexical depth is root-to-cwd discovery
        # order. Missing directories and optional shadow rows are allowed.
        _require(all(len(parent.parts) < len(child.parts)
                     for parent, child in zip(project_directories, project_directories[1:])))
        # text_bytes already accounts for replacement decoding and global trim.
        # Only nonempty selected bodies participate in the loader's joins;
        # skipped whitespace prefixes are uncharged and add no separator.
        project = [row['text_bytes'] for row in doc['entries']
                   if row['selected'] and row['scope'] == 'project' and row['text_bytes']]
        project_bytes = sum(project) + 2 * max(len(project) - 1, 0)
        global_bytes = sum(row['text_bytes'] for row in doc['entries']
                           if row['selected'] and row['scope'] == 'global')
        rendered_bytes = global_bytes + project_bytes
        if global_bytes and project_bytes:
            rendered_bytes += len(SEPARATOR.encode('utf-8'))
        _require(effective['rendered_bytes'] == rendered_bytes)
    return doc


def validate_manifest(doc: object) -> dict:
    _require(_keys(doc, 'format schema_version agent semantics directories'))
    _require(doc['format'] == MANIFEST_FORMAT and type(doc['schema_version']) is int
             and doc['schema_version'] in (1, 2) and doc['agent'] == 'codex' and doc['semantics'] == UPSTREAM_SHA)
    _require(isinstance(doc['directories'], list) and 1 <= len(doc['directories']) <= MAX_WORKDIRS)
    seen = set()
    for row in doc['directories']:
        _require(_keys(row, 'cwd snapshot'))
        _require(_relative(row['cwd'], cwd=True))
        _require(row['cwd'] not in seen)
        seen.add(row['cwd'])
        validate_snapshot(row['snapshot'])
        _require(row['snapshot']['schema_version'] == doc['schema_version'])
    return doc


def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def read_snapshot(path: Path) -> dict:
    try:
        raw = Reader().read(path)
        doc = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                         parse_constant=lambda _: _require(False))
    except (UnicodeError, ValueError, RecursionError) as exc:
        # JSON exception messages can quote instruction-like or secret content.
        raise ScopeError('invalid snapshot: unreadable JSON or malformed fields') from exc
    return validate_manifest(doc) if isinstance(doc, dict) and doc.get('format') == MANIFEST_FORMAT else validate_snapshot(doc)


def diff_snapshots(before: dict, after: dict) -> dict:
    validate_snapshot(before)
    validate_snapshot(after)
    context = {key: dict(before=before[key], after=after[key])
               for key in ('cwd', 'root_found', 'settings') if before[key] != after[key]}
    # V1 never captured cuts. Compare only common fields across versions rather
    # than treating absent knowledge as a known null cut or a metadata edit.
    states = (before, after)
    if before['schema_version'] != after['schema_version']:
        states = tuple(dict(entries=[{key: value for key, value in row.items() if key != 'cut'}
                                     for row in doc['entries']]) for doc in states)
    return dict(format='agentsmd-scope.diff', schema_version=1, agent='codex', semantics=UPSTREAM_SHA,
                effective_changed=before['effective'] != after['effective'],
                before_effective=before['effective'], after_effective=after['effective'],
                context_changes=context, changes=_changes(*states))


def _manifest_file_changes(before: dict[str, dict], after: dict[str, dict]) -> list[dict]:
    rows = []
    for cwd in dict.fromkeys([*before, *after]):
        left, right = before.get(cwd), after.get(cwd)
        if left is not None and right is not None:
            detail = diff_snapshots(left, right)
            if not (detail['effective_changed'] or detail['context_changes'] or detail['changes']):
                continue
            rows.append(dict(cwd=cwd, kind='changed', **{key: detail[key] for key in (
                'before_effective', 'after_effective', 'context_changes', 'changes')}))
        else:
            # Membership has no opposite version to project against: retain all
            # available entry fields, including cuts from a v2-only side.
            rows.append(dict(cwd=cwd, kind='added' if left is None else 'removed',
                before_effective=left['effective'] if left is not None else None,
                after_effective=right['effective'] if right is not None else None,
                context_changes={key: dict(before=left[key] if left is not None else None,
                                           after=right[key] if right is not None else None)
                                 for key in ('cwd', 'root_found', 'settings')},
                changes=_changes(left if left is not None else dict(entries=[]),
                                 right if right is not None else dict(entries=[]))))
    return rows


def _historical_loss(before: dict, after: dict) -> dict:
    """Account in saved source bytes; an accepted empty prefix is not excluded."""
    if before['schema_version'] != 2 or after['schema_version'] != 2:
        raise ScopeError('--fail-on-loss requires two v2 artifacts')
    if any(before[key] != after[key] for key in ('cwd', 'root_found')):
        raise ScopeError('--fail-on-loss requires unchanged snapshot path coordinates')
    changed = before['effective'] != after['effective']
    regressions = []
    if changed:
        old, new = ({(row['scope'], row['path']): row for row in doc['entries']}
                    for doc in (before, after))

        def retained(row: dict) -> int:
            return row.get('text_bytes', 0) if row.get('scope') == 'global' else row.get('bytes_loaded', 0)

        def excluded(row: dict) -> int:
            return row['size'] - row['cut']['byte_offset'] if row.get('cut') else 0

        for scope, path in dict.fromkeys([*old, *new]):
            left, right = old.get((scope, path), {}), new.get((scope, path), {})
            lost = max(retained(left) - retained(right), excluded(right) - excluded(left), 0)
            if lost:
                file_lost = (retained(left) > 0 and retained(right) == 0
                             or excluded(left) == 0 and excluded(right) > 0
                             and right['status'] == 'dropped-over-budget')
                regressions.append(dict(scope=scope, path=path, newly_lost_bytes=lost,
                                        newly_lost_file=file_lost))
    return dict(effective_changed=changed,
                newly_lost_bytes=sum(row['newly_lost_bytes'] for row in regressions),
                newly_lost_files=sum(row['newly_lost_file'] for row in regressions),
                regressions=regressions)


def _add_loss_policy(result: dict, before: dict, after: dict) -> dict:
    if before['format'] == MANIFEST_FORMAT:
        old, new = ({row['cwd']: row['snapshot'] for row in doc['directories']}
                    for doc in (before, after))
        if old.keys() != new.keys():
            raise ScopeError('--fail-on-loss requires unchanged manifest membership')
        rows = [dict(cwd=cwd, loss=_historical_loss(old[cwd], new[cwd])) for cwd in old]
        result['losses'] = rows
        losses = [row['loss'] for row in rows]
        result['schema_version'] = 3
    else:
        result['loss'] = _historical_loss(before, after)
        losses = [result['loss']]
        result['schema_version'] = 2
    regressed = sum(loss['newly_lost_bytes'] > 0 for loss in losses)
    result['loss_policy'] = dict(name='no-new-loss', passed=regressed == 0,
        regressed_cwd_count=regressed,
        newly_lost_bytes=sum(loss['newly_lost_bytes'] for loss in losses),
        newly_lost_files=sum(loss['newly_lost_files'] for loss in losses))
    return result


def diff_artifacts(before: dict, after: dict, *, include_file_changes: bool = False,
                   fail_on_loss: bool = False, cwd_map: dict[str, str] | None = None) -> dict:
    if cwd_map is not None and (before['format'] != MANIFEST_FORMAT or after['format'] != MANIFEST_FORMAT):
        raise ScopeError('--cwd-map requires two v2 manifests')
    if before['format'] != after['format']:
        raise ScopeError('cannot compare single-cwd snapshot with multi-cwd manifest')
    if before['format'] != MANIFEST_FORMAT:
        if include_file_changes:
            raise ScopeError('--include-file-changes requires two manifests')
        result = diff_snapshots(before, after)
        return _add_loss_policy(result, before, after) if fail_on_loss else result
    validate_manifest(before)
    validate_manifest(after)
    if cwd_map is not None:
        before, relocations = project_manifest(before, after, cwd_map)
        validate_manifest(before)
    old, new = ({row['cwd']: row['snapshot']['effective'] for row in doc['directories']}
                for doc in (before, after))
    changes = []
    for cwd in dict.fromkeys([*old, *new]):
        left, right = old.get(cwd), new.get(cwd)
        if left != right:
            changes.append(dict(cwd=cwd, kind='added' if left is None else 'removed' if right is None else 'changed',
                                before_effective=left, after_effective=right))
    result = dict(format='agentsmd-scope.manifest-diff', schema_version=1, agent='codex', semantics=UPSTREAM_SHA,
                effective_changed=bool(changes),
                summary=dict(before_cwd_count=len(old), after_cwd_count=len(new), changed_cwd_count=len(changes),
                             added_cwd_count=sum(row['kind'] == 'added' for row in changes),
                             removed_cwd_count=sum(row['kind'] == 'removed' for row in changes)),
                directories=changes)
    if include_file_changes:
        result['schema_version'] = 2
        result['file_changes'] = _manifest_file_changes(
            *({row['cwd']: row['snapshot'] for row in doc['directories']} for doc in (before, after)))
    if fail_on_loss:
        result = _add_loss_policy(result, before, after)
    if cwd_map is not None:
        result.update(schema_version=4, relocations=relocations)
    return result


def _explain_loss(diff: dict) -> list[str]:
    if 'loss_policy' not in diff:
        return []
    policy = diff['loss_policy']
    lines = [f"{'PASS' if policy['passed'] else 'FAIL'}: no-new-loss; "
             f"{policy['regressed_cwd_count']} working directories regressed; "
             f"{policy['newly_lost_bytes']} newly lost B, {policy['newly_lost_files']} newly lost files"]
    rows = diff['losses'] if 'losses' in diff else [dict(loss=diff['loss'])]
    for row in rows:
        if 'cwd' in row:
            lines.append(f"loss cwd: {json.dumps(row['cwd'], ensure_ascii=True)}")
        for regression in row['loss']['regressions']:
            lines.append(f"  {regression['scope']} {json.dumps(regression['path'], ensure_ascii=True)}: "
                         f"{regression['newly_lost_bytes']} newly lost B")
    return lines


def explain_diff(diff: dict) -> str:
    if diff['format'] == 'agentsmd-scope.manifest-diff':
        lines = [f"{'CHANGED' if diff['effective_changed'] else 'UNCHANGED'}: "
                 f"{diff['summary']['changed_cwd_count']} working directories changed"]
        lines.extend(f"{row['kind']} {json.dumps(row['cwd'], ensure_ascii=True)}" for row in diff['directories'])
        if 'file_changes' in diff:
            lines.append(f"file changes: {len(diff['file_changes'])} working directories")
            for row in diff['file_changes']:
                lines.append(f"{row['kind']} {json.dumps(row['cwd'], ensure_ascii=True)}")
                lines.extend(f"  {change['kind']} {change['scope']} "
                             f"{json.dumps(change['path'], ensure_ascii=True)}" for change in row['changes'])
                lines.extend(f'  context changed: {key}' for key in row['context_changes'])
        lines.extend(f"relocated {json.dumps(row['before_cwd'], ensure_ascii=True)} -> "
                     f"{json.dumps(row['after_cwd'], ensure_ascii=True)}" for row in diff.get('relocations', []))
        return '\n'.join(lines + _explain_loss(diff))
    lines = [f"{'CHANGED' if diff['effective_changed'] else 'UNCHANGED'}: effective instructions; "
             f"{len(diff['changes'])} file change(s)"]
    for change in diff['changes']:
        lines.append(f"{change['kind']} {change['scope']} {json.dumps(change['path'], ensure_ascii=True)}")
    for key in diff['context_changes']:
        lines.append(f'context changed: {key}')
    return '\n'.join(lines + _explain_loss(diff))
