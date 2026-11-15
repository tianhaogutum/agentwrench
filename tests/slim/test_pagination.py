"""Port of test/pagination.test.js."""

import pytest

from agentwrench.slim.pagination import MAX_PAGINATION_ENTRIES, PaginationError, PaginationTracker

UNSET = object()


def message(cursor=UNSET, params=None, meta=UNSET):
    result = {'params': {**(params or {}), **({} if cursor is UNSET else {'cursor': cursor})}}
    if meta is not UNSET:
        result['_meta'] = meta
    return result


def edge(tracker, cursor, next_cursor=UNSET, params=None, meta=UNSET):
    tracker.observe(tracker.begin(message(cursor, params, meta)), {} if next_cursor is UNSET else {'nextCursor': next_cursor})


def test_pagination_graph_detects_longer_cycles_with_out_of_order_pages_and_opaque_cursors():
    tracker = PaginationTracker()
    edge(tracker, 'B', 'C')
    edge(tracker, 'C', 'D')
    edge(tracker, 'A', 'B')
    with pytest.raises(PaginationError, match='pagination loop'):
        edge(tracker, 'D', 'A')
    # A rejected edge was never inserted; a terminal replacement still works.
    edge(tracker, 'D')
    edge(tracker, 'A', 'B')
    edge(tracker, '\ud800', '�')
    with pytest.raises(PaginationError, match='pagination loop'):
        edge(tracker, '�', '\ud800')
    edge(tracker, ' __proto__\n', '')
    with pytest.raises(PaginationError, match='pagination loop'):
        edge(tracker, '', ' __proto__\n')


def test_pagination_graph_isolates_modern_metadata_and_ignores_request_progress_tokens():
    tracker = PaginationTracker()
    edge(tracker, 'A', 'B', {}, {'tenant': 'one', 'progressToken': 1})
    edge(tracker, 'B', 'A', {}, {'tenant': 'two'})
    with pytest.raises(PaginationError, match='pagination loop'):
        edge(tracker, 'B', 'A', {}, {'progressToken': 2, 'tenant': 'one'})


def test_pagination_graph_ignores_stale_history_after_restart_or_catalogue_change():
    for reset in (lambda t: t.begin(message()), lambda t: t.clear()):
        tracker = PaginationTracker()
        stale = tracker.begin(message('A'))
        reset(tracker)
        edge(tracker, 'B', 'A')
        tracker.observe(stale, {'nextCursor': 'B'})
        with pytest.raises(PaginationError, match='pagination loop'):
            tracker.observe(stale, {'nextCursor': 'A'})
        # Stale A -> B must not poison the new graph. B -> A is a valid retry.
        edge(tracker, 'B', 'A')
        with pytest.raises(PaginationError, match='pagination loop'):
            edge(tracker, 'A', 'B')


def test_pagination_graph_bounds_retained_page_edges_without_eviction_and_recovers_on_reset():
    tracker = PaginationTracker()
    for i in range(MAX_PAGINATION_ENTRIES):
        edge(tracker, f'p{i}', f'p{i + 1}')
    # Both retries and loop checks still work at capacity.
    edge(tracker, 'p0', 'p1')
    with pytest.raises(PaginationError, match='pagination loop'):
        edge(tracker, f'p{MAX_PAGINATION_ENTRIES}', 'p0')
    with pytest.raises(PaginationError, match='page limit'):
        edge(tracker, 'overflow', 'next')
    edge(tracker, 'p0')
    edge(tracker, 'overflow', 'next')
    tracker.begin(message())
    edge(tracker, 'new', 'next')
    tracker.clear()
    edge(tracker, 'next', 'new')


def test_pagination_graph_bounds_distinct_contexts_without_evicting_existing_history():
    tracker = PaginationTracker()
    for i in range(MAX_PAGINATION_ENTRIES):
        tracker.begin(message('A', {'tenant': i}))
    with pytest.raises(PaginationError, match='context limit'):
        tracker.begin(message('A', {'tenant': 'overflow'}))
    edge(tracker, 'A', 'B', {'tenant': 0})
    with pytest.raises(PaginationError, match='pagination loop'):
        edge(tracker, 'B', 'A', {'tenant': 0})
    tracker.begin(message(UNSET, {'tenant': 0}))
    edge(tracker, 'B', 'A', {'tenant': 0})
    tracker.clear()
    edge(tracker, 'A', 'B', {'tenant': 'overflow'})
