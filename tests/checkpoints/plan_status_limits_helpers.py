"""Database builders and assertions shared by the plan-status limit tests."""
from contextlib import closing
import hashlib
import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver

FIELDS = ('added_checkpoints', 'removed_checkpoints', 'added_database_checkpoints',
          'removed_database_checkpoints', 'added_writes', 'removed_writes',
          'changed_checkpoints', 'changed_writes')
GROUPS = [('t-1', ''), ('t-2', ''), ('t-2', 'ns\x00\x1b\n雪')]
INDICES = (-2, 2, 10)


def create_database(path):
    with closing(sqlite3.connect(path)) as con:
        saver = SqliteSaver(con)
        saver.setup()
        for thread, ns in GROUPS:
            for cid in ('c1', 'c2', 'c3', 'c4'):
                kind, payload = saver.serde.dumps_typed({
                    'v': 4, 'id': cid, 'ts': '2026-11-11T12:00:00+00:00',
                    'channel_values': {'x': 1}, 'channel_versions': {}, 'versions_seen': {}})
                con.execute('INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?)',
                            (thread, ns, cid, None, kind, payload, b'{}'))
            for task in ('removed', 'stable'):
                for idx in INDICES:
                    kind, payload = saver.serde.dumps_typed(1)
                    con.execute('INSERT INTO writes VALUES (?,?,?,?,?,?,?,?,?)',
                                (thread, ns, 'c4', task, '', idx, 'x', kind, payload))
        con.commit()
    return path


def change_database(con):
    con.execute("UPDATE checkpoints SET checkpoint_id='a1' WHERE checkpoint_id='c1'")
    con.execute("UPDATE checkpoints SET checkpoint_id='a2' WHERE checkpoint_id='c2'")
    con.execute("UPDATE checkpoints SET metadata=? WHERE checkpoint_id IN ('c3','c4')", (b'private-metadata',))
    con.execute("DELETE FROM writes WHERE task_id='removed'")
    con.execute("UPDATE writes SET task_path='private-write'")
    con.execute("INSERT INTO writes SELECT thread_id,checkpoint_ns,checkpoint_id,'added',task_path,idx,channel,type,value FROM writes")
    con.commit()


def hashes(folder):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()
            if p.is_file() and (p.suffix in ('.db', '.json') or p.name.endswith('-wal'))
            and not (p.name.endswith('-wal') and p.stat().st_size == 0)}


def assert_cap(full, bounded, limit):
    assert bounded['max_differences'] == limit
    assert set(bounded) == {*full, 'max_differences', 'difference_summary'}
    assert set(bounded['difference_summary']) == set(FIELDS)
    for field in full.keys() - set(FIELDS):
        assert bounded[field] == full[field]
    for field in FIELDS:
        rows = full[field]
        assert bounded[field] == (None if rows is None else rows[:limit])
        assert bounded['difference_summary'][field] == (None if rows is None else {
            'total': len(rows), 'shown': min(len(rows), limit), 'truncated': len(rows) > limit})
