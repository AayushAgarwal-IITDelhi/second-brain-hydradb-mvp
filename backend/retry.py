"""
Phase 7: retry helper with exponential backoff for background jobs.

Used by:
  - slack_oauth.run_workspace_ingest  (Slack + HydraDB I/O)
  - realtime_ingest._process_slack_payload_inner (Slack + HydraDB I/O)
  - scheduler.run_all_workspaces_once (orchestration -- per-workspace
    retries happen INSIDE this function, not around it)

Design choices:
  - No third-party dependency. tenacity would be heavier than warranted
    for a single retry helper used in two places.
  - Caller passes a list of EXPECTED exception classes; everything else
    propagates immediately. This avoids retrying programming errors
    (TypeError, KeyError, etc.) which won't resolve themselves.
  - Final failure raises the LAST captured exception so the caller's
    own try/except can log and dead-letter it.
  - `on_giveup` callback lets the caller hook in observability without
    coupling this module to dead-letter logging.
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable, Optional, Tuple, Type, TypeVar

from logging_config import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


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
    with exponential backoff and try again, up to `attempts` total
    tries.

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
                        # Don't let an observability callback hide the
                        # actual operation failure.
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

            # Jittered exponential backoff. We compute the NEXT delay
            # after sleeping so the first retry waits `initial_delay`.
            jitter_offset = random.uniform(-jitter, jitter) * delay
            time.sleep(max(0.0, delay + jitter_offset))
            delay = min(max_delay, delay * backoff_factor)

    # Unreachable in practice (the loop either returns or raises) but
    # makes type checkers happy.
    assert last_err is not None
    raise last_err