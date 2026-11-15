"""Bounded, data-only saved prune plans. No execution options are persisted."""
import hashlib
import json
import os
from pathlib import Path

from .codec import encoded
from .db import KEY, MAX_ROWS, ScopeError

LEGACY_FORMAT = "lgckpt-scope.prune/v1"
FORMAT = "lgckpt-scope.prune/v2"
WRITE_KEY = (*KEY, "task_id", "idx")
MAX_PLAN_BYTES = 64 * 1024 * 1024


def schema_fingerprint(con):
    schema = [tuple(r) for r in con.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name")]
    pragmas = [con.execute(f"PRAGMA {name}").fetchone()[0]
               for name in ("schema_version", "user_version", "application_id")]
    return hashlib.sha256(encoded((schema, pragmas))).hexdigest()


def database_binding(con, path, cps, writes):
    # Logical content, not main-file bytes: includes committed WAL rows and is
    # independent of page layout. The caller holds a snapshot / writer lock.
    rows = hashlib.sha256()
    for table, records in (("checkpoints", cps), ("writes", writes)):
        for row in records:
            payload = encoded((table, *tuple(row)))
            rows.update(len(payload).to_bytes(8, "big"))
            rows.update(payload)
    return {"path": str(Path(path).expanduser().resolve(strict=True)),
            "schema_sha256": schema_fingerprint(con),
            "rows_sha256": rows.hexdigest()}


def row_fingerprints(cps, writes):
    """Raw typed rows, including metadata and every write column; no decoding."""
    return {table: [{**{field: row[field] for field in fields},
                     "sha256": hashlib.sha256(encoded((table, *tuple(row)))).hexdigest()}
                    for row in records]
            for table, fields, records in (("checkpoints", KEY, cps), ("writes", WRITE_KEY, writes))}


def artifact(binding, cps, doomed, summary, thread, raw_rows, version=FORMAT):
    value = {"format": version, "database": binding, "scope": {"thread": thread},
            "keep_latest": summary["keep_latest"], "summary": summary.copy(),
            "checkpoints": [{**{field: row[field] for field in KEY},
                             "action": "delete" if tuple(row[field] for field in KEY) in doomed else "keep"}
                            for row in cps]}
    if version == FORMAT:
        value["raw_rows"] = raw_rows
    return value


def load_plan(path):
    def unique(pairs):
        obj = {}
        for name, value in pairs:
            if name in obj:
                raise ValueError("duplicate JSON key")
            obj[name] = value
        return obj

    def reject_constant(_):
        raise ValueError("nonfinite JSON number")

    with Path(path).expanduser().open("rb") as source:
        data = source.read(MAX_PLAN_BYTES + 1)
    if len(data) > MAX_PLAN_BYTES:
        raise ScopeError("prune plan exceeds 64 MiB input limit")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=unique,
                           parse_constant=reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ScopeError("invalid prune plan JSON") from None

    def invalid():
        raise ScopeError("invalid prune plan: expected complete lgckpt-scope.prune/v1 or v2 plan")

    if not isinstance(value, dict) or value.get("format") not in (LEGACY_FORMAT, FORMAT):
        invalid()
    v2 = value["format"] == FORMAT
    fields = {"format", "database", "scope", "keep_latest", "summary", "checkpoints"}
    if v2:
        fields.add("raw_rows")
    if (set(value) != fields or type(value["keep_latest"]) is not int
            or value["keep_latest"] < 1 or not isinstance(value["summary"], dict)
            or not isinstance(value["scope"], dict) or set(value["scope"]) != {"thread"}
            or (value["scope"]["thread"] is not None and not isinstance(value["scope"]["thread"], str))):
        invalid()
    summary = value["summary"]
    counts = ("delete_checkpoints", "delete_writes", "reclaimable_payload_bytes",
              "reclaimable_metadata_bytes", "keep_latest", "groups")
    if (set(summary) != {*counts, "delete_path"}
            or not all(type(summary[field]) is int and summary[field] >= 0 for field in counts)
            or summary["keep_latest"] != value["keep_latest"]
            or not isinstance(summary["delete_path"], str)):
        invalid()
    binding = value["database"]
    if (not isinstance(binding, dict) or set(binding) != {"path", "schema_sha256", "rows_sha256"}
            or not isinstance(binding["path"], str)):
        invalid()
    def valid_digest(digest):
        return (isinstance(digest, str) and len(digest) == 64
                and all(c in "0123456789abcdef" for c in digest))

    if not all(valid_digest(binding[field]) for field in ("schema_sha256", "rows_sha256")):
        invalid()
    cps = value["checkpoints"]
    if not isinstance(cps, list) or len(cps) > MAX_ROWS:
        invalid()
    identities = set()
    for row in cps:
        if (not isinstance(row, dict) or set(row) != {*KEY, "action"}
                or not all(isinstance(row[field], str) for field in KEY)
                or row["action"] not in ("keep", "delete")):
            invalid()
        identity = tuple(row[field] for field in KEY)
        if identity in identities:
            invalid()
        identities.add(identity)
    if v2:
        raw = value["raw_rows"]
        if (not isinstance(raw, dict) or set(raw) != {"checkpoints", "writes"}
                or not all(isinstance(raw[t], list) for t in ("checkpoints", "writes"))
                or sum(len(raw[t]) for t in ("checkpoints", "writes")) > MAX_ROWS):
            invalid()
        raw_ids = {}
        for table, fields in (("checkpoints", KEY), ("writes", WRITE_KEY)):
            seen = set()
            for row in raw[table]:
                if (not isinstance(row, dict) or set(row) != {*fields, "sha256"}
                        or not all(isinstance(row[f], str) for f in fields if f != "idx")
                        or not valid_digest(row["sha256"])):
                    invalid()
                if table == "writes" and (type(row["idx"]) is not int or not -(2**63) <= row["idx"] < 2**63):
                    invalid()
                identity = tuple(row[f] for f in fields)
                if identity in seen:
                    invalid()
                seen.add(identity)
            raw_ids[table] = seen
        thread = value["scope"]["thread"]
        if identities != {k for k in raw_ids["checkpoints"] if thread is None or k[0] == thread}:
            invalid()
    return value


def save_plan(path, value, database_path):
    data = (json.dumps(value, ensure_ascii=True, indent=2) + "\n").encode("ascii")
    if len(data) > MAX_PLAN_BYTES:
        raise ScopeError("prune plan exceeds 64 MiB; select --thread or split offline")
    target = Path(path).expanduser()
    target = target.parent.resolve(strict=True) / target.name
    database = Path(database_path).expanduser().resolve(strict=True)
    if target in (database, *(Path(str(database) + suffix) for suffix in ("-wal", "-shm", "-journal"))):
        raise ScopeError("plan must be separate from the database and its sidecars")
    # Exclusive creation also refuses dangling symlinks. Remove incomplete output.
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return str(target)
