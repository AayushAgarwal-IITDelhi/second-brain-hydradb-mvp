"""Tests for scheduler.py -- APScheduler background ingestion.

Phase 4 update: the scheduler iterates workspaces (each with its own
HydraDB sub_tenant_id) rather than calling a single global ingestion
entry point. Tests target run_all_workspaces_once -- the per-workspace
sweep that the background job invokes -- so the env/state tests stay
identical and the workspace-isolation tests sit next to them.
"""

import os
from unittest.mock import MagicMock, patch

import pytest


class TestAutoIngestEnabled:
    @pytest.mark.parametrize("val", ["true", "1", "yes", "on", "True", "YES"])
    def test_truthy_values(self, val):
        from scheduler import auto_ingest_enabled

        with patch.dict(os.environ, {"AUTO_INGEST": val}):
            assert auto_ingest_enabled() is True

    @pytest.mark.parametrize("val", ["false", "0", "no", "off", "", "False"])
    def test_falsy_values(self, val):
        from scheduler import auto_ingest_enabled

        with patch.dict(os.environ, {"AUTO_INGEST": val}):
            assert auto_ingest_enabled() is False


class TestRunOnStartupEnabled:
    def test_true(self):
        from scheduler import run_on_startup_enabled

        with patch.dict(os.environ, {"AUTO_INGEST_RUN_ON_STARTUP": "true"}):
            assert run_on_startup_enabled() is True

    def test_false(self):
        from scheduler import run_on_startup_enabled

        with patch.dict(os.environ, {"AUTO_INGEST_RUN_ON_STARTUP": "false"}):
            assert run_on_startup_enabled() is False


class TestIntervalMinutes:
    def test_default_is_15(self):
        from scheduler import interval_minutes

        with patch.dict(os.environ, {"AUTO_INGEST_INTERVAL_MINUTES": "15"}):
            assert interval_minutes() == 15

    def test_bad_value_returns_15(self):
        from scheduler import interval_minutes

        with patch.dict(os.environ, {"AUTO_INGEST_INTERVAL_MINUTES": "bad"}):
            assert interval_minutes() == 15

    def test_floor_at_1(self):
        from scheduler import interval_minutes

        with patch.dict(os.environ, {"AUTO_INGEST_INTERVAL_MINUTES": "0"}):
            assert interval_minutes() == 1

    def test_negative_floors_to_1(self):
        from scheduler import interval_minutes

        with patch.dict(os.environ, {"AUTO_INGEST_INTERVAL_MINUTES": "-5"}):
            assert interval_minutes() == 1


class TestStartStopScheduler:
    def test_start_scheduler_when_disabled_does_nothing(self):
        import scheduler as sched_module

        original = sched_module._scheduler
        sched_module._scheduler = None
        try:
            with patch.dict(os.environ, {"AUTO_INGEST": "false"}):
                sched_module.start_scheduler()
            assert sched_module._scheduler is None
        finally:
            sched_module._scheduler = original

    def test_start_scheduler_when_enabled_creates_scheduler(self):
        import scheduler as sched_module

        original = sched_module._scheduler
        sched_module._scheduler = None
        try:
            mock_scheduler = MagicMock()
            with patch.dict(
                os.environ,
                {
                    "AUTO_INGEST": "true",
                    "AUTO_INGEST_RUN_ON_STARTUP": "false",
                    "AUTO_INGEST_INTERVAL_MINUTES": "15",
                },
            ):
                with patch(
                    "scheduler.BackgroundScheduler",
                    return_value=mock_scheduler,
                ):
                    # No need to patch the actual ingestion runner here --
                    # start_scheduler only WIRES the job; it doesn't fire
                    # it. The job wrapper has its own tests below.
                    sched_module.start_scheduler()
            assert mock_scheduler.start.called
        finally:
            sched_module._scheduler = original

    def test_stop_scheduler_when_none_is_noop(self):
        import scheduler as sched_module

        original = sched_module._scheduler
        sched_module._scheduler = None
        try:
            sched_module.stop_scheduler()  # should not raise
        finally:
            sched_module._scheduler = original

    def test_stop_scheduler_shuts_down(self):
        import scheduler as sched_module

        original = sched_module._scheduler
        mock_sched = MagicMock()
        sched_module._scheduler = mock_sched
        try:
            sched_module.stop_scheduler()
            mock_sched.shutdown.assert_called_once_with(wait=False)
            assert sched_module._scheduler is None
        finally:
            sched_module._scheduler = original

    def test_start_scheduler_not_started_twice(self):
        import scheduler as sched_module

        original = sched_module._scheduler
        mock_existing = MagicMock()
        sched_module._scheduler = mock_existing
        try:
            with patch.dict(os.environ, {"AUTO_INGEST": "true"}):
                sched_module.start_scheduler()
            # Should not create a new scheduler
            assert sched_module._scheduler is mock_existing
        finally:
            sched_module._scheduler = original


class TestJobWrapper:
    def test_job_wrapper_swallows_exceptions(self):
        """A crash in the sweep must not propagate."""
        from scheduler import _job_wrapper

        with patch(
            "scheduler.run_all_workspaces_once",
            side_effect=RuntimeError("boom"),
        ):
            _job_wrapper()  # should not raise

    def test_job_wrapper_calls_sweep(self):
        from scheduler import _job_wrapper

        with patch(
            "scheduler.run_all_workspaces_once",
            return_value={
                "workspaces_total": 0,
                "workspaces_run": 0,
                "workspaces_skipped": 0,
                "workspaces_failed": 0,
            },
        ) as mock_sweep:
            _job_wrapper()
            mock_sweep.assert_called_once()


class TestRunAllWorkspacesOnce:
    """Phase 4: workspace-isolated sweep."""

    def test_no_workspaces_returns_zero_counts(self):
        from scheduler import run_all_workspaces_once

        with patch(
            "scheduler.list_active_workspaces_with_slack",
            return_value=[],
        ), patch(
            "scheduler.list_active_workspaces_with_gmail",
            return_value=[],
        ):
            summary = run_all_workspaces_once()
        # Slack-shaped legacy keys unchanged from Phase 4.
        assert summary["workspaces_total"] == 0
        assert summary["workspaces_run"] == 0
        assert summary["workspaces_skipped"] == 0
        assert summary["workspaces_failed"] == 0
        # Phase 11: Gmail summary lives under its own key so Slack
        # consumers don't see new fields appearing in their loop.
        assert "gmail" in summary
        assert summary["gmail"]["connections_total"] == 0
        assert summary["gmail"]["connections_run"] == 0
        assert summary["gmail"]["connections_failed"] == 0

    def test_routes_each_workspace_to_its_own_sub_tenant(self):
        """
        Two workspaces, two distinct sub_tenant_ids. The runner must be
        called twice with DIFFERENT hydradb_sub_tenant_id values --
        that's the entire point of Phase 4.
        """
        from scheduler import run_all_workspaces_once

        wss = [
            {
                "workspace_id": "ws-1",
                "hydradb_sub_tenant_id": "ws_aaaaaaaaaaaa",
                "bot_token": "xoxb-1",
                "channel_ids": ["C1", "C2"],
            },
            {
                "workspace_id": "ws-2",
                "hydradb_sub_tenant_id": "ws_bbbbbbbbbbbb",
                "bot_token": "xoxb-2",
                "channel_ids": ["C3"],
            },
        ]
        with patch(
            "scheduler.list_active_workspaces_with_slack",
            return_value=wss,
        ), patch(
            "scheduler.run_workspace_ingest",
            return_value={"failures": 0},
        ) as mock_run, patch(
            "scheduler.mark_workspace_synced",
            return_value=True,
        ) as mock_mark:
            summary = run_all_workspaces_once()

        assert summary["workspaces_total"] == 2
        assert summary["workspaces_run"] == 2
        assert mock_run.call_count == 2

        # Verify each call carried the RIGHT sub_tenant_id.
        first_kwargs = mock_run.call_args_list[0].kwargs
        second_kwargs = mock_run.call_args_list[1].kwargs
        assert first_kwargs["workspace_id"] == "ws-1"
        assert first_kwargs["hydradb_sub_tenant_id"] == "ws_aaaaaaaaaaaa"
        assert first_kwargs["bot_token"] == "xoxb-1"
        assert first_kwargs["channel_ids"] == ["C1", "C2"]
        assert second_kwargs["workspace_id"] == "ws-2"
        assert second_kwargs["hydradb_sub_tenant_id"] == "ws_bbbbbbbbbbbb"

        # Both clean runs -> both should be stamped synced.
        assert mock_mark.call_count == 2

    def test_skips_workspace_with_no_channels_selected(self):
        from scheduler import run_all_workspaces_once

        wss = [
            {
                "workspace_id": "ws-1",
                "hydradb_sub_tenant_id": "ws_aaaaaaaaaaaa",
                "bot_token": "xoxb-1",
                "channel_ids": [],  # empty
            }
        ]
        with patch(
            "scheduler.list_active_workspaces_with_slack",
            return_value=wss,
        ), patch(
            "scheduler.run_workspace_ingest",
        ) as mock_run:
            summary = run_all_workspaces_once()
        assert summary["workspaces_skipped"] == 1
        assert summary["workspaces_run"] == 0
        mock_run.assert_not_called()

    def test_skips_workspace_with_no_sub_tenant(self):
        # Defensive: if a row somehow has no sub_tenant_id, we MUST NOT
        # fall back to the env default -- that would leak data into the
        # global bucket.
        from scheduler import run_all_workspaces_once

        wss = [
            {
                "workspace_id": "ws-1",
                "hydradb_sub_tenant_id": "",  # missing
                "bot_token": "xoxb-1",
                "channel_ids": ["C1"],
            }
        ]
        with patch(
            "scheduler.list_active_workspaces_with_slack",
            return_value=wss,
        ), patch(
            "scheduler.run_workspace_ingest",
        ) as mock_run:
            summary = run_all_workspaces_once()
        assert summary["workspaces_skipped"] == 1
        mock_run.assert_not_called()

    def test_per_workspace_error_does_not_stop_sweep(self):
        """The whole point of per-workspace try/except: one bad workspace
        must not poison the rest."""
        from scheduler import run_all_workspaces_once

        wss = [
            {
                "workspace_id": "ws-bad",
                "hydradb_sub_tenant_id": "ws_aaaaaaaaaaaa",
                "bot_token": "xoxb-1",
                "channel_ids": ["C1"],
            },
            {
                "workspace_id": "ws-good",
                "hydradb_sub_tenant_id": "ws_bbbbbbbbbbbb",
                "bot_token": "xoxb-2",
                "channel_ids": ["C2"],
            },
        ]

        def fake_run(*, workspace_id, **kwargs):
            if workspace_id == "ws-bad":
                raise RuntimeError("network is on fire")
            return {"failures": 0}

        with patch(
            "scheduler.list_active_workspaces_with_slack",
            return_value=wss,
        ), patch(
            "scheduler.run_workspace_ingest",
            side_effect=fake_run,
        ), patch(
            "scheduler.mark_workspace_synced",
            return_value=True,
        ) as mock_mark:
            summary = run_all_workspaces_once()
        assert summary["workspaces_failed"] == 1
        assert summary["workspaces_run"] == 1
        # Only the successful workspace gets a sync stamp.
        mock_mark.assert_called_once()
        assert mock_mark.call_args.kwargs["workspace_id"] == "ws-good"

    def test_partial_failure_does_not_stamp_synced(self):
        # When the runner reports any failures, we don't stamp synced --
        # that way a chronically failing workspace shows a stale
        # last_sync_at and operators can see it.
        from scheduler import run_all_workspaces_once

        wss = [
            {
                "workspace_id": "ws-1",
                "hydradb_sub_tenant_id": "ws_aaaaaaaaaaaa",
                "bot_token": "xoxb-1",
                "channel_ids": ["C1"],
            }
        ]
        with patch(
            "scheduler.list_active_workspaces_with_slack",
            return_value=wss,
        ), patch(
            "scheduler.run_workspace_ingest",
            return_value={"failures": 3},
        ), patch(
            "scheduler.mark_workspace_synced",
            return_value=True,
        ) as mock_mark:
            summary = run_all_workspaces_once()
        assert summary["workspaces_run"] == 1
        mock_mark.assert_not_called()
