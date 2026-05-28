"""
Optional background ingestion for the Second Brain MVP.

Phase 4 update: workspace-isolated ingestion.

If AUTO_INGEST=true in the environment, FastAPI startup spins up an
APScheduler BackgroundScheduler that runs a per-workspace ingest pass
every AUTO_INGEST_INTERVAL_MINUTES. The scheduler:

  1. Pulls every active workspace with a Slack installation from
     Supabase (see supabase_client.list_active_workspaces_with_slack).
  2. For each workspace, calls slack_oauth.run_workspace_ingest with
     that workspace's bot_token, selected channel_ids, and HydraDB
     sub_tenant_id — so workspaces never share a HydraDB bucket.
  3. On a successful pass (no failures across channels), stamps
     hydradb_last_sync_at on the workspace row so operators can see
     which workspaces are warm.

Failures in one workspace MUST NOT cancel the pass for other
workspaces. Each per-workspace call is wrapped in its own try/except,
the error is logged, and the loop continues.

Notes:
- Uses an in-process scheduler. State (next-run time, last-run time) is
  not persisted across restarts, which is fine for a single-instance MVP.
- The job runs synchronously inside the scheduler's worker thread. A run
  that takes longer than the interval will not overlap with itself
  because we set max_instances=1.
- We DO NOT auto-run on startup. The first run happens after the
  configured interval, so a freshly-started server doesn't immediately
  hammer Slack / HydraDB. Set AUTO_INGEST_RUN_ON_STARTUP=true to override.
- The legacy global ingestion entry point (ingestion.ingest_slack.main)
  is still importable and still works for ad-hoc CLI runs, but the
  scheduler no longer uses it -- calling it would write into the env
  default sub-tenant, defeating workspace isolation.
"""

import os
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler

from logging_config import get_logger
from slack_oauth import run_workspace_ingest
from supabase_client import (
    list_active_workspaces_with_slack,
    mark_workspace_synced,
)

logger = get_logger(__name__)


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


def run_all_workspaces_once() -> dict:
    """
    Run one ingestion pass across every active workspace that has Slack
    connected. Returns a small summary dict -- useful for tests and for
    callers that want to invoke a pass manually (e.g. from a future
    admin endpoint).

    Per-workspace errors are caught and logged so a single bad token
    or a single dead channel never aborts the whole sweep.
    """
    workspaces = list_active_workspaces_with_slack()
    summary = {
        "workspaces_total":   len(workspaces),
        "workspaces_run":     0,
        "workspaces_skipped": 0,
        "workspaces_failed":  0,
    }
    if not workspaces:
        logger.info("scheduler_no_workspaces")
        return summary

    for ws in workspaces:
        workspace_id = ws.get("workspace_id") or ""
        bot_token = ws.get("bot_token") or ""
        channel_ids = ws.get("channel_ids") or []
        sub_tenant = (ws.get("hydradb_sub_tenant_id") or "").strip()

        if not channel_ids:
            # Connected but nothing selected -- nothing to ingest yet.
            summary["workspaces_skipped"] += 1
            logger.info(
                "scheduler_skip_no_channels",
                extra={"workspace_id": workspace_id},
            )
            continue

        if not sub_tenant:
            # Defensive: every workspace should have a sub_tenant_id
            # after the Phase 4 migration backfill. If we somehow see
            # one without, refuse rather than route to the global
            # bucket.
            summary["workspaces_skipped"] += 1
            logger.warning(
                "scheduler_skip_no_sub_tenant",
                extra={"workspace_id": workspace_id},
            )
            continue

        try:
            result = run_workspace_ingest(
                workspace_id=workspace_id,
                bot_token=bot_token,
                channel_ids=channel_ids,
                hydradb_sub_tenant_id=sub_tenant,
            )
        except Exception as e:  # noqa: BLE001
            summary["workspaces_failed"] += 1
            logger.error(
                "scheduler_workspace_failed",
                extra={
                    "workspace_id": workspace_id,
                    "error":        type(e).__name__,
                },
                exc_info=True,
            )
            continue

        summary["workspaces_run"] += 1
        # Stamp last-synced only when the run finished cleanly. A
        # partial failure (some channels OK, others bad) still counts --
        # the alternative would mean we never mark a workspace as
        # synced if a single bad channel persists.
        if result.get("failures", 0) == 0:
            mark_workspace_synced(workspace_id=workspace_id)

    logger.info("scheduler_pass_complete", extra=summary)
    return summary


def _job_wrapper() -> None:
    """
    Run one ingestion pass. Swallow exceptions so a single failure
    never kills the scheduler thread -- APScheduler would otherwise
    stop scheduling the job.
    """
    started_at = datetime.now(timezone.utc).isoformat()
    logger.info("scheduler_job_start", extra={"started_at": started_at})
    try:
        run_all_workspaces_once()
        logger.info("scheduler_job_finished")
    except Exception as e:  # noqa: BLE001
        logger.error(
            "scheduler_job_failed",
            extra={"error": type(e).__name__},
            exc_info=True,
        )


_scheduler: Optional[BackgroundScheduler] = None


def start_scheduler() -> None:
    """Start the scheduler if AUTO_INGEST is enabled."""
    global _scheduler
    if not auto_ingest_enabled():
        logger.info("scheduler_disabled", extra={"reason": "AUTO_INGEST=false"})
        return

    if _scheduler is not None:
        logger.info("scheduler_already_started")
        return

    minutes = interval_minutes()
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        _job_wrapper,
        trigger="interval",
        minutes=minutes,
        id=JOB_ID,
        max_instances=1,        # never run two ingestions in parallel
        coalesce=True,          # if we fall behind, run once not N times
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("scheduler_started", extra={"interval_minutes": minutes})

    if run_on_startup_enabled():
        logger.info("scheduler_startup_run_queued")
        _scheduler.add_job(_job_wrapper, id=f"{JOB_ID}-startup")


def stop_scheduler() -> None:
    """Stop the scheduler cleanly on shutdown."""
    global _scheduler
    if _scheduler is None:
        return
    logger.info("scheduler_stopping")
    _scheduler.shutdown(wait=False)
    _scheduler = None