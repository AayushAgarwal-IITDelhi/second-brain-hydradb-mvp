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
import traceback
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler

from ingestion.ingest_slack import main as run_ingestion
from retry import retry, RetryExhausted


# Wrap ingestion with retry so transient network blips don't abort the whole
# run.  Delays are generous (5 s / 15 s) because ingestion is a background
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
    return os.getenv("AUTO_INGEST", "").strip().lower() in (
        "1", "true", "yes", "on"
    )


def run_on_startup_enabled() -> bool:
    return os.getenv("AUTO_INGEST_RUN_ON_STARTUP", "").strip().lower() in (
        "1", "true", "yes", "on"
    )


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
    started_at = datetime.utcnow().isoformat()
    print(f"[scheduler] Ingestion job starting at {started_at} UTC")
    try:
        _run_ingestion_with_retry()
        print("[scheduler] Ingestion job finished.")
    except SystemExit as e:
        # ingest_slack.main calls sys.exit(1) when SLACK_CHANNEL_IDS is
        # unset. Log it but keep the scheduler alive.
        print(f"[scheduler] Ingestion exited with code {e.code}.")
    except RetryExhausted as e:
        print(f"[scheduler] Ingestion exhausted retries: {e}")
    except Exception as e:  # noqa: BLE001
        print(f"[scheduler] Ingestion job failed: {type(e).__name__}: {e}")
        traceback.print_exc()


_scheduler: Optional[BackgroundScheduler] = None


def start_scheduler() -> None:
    """Start the scheduler if AUTO_INGEST is enabled."""
    global _scheduler
    if not auto_ingest_enabled():
        print("[scheduler] AUTO_INGEST is off; not starting.")
        return

    if _scheduler is not None:
        print("[scheduler] Already started.")
        return

    minutes = interval_minutes()
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        _job_wrapper,
        trigger="interval",
        minutes=minutes,
        id=JOB_ID,
        max_instances=1,        # never run two ingestions in parallel
        coalesce=True,           # if we fall behind, run once not N times
        replace_existing=True,
    )
    _scheduler.start()
    print(
        f"[scheduler] Started. Ingestion will run every {minutes} minute(s)."
    )

    if run_on_startup_enabled():
        print("[scheduler] AUTO_INGEST_RUN_ON_STARTUP=true; queueing one run now.")
        # APScheduler runs add_job-with-no-trigger immediately on a thread.
        _scheduler.add_job(_job_wrapper, id=f"{JOB_ID}-startup")


def stop_scheduler() -> None:
    """Stop the scheduler cleanly on shutdown."""
    global _scheduler
    if _scheduler is None:
        return
    print("[scheduler] Shutting down.")
    _scheduler.shutdown(wait=False)
    _scheduler = None