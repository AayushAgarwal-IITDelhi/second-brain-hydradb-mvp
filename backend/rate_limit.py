"""
Per-process in-memory rate limiter for the Second Brain MVP.

Tracks request timestamps per client identifier (the X-API-Key header, or
the client IP if the header isn't present) over a 5-minute sliding window.
When a client exceeds RATE_LIMIT_PER_5_MIN, we raise RateLimitedError
which the global error handler turns into HTTP 429.

Trade-offs:
- In-memory: state resets on restart and isn't shared across workers.
  Fine for a single-instance MVP; swap for Redis if you scale out.
- 5-minute window matches the env-var name; configurable below if needed.
"""

import os
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict

from fastapi import Request

from errors import RateLimitedError


WINDOW_SECONDS = 5 * 60  # 5 minutes


def _limit_per_window() -> int:
    """Read the limit from env each request so it can be changed live."""
    try:
        return max(1, int(os.getenv("RATE_LIMIT_PER_5_MIN", "100")))
    except ValueError:
        return 100


def _client_id_from(request: Request) -> str:
    """
    Identify the client. Prefer the X-API-Key header so a shared bot
    doesn't slow each individual user — fall back to remote IP otherwise.
    """
    key = request.headers.get("x-api-key")
    if key:
        return f"key:{key}"
    # request.client can be None during testing.
    ip = request.client.host if request.client else "unknown"
    return f"ip:{ip}"


class _SlidingWindowLimiter:
    """Maintains a deque of timestamps per client and trims by window."""

    def __init__(self) -> None:
        self._buckets: Dict[str, Deque[float]] = {}
        self._lock = Lock()

    def hit(self, client_id: str, limit: int) -> None:
        """
        Record a request. Raise RateLimitedError if the client would
        exceed the limit. Otherwise return None.
        """
        now = time.monotonic()
        cutoff = now - WINDOW_SECONDS

        with self._lock:
            bucket = self._buckets.get(client_id)
            if bucket is None:
                bucket = deque()
                self._buckets[client_id] = bucket
            # Drop timestamps that have fallen out of the window.
            while bucket and bucket[0] < cutoff:
                bucket.popleft()

            if len(bucket) >= limit:
                # Compute when the oldest request will expire, so the
                # response can give a useful "try again in N seconds" hint.
                retry_after = max(1, int(bucket[0] + WINDOW_SECONDS - now))
                raise RateLimitedError(
                    detail=(
                        f"Too many requests (limit {limit} per 5 minutes). "
                        f"Try again in about {retry_after} seconds."
                    ),
                    log_context=f"client={client_id} count={len(bucket)} limit={limit}",
                )

            bucket.append(now)


_limiter = _SlidingWindowLimiter()


def rate_limit_dependency(request: Request) -> None:
    """FastAPI dependency. Apply on any route you want rate-limited."""
    client_id = _client_id_from(request)
    limit = _limit_per_window()
    _limiter.hit(client_id, limit)