import math
import time
from dataclasses import dataclass
from threading import RLock


@dataclass
class AttemptBucket:
    failures: int
    reset_at: float


class FixedWindowRateLimiter:
    def __init__(self, max_failures: int, window_seconds: int, max_buckets: int = 4096):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.max_buckets = max_buckets
        self._buckets: dict[str, AttemptBucket] = {}
        self._lock = RLock()

    def is_limited(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            bucket = self._buckets.get(key)
            if not bucket:
                return False
            if bucket.reset_at <= now:
                self._buckets.pop(key, None)
                return False
            return bucket.failures >= self.max_failures

    def record_failure(self, key: str) -> None:
        now = time.time()
        with self._lock:
            bucket = self._buckets.get(key)
            if not bucket or bucket.reset_at <= now:
                self._make_room(now)
                self._buckets[key] = AttemptBucket(
                    failures=1,
                    reset_at=now + self.window_seconds,
                )
                return
            bucket.failures += 1

    def retry_after(self, key: str) -> int:
        now = time.time()
        with self._lock:
            bucket = self._buckets.get(key)
            if not bucket or bucket.reset_at <= now or bucket.failures < self.max_failures:
                return 0
            return max(1, math.ceil(bucket.reset_at - now))

    def reset(self, key: str) -> None:
        with self._lock:
            self._buckets.pop(key, None)

    def _make_room(self, now: float) -> None:
        if len(self._buckets) < self.max_buckets:
            return
        expired = [key for key, bucket in self._buckets.items() if bucket.reset_at <= now]
        for key in expired:
            self._buckets.pop(key, None)
        if len(self._buckets) >= self.max_buckets:
            oldest_key = min(self._buckets, key=lambda key: self._buckets[key].reset_at)
            self._buckets.pop(oldest_key, None)
