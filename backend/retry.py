"""
Retry utilities for the Second Brain backend.

This module provides two complementary retry APIs:

1. `retry` (decorator factory) — our side's addition.
   Works transparently on both sync and async functions.
   Structured log lines are emitted for every retry event.
   Default retryable conditions: TimeoutError, ConnectionError, OSError
   and HTTP status codes 429, 500, 502, 503, 504.

2. `retry_with_backoff` (function call) — upstream's addition (Phase 7).
   Used by slack_oauth.run_workspace_ingest, realtime_ingest, and scheduler.
   No third-party dependency. Caller passes a list of EXPECTED exception
   classes; everything else propagates immediately.

Both share the same jittered exponential backoff design.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import random
import time
from typing import Any, Callable, FrozenSet, Optional, Tuple, Type, TypeVar

from logging_config import get_logger as _get_logger

_logger = _get_logger(__name__)

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
# Internal helpers (shared by both APIs)
# ---------------------------------------------------------------------------
def _log(
    event: str,
    service: str,
    attempt: int,
    delay_seconds: float = 0.0,
    error: str = "",
) -> None:
    extra: dict = {"service": service, "attempt": attempt}
    if delay_seconds:
        extra["delay_seconds"] = round(delay_seconds, 3)
    if error:
        extra["error"] = error[:300]
    _logger.info(event, extra=extra)


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
# API 1: decorator factory (our side)
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
                            exc,
                            retryable_exceptions,
                            _retryable_codes,
                            non_retryable_exceptions,
                        ):
                            raise
                        if attempt == max_attempts:
                            _log("retry_exhausted", service, attempt, error=str(exc))
                            raise RetryExhausted(f"[{service}] exhausted {max_attempts} attempts: {exc}") from exc
                        delay = _compute_delay(
                            attempt,
                            initial_delay,
                            max_delay,
                            exponential_multiplier,
                            jitter,
                        )
                        _log("retry_attempt", service, attempt, delay_seconds=delay, error=str(exc))
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
                            exc,
                            retryable_exceptions,
                            _retryable_codes,
                            non_retryable_exceptions,
                        ):
                            raise
                        if attempt == max_attempts:
                            _log("retry_exhausted", service, attempt, error=str(exc))
                            raise RetryExhausted(f"[{service}] exhausted {max_attempts} attempts: {exc}") from exc
                        delay = _compute_delay(
                            attempt,
                            initial_delay,
                            max_delay,
                            exponential_multiplier,
                            jitter,
                        )
                        _log("retry_attempt", service, attempt, delay_seconds=delay, error=str(exc))
                        time.sleep(delay)

            return _sync

    return decorator


# ---------------------------------------------------------------------------
# API 2: retry_with_backoff (upstream Phase 7)
# ---------------------------------------------------------------------------
T = TypeVar("T")

logger = _get_logger(__name__)


def retry_with_backoff(
    fn: Callable[..., T],
    *args: Any,
    attempts: int = 3,
    initial_delay: float = 0.5,
    max_delay: float = 8.0,
    backoff_factor: float = 2.0,
    jitter: float = 0.25,
    retry_on: Tuple[Type[BaseException], ...] = (Exception,),
    on_attempt_failure: Optional[Callable[[int, BaseException], None]] = None,
    on_giveup: Optional[Callable[[BaseException], None]] = None,
    op_name: str = "operation",
    **kwargs: Any,
) -> T:
    """
    Call `fn(*args, **kwargs)`. If it raises one of `retry_on`, sleep
    with exponential backoff and try again, up to `attempts` total tries.

    Args:
      attempts:       total number of attempts including the first (>= 1).
      initial_delay:  seconds to sleep before the SECOND attempt.
      max_delay:      ceiling for the sleep between attempts.
      backoff_factor: multiplier applied each retry (delay *= factor).
      jitter:         +/- fraction of the delay to randomize over,
                      so retries from many workers don't synchronize.
      retry_on:       exception classes that trigger a retry; everything
                      else propagates on the first attempt.
      on_attempt_failure(attempt, err): called per failed attempt that
                      isn't the final one. attempt is 1-indexed.
      on_giveup(err): called once if we exhaust attempts. The same
                      exception is then re-raised to the caller.

    Returns the function's return value on success. Re-raises the LAST
    captured exception on giveup.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    last_err: Optional[BaseException] = None
    delay = initial_delay

    for attempt in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except retry_on as e:
            last_err = e
            if attempt >= attempts:
                logger.warning(
                    "retry_giveup",
                    extra={
                        "op_name":  op_name,
                        "attempts": attempts,
                        "error":    type(e).__name__,
                    },
                )
                if on_giveup is not None:
                    try:
                        on_giveup(e)
                    except Exception as cb_err:  # noqa: BLE001
                        logger.warning(
                            "retry_on_giveup_callback_failed",
                            extra={"error": type(cb_err).__name__},
                        )
                raise

            # Mid-stream failure: log, optionally notify, sleep, retry.
            logger.info(
                "retry_attempt_failed",
                extra={
                    "op_name": op_name,
                    "attempt": attempt,
                    "of":      attempts,
                    "error":   type(e).__name__,
                    "next_delay_s": round(delay, 3),
                },
            )
            if on_attempt_failure is not None:
                try:
                    on_attempt_failure(attempt, e)
                except Exception as cb_err:  # noqa: BLE001
                    logger.warning(
                        "retry_on_attempt_failure_callback_failed",
                        extra={"error": type(cb_err).__name__},
                    )

            # Jittered exponential backoff.
            jitter_offset = random.uniform(-jitter, jitter) * delay
            time.sleep(max(0.0, delay + jitter_offset))
            delay = min(max_delay, delay * backoff_factor)

    # Unreachable in practice (the loop either returns or raises) but
    # makes type checkers happy.
    assert last_err is not None
    raise last_err
