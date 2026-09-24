from __future__ import annotations

import asyncio
import threading
import time
from typing import Final

from app.metrics import (
    ADMISSION_WAIT_SECONDS,
    EXECUTION_DURATION_SECONDS,
    EXECUTIONS_ACTIVE,
    EXECUTIONS_LIMIT,
)

_POLL_INTERVAL_SEC: Final[float] = 0.05


class ExecutionSlot:
    """One admitted execution. ``release`` is idempotent and thread-safe."""

    def __init__(self, limiter: ExecutionLimiter, operation: str) -> None:
        self._limiter = limiter
        self._operation = operation
        self._started = time.monotonic()
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        EXECUTION_DURATION_SECONDS.labels(self._operation).observe(time.monotonic() - self._started)
        EXECUTIONS_ACTIVE.labels(self._operation).dec()
        self._limiter._release()


class ExecutionLimiter:
    """Per-replica cap on in-flight executions with a short, bounded wait.

    Uses a lock-protected counter rather than an asyncio primitive so the
    limiter is not bound to one event loop, and slots can be released from
    the worker thread that finishes a streaming response.
    """

    def __init__(self, limit: int, queue_timeout_sec: float) -> None:
        self.limit = limit
        self.queue_timeout_sec = queue_timeout_sec
        self._active = 0
        self._lock = threading.Lock()
        EXECUTIONS_LIMIT.set(limit)

    @property
    def active(self) -> int:
        return self._active

    def _try_acquire(self) -> bool:
        with self._lock:
            if self._active >= self.limit:
                return False
            self._active += 1
            return True

    def _release(self) -> None:
        with self._lock:
            self._active -= 1

    async def acquire(self, operation: str) -> ExecutionSlot | None:
        """Return a slot, or None if none freed up within the queue timeout."""
        start = time.monotonic()
        deadline = start + self.queue_timeout_sec
        while not self._try_acquire():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                ADMISSION_WAIT_SECONDS.labels(operation).observe(time.monotonic() - start)
                return None
            await asyncio.sleep(min(_POLL_INTERVAL_SEC, remaining))
        ADMISSION_WAIT_SECONDS.labels(operation).observe(time.monotonic() - start)
        EXECUTIONS_ACTIVE.labels(operation).inc()
        return ExecutionSlot(self, operation)
