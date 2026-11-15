#!/usr/bin/env python3
"""Generate synthetic, deterministic, redistributable LangGraph SQLite history."""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import operator
from pathlib import Path
import sqlite3
from typing import Annotated, TypedDict
import uuid

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context


class State(TypedDict):
    items: Annotated[list[str], operator.add]
    counter: int
    payload: str


def advance(state: State):
    n = state.get("counter", 0) + 1
    return {"counter": n, "items": [f"event-{n:04d}"], "payload": "synthetic-" * 64}


def build_graph(saver):
    graph = StateGraph(State)
    graph.add_node("advance", advance)
    graph.add_edge(START, "advance")
    graph.add_edge("advance", END)
    return graph.compile(checkpointer=saver)


def normalize(con, saver):
    """Remove runtime UUIDs, random versions and wall clock from this simple graph.

    The original graph really executes; normalized rows are written using the
    upstream serializer. This fixture has no subgraphs or embedded checkpoint IDs.
    """
    con.row_factory = sqlite3.Row
    rows = list(con.execute("SELECT * FROM checkpoints ORDER BY thread_id, checkpoint_ns, checkpoint_id"))
    writes = list(con.execute("SELECT * FROM writes ORDER BY thread_id, checkpoint_ns, checkpoint_id, task_id, idx"))
    ids, counters = {}, {}
    versions = {}
    decoded = []
    for row in rows:
        group = (row["thread_id"], row["checkpoint_ns"])
        n = counters[group] = counters.get(group, 0) + 1
        ids[(*group, row["checkpoint_id"])] = f"00000000-0000-6000-8000-{n:012d}"
        cp = saver.serde.loads_typed((row["type"], row["checkpoint"]))
        decoded.append(cp)
        versions.setdefault(group, set()).update(cp["channel_versions"].values())
    version_maps = {g: {v: f"{i + 1:032d}.0.0000000000000000" for i, v in enumerate(sorted(vs))} for g, vs in versions.items()}
    con.execute("DELETE FROM writes")
    con.execute("DELETE FROM checkpoints")
    for row, cp in zip(rows, decoded):
        group = (row["thread_id"], row["checkpoint_ns"])
        identity = ids[(*group, row["checkpoint_id"])]
        cp["id"] = identity
        cp["ts"] = (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=int(identity[-12:]))).isoformat()
        cp["channel_versions"] = {k: version_maps[group][v] for k, v in cp["channel_versions"].items()}
        cp["versions_seen"] = {node: {k: version_maps[group][v] for k, v in seen.items()}
                               for node, seen in cp["versions_seen"].items()}
        kind, blob = saver.serde.dumps_typed(cp)
        parent = ids.get((*group, row["parent_checkpoint_id"]))
        con.execute("INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?)",
                    (*group, identity, parent, kind, blob, row["metadata"]))
    task_maps = {}
    for row in writes:
        group = (row["thread_id"], row["checkpoint_ns"])
        cp_id = ids[(*group, row["checkpoint_id"])]
        tasks = task_maps.setdefault((*group, cp_id), {})
        task_id = tasks.setdefault(row["task_id"], str(uuid.uuid5(uuid.NAMESPACE_URL, repr((*group, cp_id, len(tasks))))))
        con.execute("INSERT INTO writes VALUES (?,?,?,?,?,?,?,?,?)",
                    (*group, cp_id, task_id, row["task_path"], row["idx"], row["channel"], row["type"], row["value"]))
    con.commit()


def make_fixture(path, threads=3, steps=12):
    path = Path(path)
    if threads < 1 or steps < 1 or threads * steps > 20_000:
        raise ValueError("threads and steps must be positive; product must be <= 20000")
    # Exclusive reservation prevents overwriting user data, including symlinks.
    with path.open("xb"):
        pass
    with closing(sqlite3.connect(path, check_same_thread=False)) as con:
        saver = SqliteSaver(con)
        graph = build_graph(saver)
        for t in range(1, threads + 1):
            config = {"configurable": {"thread_id": f"t-{t}"}}
            for _ in range(steps):
                with tracing_context(enabled=False):
                    graph.invoke({"items": []}, config)
        normalize(con, saver)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("VACUUM")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("PRAGMA journal_mode=DELETE")
    return {"threads": threads, "invocations_per_thread": steps, "checkpoints": threads * steps * 3}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db", type=Path)
    parser.add_argument("--threads", type=int, default=3)
    parser.add_argument("--steps", type=int, default=12)
    args = parser.parse_args()
    try:
        print(json.dumps(make_fixture(args.db, args.threads, args.steps)))
    except (OSError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    main()
