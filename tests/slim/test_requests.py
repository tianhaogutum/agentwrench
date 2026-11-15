"""Port of test/requests.test.js (deterministic mock clock)."""

import math

import pytest

from agentwrench.slim import timers
from agentwrench.slim.config import InputError
from agentwrench.slim.requests import MAX_CANCELLED, MAX_PENDING, MAX_TIMEOUT_MS, PendingRequest, RequestTracker, validate_request_timeout


def raise_error(error):
    raise error


def test_request_deadline_response_and_expiry_at_the_same_time_have_one_synchronous_winner(mock_timers):
    for response_first in (True, False):
        events = []
        tracker = RequestTracker(100, lambda request_id, request: events.append(('timeout', request_id)), raise_error)

        def respond():
            request = tracker.take('boundary')
            if not request.timed_out:
                events.append(('result', 'boundary'))

        # Both callbacks have exactly the same deadline on a deterministic clock.
        # Exercise each ordering rather than relying on OS scheduling jitter.
        if response_first:
            timers.set_timeout(respond, 100)
        tracker.add('boundary', PendingRequest('tools/list'))
        if not response_first:
            timers.set_timeout(respond, 100)
        mock_timers.tick(100)
        assert events == [('result' if response_first else 'timeout', 'boundary')]
        assert len(tracker.pending) + len(tracker.cancelled) == 0
        mock_timers.tick(100)
        assert len(events) == 1


def test_request_deadline_reply_just_before_expiry_disarms_timer_before_asynchronous_output(mock_timers):
    expired = []
    tracker = RequestTracker(100, lambda request_id, request: expired.append(request_id), raise_error)
    tracker.add(1, PendingRequest('tools/list'))
    mock_timers.tick(99)
    assert tracker.take(1).method == 'tools/list'
    # It is safe to reuse a completed ID; its old timer must not expire this one.
    tracker.add(1, PendingRequest('ping'))
    mock_timers.tick(1)
    assert expired == []
    mock_timers.tick(99)
    assert expired == [1]
    assert tracker.take(1).timed_out is True


def test_request_deadline_disabled_by_default_cancellation_and_shutdown_disarm_timers(mock_timers):
    expired = []
    for mode in ('disabled', 'cancelled', 'shutdown'):
        tracker = RequestTracker(None if mode == 'disabled' else 100, lambda request_id, request: expired.append(request_id), raise_error)
        tracker.add(mode, PendingRequest('tools/list'))
        if mode == 'cancelled':
            tracker.cancel(mode)
        if mode == 'shutdown':
            tracker.stop_timers()
        mock_timers.tick(1000)
        assert expired == []
        assert tracker.take(mode).timed_out is (False if mode == 'cancelled' else None)


def test_request_deadline_cancelled_and_timed_out_requests_share_a_bounded_reservation_pool(mock_timers):
    expired, failures = [], []
    tracker = RequestTracker(100, lambda request_id, request: expired.append(request_id), lambda error: failures.append(str(error)))
    for request_id in range(MAX_CANCELLED - 1):
        tracker.add(request_id, PendingRequest('tools/list'))
        tracker.cancel(request_id)
    tracker.add('last', PendingRequest('tools/list'))
    mock_timers.tick(100)
    assert expired == ['last']
    assert len(tracker.cancelled) == MAX_CANCELLED
    assert len(tracker.pending) == 0
    with pytest.raises(InputError, match='unresolved'):
        tracker.add('last', PendingRequest('ping'))
    tracker.add('overflow', PendingRequest('ping'))
    mock_timers.tick(100)
    assert 'cancelled request limit reached' in failures[0]
    assert len(tracker.cancelled) == MAX_CANCELLED
    assert len(tracker.pending) == 1
    assert expired == ['last']
    assert tracker.take(0).method == 'tools/list'
    assert tracker.take('last').timed_out is True
    assert tracker.take('overflow').timed_out is None
    tracker.add('recovered', PendingRequest('ping'))
    mock_timers.tick(100)
    assert expired == ['last', 'recovered']


def test_request_deadline_active_request_bound_is_separate_from_retained_cancellations():
    tracker = RequestTracker()
    for request_id in range(MAX_PENDING):
        tracker.add(request_id, PendingRequest('ping'))
    with pytest.raises(InputError, match='pending request limit'):
        tracker.add('overflow', PendingRequest('ping'))
    tracker.cancel(0)
    tracker.add('replacement', PendingRequest('ping'))
    assert len(tracker.pending) == MAX_PENDING
    assert len(tracker.cancelled) == 1


def test_request_deadline_validation_prevents_timer_overflow_and_accidental_immediate_deadlines():
    assert validate_request_timeout(None) is None
    for ms in (1, MAX_TIMEOUT_MS):
        assert validate_request_timeout(ms) == ms
    for ms in (0, -1, 1.5, math.nan, math.inf, '100', MAX_TIMEOUT_MS + 1, True):
        with pytest.raises(InputError, match='--request-timeout-ms'):
            validate_request_timeout(ms)
