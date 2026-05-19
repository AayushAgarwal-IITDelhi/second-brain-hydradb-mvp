"""
Generic retry-with-exponential-backoff framework for the Second Brain MVP.

Usage (decorator form):

    from retry import retry

    @retry(service="hydradb", max_attempts=3)
    def upload():
        ...

    @retry(service="llm", max_attempts=3)
    async def generate():
        ...

Streaming note:
    Only wrap the *initialisation* call (before first token is emitted).
    Never apply this decorator to a generator that has already started
    yielding — restarting mid-stream produces garbled output.

Structured log lines are emitted to stdout for every retry event:
    {"event": "retry_attempt",   "service": "...", "attempt": N, "delay_seconds": X}
    {"event": "retry_success",   "service": "...", "attempt": N}
    {"event": "retry_exhausted", "service": "...", "attempt": N, "error": "..."}

Default retryable conditions:
    Exceptions: TimeoutError, ConnectionError, OSError
    HTTP status codes: 429, 500, 502, 503, 504

Never retried:
    HTTP status codes: 400, 401, 403, 404, 422
    Exception types: ValueError, TypeError, NonRetryableError
"""

import asyncio
import functools
import inspect
import json
import random
import time
from typing import Any, Callable, FrozenSet, Optional, Tuple, Type

# ---------------------------------------------------------------------------
# Status code classification
# ---------------------------------------------------------------------------
RETRYABLE_STATUS_CODES: FrozenSet[int] = frozenset({429, 500, 502, 503, 504})
NON_RETRYABLE_STATUS_CODES: FrozenSet[int] = frozenset({400, 401, 403, 404, 422})

_DEFAULT_RETRYABLE_EXCEPTIONS: Tuple[Type[Exception], ...] = (
    TimeoutError,
    ConnectionError,
    OSError,
)


# ---------------------------------------------------------------------------
# Public sentinels
# ---------------------------------------------------------------------------
class RetryExhausted(Exception):
    """All retry attempts have been exhausted."""


class NonRetryableError(Exception):
    """Raise to skip remaining retry attempts immediately."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _log(
    event: str,
    service: str,
    attempt: int,
    delay_seconds: float = 0.0,
    error: str = "",
) -> None:
    record: dict = {"event": event, "service": service, "attempt": attempt}
    if delay_seconds:
        record["delay_seconds"] = round(delay_seconds, 3)
    if error:
        record["error"] = error[:300]
    print(json.dumps(record))


def _compute_delay(
    attempt: int,
    initial_delay: float,
    max_delay: float,
    multiplier: float,
    jitter: bool,
) -> float:
    """Exponential backoff with optional ±25 % jitter to spread retry storms."""
    delay = min(initial_delay * (multiplier ** (attempt - 1)), max_delay)
    if jitter:
        delay *= 0.75 + random.random() * 0.5
    return max(0.0, delay)


def _extract_status_code(exc: Exception) -> Optional[int]:
    """
    Try to read an HTTP status code from an exception.

    Checks, in order:
      - exc.upstream_status  (AppError subclasses that carry the upstream HTTP
                              status separately from our own response code)
      - exc.status_code
      - exc.status
      - exc.response.status_code / exc.response.status
    """
    for attr in ("upstream_status", "status_code", "status"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    response = getattr(exc, "response", None)
    if response is not None:
        for attr in ("status_code", "status"):
            val = getattr(response, attr, None)
            if isinstance(val, int):
                return val
    return None


def _should_retry(
    exc: Exception,
    retryable_exceptions: Tuple[Type[Exception], ...],
    retryable_status_codes: FrozenSet[int],
    non_retryable_exceptions: Tuple[Type[Exception], ...],
) -> bool:
    # Hard stops first.
    if isinstance(exc, NonRetryableError):
        return False
    if non_retryable_exceptions and isinstance(exc, non_retryable_exceptions):
        return False
    # Argument / validation errors are programming mistakes, not transient.
    if isinstance(exc, (ValueError, TypeError)):
        return False

    status = _extract_status_code(exc)
    if status is not None:
        if status in NON_RETRYABLE_STATUS_CODES:
            return False
        if status in retryable_status_codes:
            return True

    return isinstance(exc, retryable_exceptions)


# ---------------------------------------------------------------------------
# Public decorator
# ---------------------------------------------------------------------------
def retry(
    max_attempts: int = 3,
    initial_delay: float = 1.0,
    max_delay: float = 30.0,
    exponential_multiplier: float = 2.0,
    jitter: bool = True,
    retryable_exceptions: Tuple[Type[Exception], ...] = _DEFAULT_RETRYABLE_EXCEPTIONS,
    retryable_status_codes: Tuple[int, ...] = tuple(RETRYABLE_STATUS_CODES),
    non_retryable_exceptions: Tuple[Type[Exception], ...] = (),
    service: str = "unknown",
) -> Callable:
    """
    Decorator factory — works transparently on both sync and async functions.

    Default backoff (no jitter):
        attempt 1 fails → sleep 1 s
        attempt 2 fails → sleep 2 s
        attempt 3 fails → raises RetryExhausted

    Jitter (enabled by default) multiplies the computed delay by a random
    factor in [0.75, 1.25] to prevent thundering-herd on coordinated restarts.
    """
    _retryable_codes: FrozenSet[int] = frozenset(retryable_status_codes)

    def decorator(func: Callable) -> Callable:
        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def _async(*args: Any, **kwargs: Any) -> Any:
                for attempt in range(1, max_attempts + 1):
                    try:
                        result = await func(*args, **kwargs)
                        if attempt > 1:
                            _log("retry_success", service, attempt)
                        return result
                    except Exception as exc:
                        if not _should_retry(
                            exc, retryable_exceptions, _retryable_codes,
                            non_retryable_exceptions,
                        ):
                            raise
                        if attempt == max_attempts:
                            _log("retry_exhausted", service, attempt, error=str(exc))
                            raise RetryExhausted(
                                f"[{service}] exhausted {max_attempts} attempts: {exc}"
                            ) from exc
                        delay = _compute_delay(
                            attempt, initial_delay, max_delay,
                            exponential_multiplier, jitter,
                        )
                        _log("retry_attempt", service, attempt,
                             delay_seconds=delay, error=str(exc))
                        await asyncio.sleep(delay)
            return _async

        else:
            @functools.wraps(func)
            def _sync(*args: Any, **kwargs: Any) -> Any:
                for attempt in range(1, max_attempts + 1):
                    try:
                        result = func(*args, **kwargs)
                        if attempt > 1:
                            _log("retry_success", service, attempt)
                        return result
                    except Exception as exc:
                        if not _should_retry(
                            exc, retryable_exceptions, _retryable_codes,
                            non_retryable_exceptions,
                        ):
                            raise
                        if attempt == max_attempts:
                            _log("retry_exhausted", service, attempt, error=str(exc))
                            raise RetryExhausted(
                                f"[{service}] exhausted {max_attempts} attempts: {exc}"
                            ) from exc
                        delay = _compute_delay(
                            attempt, initial_delay, max_delay,
                            exponential_multiplier, jitter,
                        )
                        _log("retry_attempt", service, attempt,
                             delay_seconds=delay, error=str(exc))
                        time.sleep(delay)
            return _sync

    return decorator
