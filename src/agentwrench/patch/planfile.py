"""Versioned, self-contained preview files bound to a root and its original files.

These are review artifacts, not authenticated approvals. Loading is read-only;
the resulting in-memory plan still needs the normal commit-time stale checks.
"""
import hashlib
import json
from pathlib import Path
import re
import stat

from .engine import MAX_FILE_BYTES, MAX_TOTAL_BYTES, Plan, Snapshot, plan, safe_path
from .model import ApplyError, Patch, PatchError, validate_patch_paths, validate_path
from .parser import parse
from .unified import to_unified

MAX_PLAN_BYTES = 64 * 1024 * 1024
FIELDS = {'format', 'version', 'root', 'patch', 'preconditions', 'diff'}


def fingerprint(snapshot: Snapshot) -> dict:
    return {'sha256': hashlib.sha256(snapshot.data).hexdigest() if snapshot.data is not None else None,
            'mode': f'{snapshot.mode:04o}' if snapshot.data is not None else None}


def preview(patch: Patch, root: str | Path) -> str:
    """Return a JSON preview; do not write the plan or modify the target tree."""
    # Reparse so a programmatically built AST uses exactly the persisted grammar.
    text = patch.to_text()
    prepared = plan(parse(text), root)
    diff = to_unified(prepared)  # Also rejects an unsuccessful plan.
    document = {'format': 'v4a-plan', 'version': 1, 'root': str(prepared.root),
                'patch': text,
                'preconditions': {path: fingerprint(s) for path, s in prepared.snapshots.items()},
                'diff': diff}
    result = json.dumps(document, ensure_ascii=False, indent=2) + '\n'
    if len(result.encode('utf-8')) > MAX_PLAN_BYTES:
        raise PatchError('preview plan exceeds 64 MiB limit; split the patch')
    return result


def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise PatchError(f'duplicate JSON key in plan: {key}')
        result[key] = value
    return result


def _document(text: str) -> dict:
    if len(text.encode('utf-8')) > MAX_PLAN_BYTES:
        raise PatchError('preview plan exceeds 64 MiB limit')
    try:
        doc = json.loads(text, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError) as exc:
        raise PatchError(f'invalid plan JSON: {exc}') from exc
    if not isinstance(doc, dict) or doc.keys() != FIELDS:
        raise PatchError('plan requires exactly format, version, root, patch, preconditions and diff')
    if doc['format'] != 'v4a-plan' or type(doc['version']) is not int or doc['version'] != 1:
        raise PatchError('unsupported preview plan format/version')
    if not all(isinstance(doc[key], str) for key in ('root', 'patch', 'diff')):
        raise PatchError('plan root, patch and diff must be strings')
    if not isinstance(doc['preconditions'], dict) or len(doc['preconditions']) > 2000:
        raise PatchError('plan preconditions must be an object with at most 2000 paths')
    for path, entry in doc['preconditions'].items():
        validate_path(path)
        if not isinstance(entry, dict) or entry.keys() != {'sha256', 'mode'}:
            raise PatchError(f'{path}: precondition requires sha256 and mode')
        if entry == {'sha256': None, 'mode': None}:
            continue  # Absence is distinct from the SHA-256 of a zero-byte file.
        if (not isinstance(entry['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', entry['sha256'])
                or not isinstance(entry['mode'], str) or not re.fullmatch('[0-7]{4}', entry['mode'])):
            raise PatchError(f'{path}: expected SHA-256 and four octal permission digits, or two nulls')
    return doc


def _plan_document(text: str) -> tuple[dict, Patch]:
    doc = _document(text)
    patch = parse(doc['patch'])
    paths = validate_patch_paths(patch)
    if set(paths) != doc['preconditions'].keys():
        raise PatchError('plan preconditions must cover every patch path exactly once')
    for operation in patch.files:
        absent = doc['preconditions'][operation.path]['sha256'] is None
        if absent != (operation.kind == 'add'):
            required = 'absence' if operation.kind == 'add' else 'an existing file'
            raise PatchError(f'{operation.path}: {operation.kind} precondition requires {required}')
        if operation.move_to and doc['preconditions'][operation.move_to]['sha256'] is not None:
            raise PatchError(f'{operation.move_to}: move destination precondition requires absence')
    return doc, patch


def plan_check_report(diagnostics: list[dict], *, diff_verified: bool = False) -> dict:
    return {'format': 'v4a-plan-check', 'version': 1, 'ok': not diagnostics,
            'diff_verified': diff_verified, 'diagnostics': diagnostics}


def _drift(path: str | None, codes: list[str], reason: str,
           expected: dict | None = None, actual: dict | None = None) -> dict:
    return {'path': path, 'codes': codes, 'expected': expected, 'actual': actual, 'reason': reason}


def check_plan(text: str, root: str | Path) -> dict:
    """Report all precondition failures without writes; verify the diff if none fail.

    Diagnostics follow patch path order, independently of JSON object ordering.
    Unsafe/unreadable/over-budget paths have an unknown (null) actual fingerprint.
    Invalid plan input raises PatchError; no saved diff can be verified on a stale
    base because the plan stores original hashes rather than complete originals.
    """
    doc, patch = _plan_document(text)
    root = Path(root).resolve()
    if str(root) != doc['root']:
        return plan_check_report([_drift(None, ['ROOT_MISMATCH'], 'plan belongs to a different root')])
    if not root.is_dir():
        return plan_check_report([_drift(None, ['ROOT_UNAVAILABLE'], 'plan root is no longer a directory')])
    diagnostics = []
    total = 0
    paths = [path for f in patch.files for path in ([f.path, f.move_to] if f.move_to else [f.path])]
    for relative in paths:
        expected = doc['preconditions'][relative]
        try:
            target = safe_path(root, relative)
        except (PatchError, ApplyError) as exc:
            diagnostics.append(_drift(relative, ['UNSAFE_PATH'], str(exc), expected))
            continue
        except OSError as exc:
            diagnostics.append(_drift(relative, ['IO_ERROR'], str(exc), expected))
            continue
        try:
            try:
                info = target.stat()
            except FileNotFoundError:
                info = None
            snapshot = Snapshot(None)
            if info is not None:
                limit = min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - total)
                if info.st_size > limit:
                    diagnostics.append(_drift(relative, ['RESOURCE_LIMIT'],
                        'file exceeds 8 MiB or remaining 64 MiB source budget', expected))
                    continue
                with target.open('rb') as handle:
                    data = handle.read(limit + 1)
                total += len(data)
                if len(data) > limit:
                    diagnostics.append(_drift(relative, ['RESOURCE_LIMIT'],
                        'file exceeds 8 MiB or remaining 64 MiB source budget', expected))
                    continue
                snapshot = Snapshot(data, stat.S_IMODE(info.st_mode))
            actual = fingerprint(snapshot)
        except OSError as exc:
            diagnostics.append(_drift(relative, ['IO_ERROR'], str(exc), expected))
            continue
        codes = []
        if (actual['sha256'] is None) != (expected['sha256'] is None):
            codes.append('ABSENCE_MISMATCH')
        elif actual['sha256'] is not None:
            if actual['sha256'] != expected['sha256']:
                codes.append('BYTES_MISMATCH')
            if actual['mode'] != expected['mode']:
                codes.append('MODE_MISMATCH')
        if codes:
            diagnostics.append(_drift(relative, codes, 'tree changed since planning', expected, actual))
    if diagnostics:
        return plan_check_report(diagnostics)
    # Reuse full application validation, including the saved diff comparison and
    # a second snapshot check. This never calls commit or creates staging data.
    load_plan(text, root)
    return plan_check_report([], diff_verified=True)


def load_plan(text: str, root: str | Path) -> Plan:
    """Verify all original hashes/modes and the preview, then prepare for commit.

    Never silently rebase a saved plan onto new context. Every check here precedes
    staging, including for dry runs, and commit rechecks the resulting snapshots.
    """
    doc, patch = _plan_document(text)
    root = Path(root).resolve()
    if str(root) != doc['root']:
        raise ApplyError('plan belongs to a different root; generate a new preview')
    if not root.is_dir():
        raise ApplyError(f'plan root is no longer a directory: {root}')
    total = 0
    for relative, expected in doc['preconditions'].items():
        try:
            target = safe_path(root, relative)
            snapshot = Snapshot(None)
            if target.exists():
                info = target.stat()
                if info.st_size > MAX_FILE_BYTES:
                    raise ApplyError('file exceeds 8 MiB limit')
                remaining = MAX_TOTAL_BYTES - total
                if info.st_size > remaining:
                    raise ApplyError('total source data exceeds 64 MiB limit')
                mode = stat.S_IMODE(info.st_mode)
                # Stat is only an early refusal; cap growth before hashing too.
                with target.open('rb') as handle:
                    data = handle.read(min(MAX_FILE_BYTES, remaining) + 1)
                if len(data) > MAX_FILE_BYTES:
                    raise ApplyError('file exceeds 8 MiB limit')
                total += len(data)
                if total > MAX_TOTAL_BYTES:
                    raise ApplyError('total source data exceeds 64 MiB limit')
                snapshot = Snapshot(data, mode)
            if fingerprint(snapshot) != expected:
                raise ApplyError('original bytes, permissions or absence no longer match')
        except (OSError, ValueError, ApplyError) as exc:
            raise ApplyError(f'tree changed since planning: {relative}: {exc}') from exc
    prepared = plan(patch, root)
    # Catch changes between the hash checks and the planning reads as well.
    for relative, snapshot in prepared.snapshots.items():
        if fingerprint(snapshot) != doc['preconditions'][relative]:
            raise ApplyError(f'tree changed since planning: {relative}')
    if to_unified(prepared) != doc['diff']:
        raise PatchError('saved diff disagrees with planned result; generate a new preview')
    return prepared
