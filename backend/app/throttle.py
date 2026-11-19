"""Stay inside CORE's real request budget: about 10 requests a minute.

CORE's headers advertise 10 requests per short window, but in practice we were
answered 429 after about 10 calls in about 60 s, and the penalty lasted up to
a minute (measured 2026-11-13). So every outgoing CORE call takes a slot in a
sliding one-minute window first (default 9: one under CORE's budget, so the two
windows never disagree by a request). With no slot free, the call fails fast
with CoreRateLimited and the seconds until a slot frees up. It does not queue,
so no gunicorn thread sits waiting. The cache still serves stale or partial
rows when it has them, and the route turns the rest into a 429 with
Retry-After.

If CORE answers 429 anyway (another client on the same IP, or a different
window), no call goes out for PENALTY seconds.

The window lives in process memory. That fits the single gunicorn worker this
app runs; several workers or hosts would need a shared store such as Redis.
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable

from .core_client import CoreRateLimited

DEFAULT_PER_MINUTE = 9
WINDOW = 60.0  # seconds
PENALTY = 30.0  # seconds of silence after a real 429 from CORE


class Throttle:
    def __init__(self, per_minute: int = DEFAULT_PER_MINUTE, *, clock: Callable[[], float] = time.time):
        self.per_minute = per_minute  # 0 turns the throttle off
        self.clock = clock
        self._sent: deque[float] = deque()
        self._quiet_until = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Take a slot and return 0, or take nothing and return the seconds until a slot frees up."""
        if self.per_minute <= 0:
            return 0.0
        with self._lock:
            now = self.clock()
            while self._sent and now - self._sent[0] >= WINDOW:
                self._sent.popleft()
            if now < self._quiet_until:
                return self._quiet_until - now
            if len(self._sent) >= self.per_minute:
                return self._sent[0] + WINDOW - now
            self._sent.append(now)
            return 0.0

    def penalize(self, until: float | None = None) -> float:
        """CORE said 429: send nothing until `until`, and for at least PENALTY seconds. Returns the end."""
        with self._lock:
            self._quiet_until = max(self._quiet_until, self.clock() + PENALTY, until or 0)
            return self._quiet_until


class ThrottledCore:
    """CoreClient's search/aggregate, each one taking a throttle slot first."""

    def __init__(self, core, throttle: Throttle):
        self.core = core
        self.throttle = throttle

    def search(self, *args, **kwargs):
        return self._send(self.core.search, *args, **kwargs)

    def aggregate(self, *args, **kwargs):
        return self._send(self.core.aggregate, *args, **kwargs)

    def _send(self, call, *args, **kwargs):
        wait = self.throttle.acquire()
        if wait > 0:
            resets_at = math.ceil(self.throttle.clock() + wait)
            raise CoreRateLimited("held back to stay inside CORE's rate limit", resets_at=resets_at)
        try:
            return call(*args, **kwargs)
        except CoreRateLimited as exc:
            if self.throttle.per_minute <= 0:
                raise
            # tell the user the real wait (our penalty), not CORE's optimistic reset header
            until = math.ceil(self.throttle.penalize(exc.resets_at))
            raise CoreRateLimited(str(exc), resets_at=until, status=exc.status, rate=exc.rate) from exc
