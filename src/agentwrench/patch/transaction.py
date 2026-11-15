"""Staged writes with rollback on ordinary commit-time errors.

A multi-file filesystem transaction is not crash atomic or isolated from readers.
The caller must exclude concurrent writers (including hostile symlink races).
"""
import json
import os
import shutil
from pathlib import Path
import tempfile

from .engine import Plan, plan, safe_path
from .model import ApplyError, Patch


def _check_snapshots(prepared: Plan) -> None:
    for relative, snapshot in prepared.snapshots.items():
        try:
            target = safe_path(prepared.root, relative)
            current = None
            if target.exists():
                info = target.stat()
                if (snapshot.data is None or info.st_size != len(snapshot.data)
                        or (info.st_mode & 0o7777) != snapshot.mode):
                    raise ApplyError('original bytes, permissions or absence no longer match')
                # Stat is only an early refusal; cap post-stat growth too.
                with target.open('rb') as handle:
                    current = handle.read(len(snapshot.data) + 1)
            if current != snapshot.data or (target.exists() and (target.stat().st_mode & 0o7777) != snapshot.mode):
                raise ApplyError('original bytes, permissions or absence no longer match')
        except (OSError, ValueError, ApplyError) as exc:
            raise ApplyError(f'tree changed since planning: {relative}: {exc}') from exc


def commit(prepared: Plan, *, dry_run: bool = False) -> Plan:
    if not prepared.ok:
        raise ApplyError("patch validation failed; no files changed", prepared.diagnostics)
    _check_snapshots(prepared)
    if dry_run or not prepared.changes:
        return prepared
    root = prepared.root
    created_dirs: list[Path] = []
    touched: list[str] = []
    temp = Path(tempfile.mkdtemp(prefix=".v4a-", dir=root))
    retain_recovery = False
    try:
        staged, backups = {}, {}
        try:
            # Snapshot bytes/permissions, not links: replacing a hard link never writes its peer.
            for number, (relative, snapshot) in enumerate(prepared.snapshots.items()):
                if snapshot.data is not None:
                    backup = temp / f"backup-{number}"
                    backup.write_bytes(snapshot.data)
                    backup.chmod(snapshot.mode)
                    backups[relative] = backup
            (temp / 'recovery.json').write_text(json.dumps({
                relative: {'backup': backups[relative].name if relative in backups else None,
                           'mode': snapshot.mode}
                for relative, snapshot in prepared.snapshots.items()
            }, indent=2) + '\n', encoding='utf-8')
            for number, change in enumerate(prepared.changes):
                if change.after is not None:
                    target = temp / f"new-{number}"
                    target.write_bytes(change.after)
                    target.chmod(change.mode)
                    staged[change.destination] = target
            _check_snapshots(prepared)
            for change in prepared.changes:
                if change.destination:
                    target = safe_path(root, change.destination)
                    missing = []
                    parent = target.parent
                    while not parent.exists():
                        missing.append(parent)
                        parent = parent.parent
                    for directory in reversed(missing):
                        directory.mkdir()
                        created_dirs.append(directory)
                    os.replace(staged[change.destination], target)
                    touched.append(change.destination)
                if change.destination != change.source and change.before is not None:
                    safe_path(root, change.source).unlink()
                    touched.append(change.source)
        except (OSError, ApplyError, ValueError) as exc:
            rollback_errors = []
            for relative in reversed(touched):
                try:
                    target = safe_path(root, relative)
                    if relative in backups:
                        os.replace(backups[relative], target)
                    else:
                        target.unlink(missing_ok=True)
                except (OSError, ValueError, ApplyError) as rollback_exc:
                    rollback_errors.append(f"{relative}: {rollback_exc}")
            for directory in reversed(created_dirs):
                try:
                    directory.rmdir()
                except OSError as rollback_exc:
                    rollback_errors.append(f"{directory}: {rollback_exc}")
            if rollback_errors:
                retain_recovery = True
                raise ApplyError(f"commit failed: {exc}; ROLLBACK INCOMPLETE: {'; '.join(rollback_errors)}; recovery data: {temp}") from exc
            raise ApplyError(f"commit failed; changes rolled back: {exc}") from exc
    finally:
        if not retain_recovery:
            try:
                shutil.rmtree(temp)
            except OSError as exc:
                prepared.diagnostics.append({'path': str(temp), 'hunk': None, 'status': 'warning',
                    'reason': f'temporary-data cleanup failed: {exc}',
                    'suggested_fix': 'Remove this private staging directory after inspecting it.'})
    return prepared


def apply(patch: Patch, root: str | Path, *, dry_run: bool = False) -> Plan:
    return commit(plan(patch, root), dry_run=dry_run)
