"""In-flight request correlation with optional per-request deadlines."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import timers
from .config import PROGRAM, InputError
from .jsjson import id_key, is_integer

MAX_PENDING = 10000
MAX_CANCELLED = 10000
MAX_TIMEOUT_MS = 2147483647


def validate_request_timeout(value: Any) -> Any:
    if value is not None and (not is_integer(value) or value < 1 or value > MAX_TIMEOUT_MS):
        raise InputError(f'--request-timeout-ms: expected an integer from 1 to {MAX_TIMEOUT_MS}')
    return value


@dataclass(eq=False)
class PendingRequest:
    method: str
    page: Any = None
    task_augmented: bool = False
    id: Any = None
    timer: timers.Timer | None = field(default=None, repr=False)
    timed_out: bool | None = None


class RequestTracker:
    """A deadline and an upstream response compete synchronously for the active
    descriptor. Claim before any I/O can yield, so exactly one outcome wins.

    ``pending`` and ``cancelled`` map typed ids (``1`` and ``"1"`` stay distinct)
    to their :class:`PendingRequest`; iterate ``request.id`` for the raw id.
    """

    def __init__(self, timeout_ms: int | None = None,
                 on_timeout: Callable[[Any, PendingRequest], Any] | None = None,
                 on_failure: Callable[[Exception], Any] | None = None) -> None:
        self.timeout_ms = validate_request_timeout(timeout_ms)
        self.on_timeout = on_timeout
        self.on_failure = on_failure
        self.pending: dict[Any, PendingRequest] = {}
        self.cancelled: dict[Any, PendingRequest] = {}

    def has_pending(self, request_id: Any) -> bool:
        return id_key(request_id) in self.pending

    def has_cancelled(self, request_id: Any) -> bool:
        return id_key(request_id) in self.cancelled

    def add(self, request_id: Any, request: PendingRequest) -> None:
        key = id_key(request_id)
        if len(self.pending) >= MAX_PENDING:
            raise InputError(f'{PROGRAM} pending request limit reached')
        if key in self.pending or key in self.cancelled:
            raise InputError('duplicate or unresolved client id')
        request.id = request_id
        self.pending[key] = request
        if self.timeout_ms is not None:
            def expire() -> None:
                if self.pending.get(key) is not request:
                    return
                try:
                    self.cancel(request_id, True)
                    self.on_timeout(request_id, request)
                except Exception as error:
                    self.on_failure(error)

            request.timer = timers.set_timeout(expire, self.timeout_ms)
            request.timer.unref()

    def cancel(self, request_id: Any, timed_out: bool = False) -> None:
        key = id_key(request_id)
        request = self.pending.get(key)
        if request is None:
            return
        if len(self.cancelled) >= MAX_CANCELLED:
            raise InputError(f'{PROGRAM} cancelled request limit reached')
        timers.clear_timeout(request.timer)
        request.timer = None
        request.timed_out = timed_out
        self.cancelled[key] = request
        del self.pending[key]

    def take(self, request_id: Any) -> PendingRequest | None:
        key = id_key(request_id)
        request = self.pending.get(key) or self.cancelled.get(key)
        timers.clear_timeout(request.timer if request is not None else None)
        self.pending.pop(key, None)
        self.cancelled.pop(key, None)
        return request

    def stop_timers(self) -> None:
        for request in self.pending.values():
            timers.clear_timeout(request.timer)
            request.timer = None
