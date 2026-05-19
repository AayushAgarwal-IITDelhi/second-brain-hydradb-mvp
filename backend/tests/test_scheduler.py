"""Tests for scheduler.py — APScheduler background ingestion."""

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

        # Reset scheduler state
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
                with patch("scheduler.BackgroundScheduler", return_value=mock_scheduler):
                    with patch("scheduler.run_ingestion"):
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
        """A crash in ingestion must not propagate; scheduler must stay alive."""
        from scheduler import _job_wrapper

        with patch("scheduler.run_ingestion", side_effect=RuntimeError("boom")):
            _job_wrapper()  # should not raise

    def test_job_wrapper_handles_system_exit(self):
        from scheduler import _job_wrapper

        with patch("scheduler.run_ingestion", side_effect=SystemExit(1)):
            _job_wrapper()  # should not propagate SystemExit

    def test_job_wrapper_calls_ingestion(self):
        from scheduler import _job_wrapper

        # _job_wrapper calls _run_ingestion_with_retry (the retry-wrapped version),
        # not run_ingestion directly — patch the wrapper that's actually invoked.
        with patch("scheduler._run_ingestion_with_retry") as mock_run:
            _job_wrapper()
            mock_run.assert_called_once()
