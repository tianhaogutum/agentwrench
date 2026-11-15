"""Plan, back up, delete, and verify while holding a single SQLite writer lock."""
from collections import defaultdict
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from .codec import checkpoint, decode, encoded, state_hash
from .db import ScopeError, blob_size, connect, key, read_rows
from .prune_plan import FORMAT, artifact, database_binding, load_plan, row_fingerprints, save_plan

DELETE_PATH = "local-transaction (pinned SqliteSaver.prune is unimplemented)"


def validate_prunable(cps, writes):
    for row in cps:
        cp, error = checkpoint(row)
        if error:
            raise ScopeError("prune refused: opaque/unsupported checkpoint; inspection is still available")
        try:
            metadata = json.loads(row["metadata"])
        except (ValueError, TypeError):
            raise ScopeError("prune refused: invalid checkpoint metadata") from None
        if not isinstance(metadata, dict):
            raise ScopeError("prune refused: invalid checkpoint metadata")
        if "counters_since_delta_snapshot" in metadata:
            raise ScopeError("prune refused: DeltaChannel history requires ancestor reconstruction")
        # Saved subgraphs can refer to historical child checkpoints. No graph code
        # is loaded to guess whether deleting them is safe.
        if row["checkpoint_ns"] or metadata.get("parents"):
            raise ScopeError("prune refused: subgraphs/nonempty namespaces are outside v1 continuation guarantees")
        if not isinstance(cp.get("channel_versions"), dict) or not isinstance(cp.get("versions_seen"), dict):
            raise ScopeError("prune refused: unsupported checkpoint payload")
    known = {key(r) for r in cps}
    for row in writes:
        if key(row) not in known:
            raise ScopeError("prune refused: orphan pending writes")
        _, error = decode(row["type"], row["value"])
        if error:
            raise ScopeError("prune refused: opaque pending write")


def plan(cps, writes, keep):
    if keep < 1:
        raise ScopeError("--keep-latest must be >= 1")
    validate_prunable(cps, writes)
    grouped = defaultdict(list)
    for row in cps:
        grouped[key(row)[:2]].append(row)
    doomed = [row for rows in grouped.values() for row in rows[:-keep]]
    doomed_keys = {key(r) for r in doomed}
    doomed_writes = [r for r in writes if key(r) in doomed_keys]
    return {"delete_checkpoints": len(doomed), "delete_writes": len(doomed_writes),
            "reclaimable_payload_bytes": sum(blob_size(r, "checkpoint") for r in doomed) + sum(blob_size(r, "value") for r in doomed_writes),
            "reclaimable_metadata_bytes": sum(blob_size(r, "metadata") for r in doomed),
            "keep_latest": keep, "groups": len(grouped), "delete_path": DELETE_PATH}, doomed_keys


def fingerprints(cps, writes):
    """Both data-only state hashes and full retained row hashes, including writes."""
    latest, raw = {}, {}
    for row in cps:
        cp, error = checkpoint(row)
        if error:
            raise ScopeError("verification failed: checkpoint became opaque")
        latest[key(row)[:2]] = {"checkpoint_id": row["checkpoint_id"],
                               "state_sha256": state_hash(cp["channel_values"])}
        raw[("checkpoint", *key(row))] = hashlib.sha256(encoded(tuple(row))).hexdigest()
    for row in writes:
        raw[("write", *key(row), row["task_id"], row["idx"])] = hashlib.sha256(encoded(tuple(row))).hexdigest()
    return latest, raw


def write_backup(source_path, backup_path):
    target = Path(backup_path).expanduser()
    # Parent must already exist; never overwrite any file or symlink.
    target = target.parent.resolve(strict=True) / target.name
    db_path = Path(source_path).expanduser().resolve(strict=True)
    if target in (db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm"), Path(str(db_path) + "-journal")):
        raise ScopeError("backup must be separate from the database and its sidecars")
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        # Separate reader avoids backup() on a connection with an active write
        # transaction. The caller's BEGIN IMMEDIATE excludes concurrent writers.
        with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=0.25)) as source:
            with closing(sqlite3.connect(target)) as dest:
                source.backup(dest)
                if dest.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ScopeError("backup verification failed")
        return str(target)
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def prune(path, keep=None, *, thread=None, dry_run=False, backup=None, no_backup=False, vacuum=False,
          export_plan=None, apply_plan=None):
    if apply_plan is not None and (keep is not None or thread is not None or export_plan is not None):
        raise ScopeError("--apply-plan cannot be combined with --keep-latest, --thread or --export-plan")
    if export_plan is not None and not dry_run:
        raise ScopeError("--export-plan requires --dry-run; apply the saved plan separately")
    saved = load_plan(apply_plan) if apply_plan is not None else None
    if saved is not None:
        keep, thread = saved["keep_latest"], saved["scope"]["thread"]
    if keep is None:
        raise ScopeError("prune requires --keep-latest N or --apply-plan PATH")
    if keep < 1:
        raise ScopeError("--keep-latest must be >= 1")
    if backup and no_backup:
        raise ScopeError("choose either --backup or --no-backup")
    if not dry_run and not (backup or no_backup):
        raise ScopeError("execution requires --backup PATH or --no-backup")
    with connect(path, writable=not dry_run) as con:
        if saved is not None or export_plan is not None:
            # Bind the whole logical DB, including unselected rows. A changed
            # pending write must invalidate a saved decision even with stable IDs.
            cps, writes = read_rows(con)
            binding = database_binding(con, path, cps, writes)
            if saved is not None and binding != saved["database"]:
                raise ScopeError("stale prune plan: database path, schema or rows changed; export a new plan")
            raw_rows = row_fingerprints(cps, writes) if saved is None or saved["format"] == FORMAT else None
            if thread is not None:
                cps = [r for r in cps if r["thread_id"] == thread]
                writes = [r for r in writes if r["thread_id"] == thread]
                if not cps:
                    raise ScopeError("thread id not found", 1)
        else:
            cps, writes = read_rows(con, thread)
        result, doomed = plan(cps, writes, keep)
        if saved is not None or export_plan is not None:
            current = artifact(binding, cps, doomed, result, thread, raw_rows, saved["format"] if saved else FORMAT)
            if saved is not None and current != saved:
                raise ScopeError("invalid prune plan: checkpoint actions or summary do not match; export a new plan")
            if export_plan is not None:
                result["exported_plan"] = save_plan(export_plan, current, path)
            if saved is not None:
                result["plan_source"] = str(Path(apply_plan).expanduser().resolve(strict=True))
        before_latest, _ = fingerprints(cps, writes)
        retained_cps = [r for r in cps if key(r) not in doomed]
        retained_writes = [r for r in writes if key(r) not in doomed]
        _, before_retained = fingerprints(retained_cps, retained_writes)
        result.update({"dry_run": dry_run, "verified": False, "vacuum": "not requested",
                       "latest": [{"thread_id": t, "checkpoint_ns": ns, **data} for (t, ns), data in before_latest.items()]})
        if dry_run:
            result["verification"] = "plan preserves latest rows; no deletion or workflow was executed"
            result["vacuum"] = "skipped (dry-run)" if vacuum else "not requested"
            return result
        if backup:
            result["backup"] = write_backup(path, backup)
        for identity in sorted(doomed):
            con.execute("DELETE FROM writes WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?", identity)
            con.execute("DELETE FROM checkpoints WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?", identity)
        after_cps, after_writes = read_rows(con, thread)
        after_latest, after_retained = fingerprints(after_cps, after_writes)
        if before_latest != after_latest or before_retained != after_retained:
            raise ScopeError("post-prune verification failed; transaction rolled back")
        if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ScopeError("post-prune SQLite integrity check failed; transaction rolled back")
        con.commit()
        result["verified"] = True
        result["verification"] = "latest channel-state SHA256 and every retained checkpoint/write unchanged"
        if vacuum:
            # SQLite forbids VACUUM inside a transaction. A failed VACUUM does
            # not undo the already verified prune; report that distinction.
            try:
                con.execute("VACUUM")
                result["vacuum"] = "completed"
            except sqlite3.Error:
                result["vacuum"] = "failed after prune committed; run VACUUM offline later"
                result["vacuum_failed"] = True
        return result
