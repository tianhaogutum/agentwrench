"""Event-loop timers and a monotonic clock, mirroring Node.js timer globals.

Library code always calls these through the module (``timers.set_timeout``) so
tests can substitute a deterministic clock, like Node's ``t.mock.timers``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any


class Timer:
    """A cancellable one-shot timer handle (``setTimeout`` return value)."""

    def __init__(self, handle: Any) -> None:
        self._handle = handle
        self._ref = True

    def cancel(self) -> None:
        self._handle.cancel()

    def unref(self) -> Timer:
        # asyncio timers never keep the loop alive; the flag is informational.
        self._ref = False
        return self

    def has_ref(self) -> bool:
        return self._ref


def set_timeout(callback: Callable[[], Any], ms: float) -> Timer:
    loop = asyncio.get_running_loop()
    return Timer(loop.call_later(ms / 1000, callback))


def clear_timeout(timer: Timer | None) -> None:
    if timer is not None:
        timer.cancel()


def now() -> float:
    """Monotonic milliseconds (``performance.now``)."""
    return time.monotonic() * 1000
