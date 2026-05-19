"""
Optional background ingestion for the Second Brain MVP.

If AUTO_INGEST=true in the environment, FastAPI startup spins up an
APScheduler BackgroundScheduler that runs the same ingestion entry
point (ingestion.ingest_slack.main) every AUTO_INGEST_INTERVAL_MINUTES.

Notes:
- Uses an in-process scheduler. State (next-run time, last-run time) is
  not persisted across restarts, which is fine for a single-instance MVP.
- The job runs synchronously inside the scheduler's worker thread. A run
  that takes longer than the interval will not overlap with itself
  because we set max_instances=1.
- We DO NOT auto-run on startup. The first run happens after the
  configured interval, so a freshly-started server doesn't immediately
  hammer Slack / HydraDB. Set AUTO_INGEST_RUN_ON_STARTUP=true to override.
"""

import os
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler

from ingestion.ingest_slack import main as run_ingestion
from logging_config import get_logger
from retry import RetryExhausted, retry

logger = get_logger(__name__)


# Wrap ingestion with retry so transient network blips don't abort the whole
# run.  Delays are generous (5 s / 60 s) because ingestion is a background
# batch job, not a user-facing request.
_run_ingestion_with_retry = retry(
    service="scheduler",
    max_attempts=3,
    initial_delay=5.0,
    max_delay=60.0,
    retryable_exceptions=(ConnectionError, TimeoutError, OSError),
)(run_ingestion)


JOB_ID = "second-brain-ingestion"


def auto_ingest_enabled() -> bool:
    return os.getenv("AUTO_INGEST", "").strip().lower() in ("1", "true", "yes", "on")


def run_on_startup_enabled() -> bool:
    return os.getenv("AUTO_INGEST_RUN_ON_STARTUP", "").strip().lower() in ("1", "true", "yes", "on")


def interval_minutes() -> int:
    """Read the configured interval, with a sensible floor."""
    try:
        value = int(os.getenv("AUTO_INGEST_INTERVAL_MINUTES", "15"))
    except ValueError:
        value = 15
    # Floor at 1 minute so a typo can't accidentally hammer Slack.
    return max(1, value)


def _job_wrapper() -> None:
    """
    Run one ingestion pass. We swallow exceptions here so a single failure
    never kills the scheduler thread — APScheduler would otherwise
    stop scheduling the job.
    """
    started_at = datetime.now(timezone.utc).isoformat()
    logger.info('scheduler_job_start', extra={'started_at': started_at})
    try:
        _run_ingestion_with_retry()
        logger.info('scheduler_job_finished')
    except SystemExit as e:
        logger.warning('scheduler_job_sys_exit', extra={'exit_code': e.code})
    except RetryExhausted as e:
        logger.error('scheduler_job_retry_exhausted', extra={'error': str(e)})
    except Exception as e:  # noqa: BLE001
        logger.error('scheduler_job_failed', extra={'error': type(e).__name__}, exc_info=True)


_scheduler: Optional[BackgroundScheduler] = None


def start_scheduler() -> None:
    """Start the scheduler if AUTO_INGEST is enabled."""
    global _scheduler
    if not auto_ingest_enabled():
        logger.info('scheduler_disabled', extra={'reason': 'AUTO_INGEST=false'})
        return

    if _scheduler is not None:
        logger.info('scheduler_already_started')
        return

    minutes = interval_minutes()
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        _job_wrapper,
        trigger="interval",
        minutes=minutes,
        id=JOB_ID,
        max_instances=1,  # never run two ingestions in parallel
        coalesce=True,  # if we fall behind, run once not N times
        replace_existing=True,
    )
    _scheduler.start()
    logger.info('scheduler_started', extra={'interval_minutes': minutes})

    if run_on_startup_enabled():
        logger.info('scheduler_startup_run_queued')
        _scheduler.add_job(_job_wrapper, id=f"{JOB_ID}-startup")


def stop_scheduler() -> None:
    """Stop the scheduler cleanly on shutdown."""
    global _scheduler
    if _scheduler is None:
        return
    logger.info('scheduler_stopping')
    _scheduler.shutdown(wait=False)
    _scheduler = None
