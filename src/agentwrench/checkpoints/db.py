"""Strict schema boundary and snapshot reads; never run saver setup on user DBs."""
from contextlib import closing, contextmanager
from functools import lru_cache
from pathlib import Path
import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver

from .codec import MAX_BLOB_BYTES

MAX_ROWS = 200_000
MAX_PAYLOAD_BYTES = 256 * 1024 * 1024
KEY = ("thread_id", "checkpoint_ns", "checkpoint_id")


class ScopeError(Exception):
    def __init__(self, message, code=2):
        super().__init__(message)
        self.code = code


@lru_cache(maxsize=1)
def expected_schema():
    with closing(sqlite3.connect(":memory:")) as con:
        SqliteSaver(con).setup()
        return schema(con)


def schema(con):
    result = {}
    for table in ("checkpoints", "writes"):
        result[table] = [tuple(r) for r in con.execute(f"PRAGMA table_info({table})")]
    return result


def validate_tables(con):
    tables = con.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name IN ('checkpoints','writes')").fetchall()
    if sorted(r[0] for r in tables) != ["checkpoints", "writes"]:
        raise ScopeError("unsupported schema version: expected real SqliteSaver tables")
    if schema(con) != expected_schema():
        raise ScopeError("unsupported schema version: expected pinned SqliteSaver tables/columns")


def validate(con):
    validate_tables(con)
    objects = con.execute("SELECT type, name FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*'").fetchall()
    if sorted(tuple(r) for r in objects) != [("table", "checkpoints"), ("table", "writes")]:
        raise ScopeError("unsupported schema version: extra tables, indexes, views or triggers")
    for table in ("checkpoints", "writes"):
        if con.execute(f"PRAGMA foreign_key_list({table})").fetchone():
            raise ScopeError("unsupported schema version: foreign keys")


@contextmanager
def connect(path, *, writable=False, validate_schema=True):
    # Diagnostics may inspect schema drift without accepting it for pruning.
    if writable and not validate_schema:
        raise ScopeError("writable connections require schema validation")
    path = Path(path).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ScopeError("database must be an existing regular file")
    mode = "rw" if writable else "ro"
    con = sqlite3.connect(path.as_uri() + f"?mode={mode}", uri=True, timeout=0.25,
                          isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA trusted_schema=OFF")
        if not writable:
            con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN IMMEDIATE" if writable else "BEGIN")
        if validate_schema:
            validate(con)
        yield con
    finally:
        if con.in_transaction:
            con.rollback()
        con.close()


def read_rows(con, thread=None):
    where, args = (" WHERE thread_id = ?", (thread,)) if thread is not None else ("", ())
    total, payload = 0, 0
    for table, expression in (("checkpoints", "coalesce(length(CAST(checkpoint AS BLOB)),0)+coalesce(length(CAST(metadata AS BLOB)),0)"),
                              ("writes", "coalesce(length(CAST(value AS BLOB)),0)")):
        count, size, largest = con.execute(
            f"SELECT count(*), coalesce(sum({expression}),0), coalesce(max({expression}),0) FROM {table}{where}", args
        ).fetchone()
        total += count
        payload += size
        if largest > MAX_BLOB_BYTES:
            raise ScopeError("database row exceeds 16 MiB safety limit")
    if total > MAX_ROWS or payload > MAX_PAYLOAD_BYTES:
        raise ScopeError("selection exceeds safety limits (200000 rows / 256 MiB payload); select --thread or split offline")
    cps = list(con.execute("SELECT * FROM checkpoints" + where + " ORDER BY thread_id, checkpoint_ns, checkpoint_id", args))
    writes = list(con.execute("SELECT * FROM writes" + where + " ORDER BY thread_id, checkpoint_ns, checkpoint_id, task_id, idx", args))
    for row in [*cps, *writes]:
        if not all(isinstance(row[column], str) for column in KEY):
            raise ScopeError("unsupported row format: checkpoint keys must be text")
    for row in writes:
        if (not isinstance(row["channel"], str) or not isinstance(row["task_id"], str)
                or not isinstance(row["task_path"], str) or not isinstance(row["idx"], int)):
            raise ScopeError("unsupported row format: invalid pending write keys")
    if thread is not None and not cps:
        raise ScopeError("thread id not found", 1)
    return cps, writes


def key(row):
    return tuple(row[k] for k in KEY)


def blob_size(row, field):
    value = row[field]
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    return len(value) if isinstance(value, bytes) else 0
