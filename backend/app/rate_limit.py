import time
from dataclasses import dataclass
from threading import RLock


@dataclass
class AttemptBucket:
    failures: int
    reset_at: float


class FixedWindowRateLimiter:
    def __init__(self, max_failures: int, window_seconds: int):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
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
                self._buckets[key] = AttemptBucket(
                    failures=1,
                    reset_at=now + self.window_seconds,
                )
                return
            bucket.failures += 1

    def reset(self, key: str) -> None:
        with self._lock:
            self._buckets.pop(key, None)
