"""Abuse detection: sliding-window rate limiter + escalating abuse score per IP."""
from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    """Token-bucket-free sliding window counter, thread-safe."""

    def __init__(self, limit_per_minute: int):
        self.limit = max(1, limit_per_minute)
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            dq = self._hits.setdefault(key, deque())
            while dq and now - dq[0] > 60.0:
                dq.popleft()
            if len(dq) >= self.limit:
                return False, len(dq)
            dq.append(now)
            return True, len(dq)

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


class AbuseDetector:
    """Tracks security findings over time; repeat offenders get throttled harder.

    Score decays exponentially; when a client crosses thresholds it is placed
    on a soft-block list for a cooling-off period (progressive punishment).
    """

    SOFT_BLOCK_THRESHOLD = 12.0
    HARD_BLOCK_THRESHOLD = 30.0

    def __init__(self):
        self._scores: dict[str, float] = {}
        self._blocked_until: dict[str, float] = {}
        self._last_seen: dict[str, float] = {}
        self._lock = threading.Lock()

    _WEIGHTS = {"critical": 6.0, "high": 4.0, "medium": 2.0, "low": 0.5}

    def report(self, key: str, severity: str) -> None:
        weight = self._WEIGHTS.get(severity, 1.0)
        now = time.monotonic()
        with self._lock:
            self._decay_locked(key, now)
            self._scores[key] = self._scores.get(key, 0.0) + weight
            self._last_seen[key] = now
            score = self._scores[key]
            if score >= self.HARD_BLOCK_THRESHOLD:
                self._blocked_until[key] = now + 900.0   # 15 min
            elif score >= self.SOFT_BLOCK_THRESHOLD:
                self._blocked_until[key] = max(self._blocked_until.get(key, 0), now + 180.0)

    def is_blocked(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            return self._blocked_until.get(key, 0.0) > now

    def score(self, key: str) -> float:
        now = time.monotonic()
        with self._lock:
            self._decay_locked(key, now)
            return round(self._scores.get(key, 0.0), 2)

    def _decay_locked(self, key: str, now: float) -> None:
        last = self._last_seen.get(key)
        if last is None:
            return
        elapsed = now - last
        half_life = 600.0  # 10 min half-life
        self._scores[key] = self._scores.get(key, 0.0) * (0.5 ** (elapsed / half_life))
        self._last_seen[key] = now

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return {k: round(v, 2) for k, v in self._scores.items() if v > 0.1}


rate_limiter = RateLimiter(60)
abuse_detector = AbuseDetector()
