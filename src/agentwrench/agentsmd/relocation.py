"""Explicit saved-manifest comparison views; never discover or rewrite sources."""
from __future__ import annotations

import json
import posixpath
from pathlib import Path, PurePosixPath

from .core import Reader, ScopeError
from .impact import MAX_WORKDIRS


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise ScopeError('invalid cwd map: ' + message)


def _identity(value: object) -> bool:
    return (isinstance(value, str) and bool(value) and '\0' not in value
            and not value.startswith('/') and '..' not in PurePosixPath(value).parts
            and PurePosixPath(value).as_posix() == value)


def validate_cwd_map(value: object) -> dict[str, str]:
    _check(isinstance(value, dict) and len(value) <= MAX_WORKDIRS,
           'expected an object with at most 64 cwd pairs')
    _check(all(_identity(k) and _identity(v) for k, v in value.items()),
           'endpoints must be exact normalized relative cwd identities')
    _check(len(set(value.values())) == len(value), 'destinations must be unique')
    return value


def read_cwd_map(path: Path) -> dict[str, str]:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _check(key not in result, 'duplicate JSON key')
            result[key] = value
        return result

    try:
        doc = json.loads(Reader().read(path).decode('utf-8'), object_pairs_hook=unique,
                         parse_constant=lambda _: _check(False, 'nonfinite JSON constant'))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ScopeError('invalid cwd map: unreadable JSON') from exc
    return validate_cwd_map(doc)


def _under(path: str, parent: str) -> bool:
    return parent == '.' or path == parent or path.startswith(parent + '/')


def _anchor(cwd: str, snapshot: dict) -> str:
    # The nested cwd is root-relative. A suffix match establishes the root's
    # base-relative coordinate without knowing either host's absolute paths.
    nested = snapshot['cwd']
    if nested == '.':
        return cwd
    _check(cwd == nested or cwd.endswith('/' + nested),
           'snapshot root is outside the saved base or cwd coordinates disagree')
    return cwd[:-len(nested)].rstrip('/') or '.'


def project_manifest(before: dict, after: dict, mapping: dict[str, str]) -> tuple[dict, list[dict]]:
    """Build a new baseline view, preserving original artifact objects."""
    validate_cwd_map(mapping)
    _check(before['schema_version'] == after['schema_version'] == 2,
           '--cwd-map requires two v2 manifests')
    old = {row['cwd']: row['snapshot'] for row in before['directories']}
    new = {row['cwd']: row['snapshot'] for row in after['directories']}
    _check(mapping.keys() <= old.keys() and set(mapping.values()) <= new.keys(),
           'every endpoint must exist in its respective manifest')
    identities = [mapping.get(cwd, cwd) for cwd in old]
    _check(len(set(identities)) == len(identities), 'projected cwd identities collide')

    def project(path):
        candidates = set()
        for source, target in mapping.items():
            if _under(path, source):
                suffix = path if source == '.' else path[len(source):].lstrip('/')
                candidates.add(posixpath.normpath(posixpath.join(target, suffix)))
        _check(len(candidates) <= 1, 'overlapping moves project conflicting coordinates')
        return next(iter(candidates)) if candidates else path

    # Validate overlaps even when the snapshots have no instruction entries.
    for source in mapping:
        project(source)
    forward, inverse = {}, {}
    roots_forward, roots_inverse = {}, {}
    directories, relocations = [], []
    for cwd, snapshot in old.items():
        target = mapping.get(cwd, cwd)
        if not mapping:
            directories.append(dict(cwd=cwd, snapshot=snapshot))
            continue
        anchor = _anchor(cwd, snapshot)
        mapped = cwd in mapping
        target_anchor = _anchor(target, new[target]) if mapped else anchor
        if mapped:
            _check(snapshot['root_found'] == new[target]['root_found'],
                   'mapped root_found must remain unchanged')
            _check(project(anchor) == target_anchor, 'mapped snapshot roots disagree')
        paired = target in new
        if paired:
            _check(roots_forward.get(anchor, target_anchor) == target_anchor,
                   'one original root has ambiguous projected coordinates')
        _check(roots_inverse.get(target_anchor, anchor) == anchor,
               'projected snapshot root coordinates collide')
        if paired:
            roots_forward[anchor] = target_anchor
        roots_inverse[target_anchor] = anchor
        entries, paths = [], []
        for entry in snapshot['entries']:
            if entry['scope'] == 'global':
                entries.append(entry)
                continue
            source = posixpath.join(anchor, entry['path']) if anchor != '.' else entry['path']
            projected = project(source) if mapped else source
            if paired:
                _check(forward.get(source, projected) == projected,
                       'one original file has ambiguous projected coordinates')
            _check(inverse.get(projected, source) == source, 'projected project file coordinates collide')
            if paired:
                forward[source] = projected
            inverse[projected] = source
            path = posixpath.relpath(projected, target_anchor)
            _check(_identity(path) and path != '.', 'projected file is outside the target snapshot root')
            entries.append(dict(entry, path=path))
            if mapped and path != entry['path']:
                paths.append(dict(before=entry['path'], after=path))
        if mapped:
            relocations.append(dict(before_cwd=cwd, after_cwd=target,
                before_snapshot_cwd=snapshot['cwd'], after_snapshot_cwd=new[target]['cwd'],
                project_paths=paths))
            snapshot = dict(snapshot, cwd=new[target]['cwd'], entries=entries)
        directories.append(dict(cwd=target, snapshot=snapshot))
    return dict(before, directories=directories), relocations
