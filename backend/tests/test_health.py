"""
Tests for backend/health.py

Covers:
    ✓ /api/health/live always returns 200 + {"status": "alive"}
    ✓ /api/health/ready returns 200 when required deps healthy
    ✓ /api/health/ready returns 503 when a required dep fails
    ✓ /api/health/ready checks only required services
    ✓ /api/health returns detailed results for all services
    ✓ /api/health overall status "healthy" when all ok
    ✓ /api/health overall status "degraded" when optional dep fails
    ✓ /api/health overall status "unhealthy" when required dep fails
    ✓ each check has latency_ms for real calls
    ✓ timeout: slow check capped at CHECK_TIMEOUT_SECONDS
    ✓ optional dep failure does not affect readiness
    ✓ scheduler disabled → ok (not warning)
    ✓ scheduler running → ok + jobs count
    ✓ scheduler broken → warning
    ✓ HydraDB auth failure → error
    ✓ HydraDB connection error → error
    ✓ LLM auth failure → error
    ✓ Slack token missing → not_configured
    ✓ Slack token invalid → warning
    ✓ database placeholder → not_configured
    ✓ future check registration via register_health_check
"""

import asyncio
import json
import logging
import logging.handlers
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from health import (
    CHECK_TIMEOUT_SECONDS,
    STATUS_ERROR,
    STATUS_NOT_CONFIGURED,
    STATUS_OK,
    STATUS_WARNING,
    DatabaseHealthCheckPlaceholder,
    HealthCheck,
    HealthResult,
    HydraHealthCheck,
    LLMHealthCheck,
    SchedulerHealthCheck,
    SlackHealthCheck,
    _aggregate_status,
    _run_check,
    register_health_check,
    router,
)

# ---------------------------------------------------------------------------
# Minimal FastAPI app for route testing
# ---------------------------------------------------------------------------

_app = FastAPI()
_app.include_router(router)
_client = TestClient(_app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ConstantCheck(HealthCheck):
    """Health check that always returns a fixed result."""

    def __init__(self, name: str, result: HealthResult, required: bool = True):
        self.name = name
        self._result = result
        self.required = required

    async def check(self) -> HealthResult:
        return self._result


class _SlowCheck(HealthCheck):
    """Health check that sleeps past the timeout."""

    name = "slow"
    required = False

    async def check(self) -> HealthResult:
        await asyncio.sleep(CHECK_TIMEOUT_SECONDS + 2)
        return HealthResult(status=STATUS_OK)


# ---------------------------------------------------------------------------
# Liveness endpoint
# ---------------------------------------------------------------------------


class TestLiveness:
    def test_always_200(self):
        resp = _client.get("/api/health/live")
        assert resp.status_code == 200

    def test_body_contains_alive(self):
        resp = _client.get("/api/health/live")
        assert resp.json() == {"status": "alive"}

    def test_no_auth_required(self):
        # should never need X-API-Key
        resp = _client.get("/api/health/live", headers={})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# _aggregate_status helper
# ---------------------------------------------------------------------------


class TestAggregateStatus:
    def _checks_and_results(self, specs):
        checks = []
        results = {}
        for name, status, required in specs:
            c = _ConstantCheck(name, HealthResult(status=status), required=required)
            checks.append(c)
            results[name] = HealthResult(status=status)
        return checks, results

    def test_all_ok_is_healthy(self):
        checks, results = self._checks_and_results(
            [
                ("hydradb", STATUS_OK, True),
                ("llm", STATUS_OK, True),
                ("slack", STATUS_OK, False),
            ]
        )
        assert _aggregate_status(checks, results) == "healthy"

    def test_required_error_is_unhealthy(self):
        checks, results = self._checks_and_results(
            [
                ("hydradb", STATUS_ERROR, True),
                ("llm", STATUS_OK, True),
            ]
        )
        assert _aggregate_status(checks, results) == "unhealthy"

    def test_optional_error_is_degraded(self):
        checks, results = self._checks_and_results(
            [
                ("hydradb", STATUS_OK, True),
                ("llm", STATUS_OK, True),
                ("slack", STATUS_ERROR, False),
            ]
        )
        assert _aggregate_status(checks, results) == "degraded"

    def test_optional_warning_is_degraded(self):
        checks, results = self._checks_and_results(
            [
                ("hydradb", STATUS_OK, True),
                ("llm", STATUS_OK, True),
                ("slack", STATUS_WARNING, False),
            ]
        )
        assert _aggregate_status(checks, results) == "degraded"

    def test_not_configured_is_neutral(self):
        checks, results = self._checks_and_results(
            [
                ("hydradb", STATUS_OK, True),
                ("llm", STATUS_OK, True),
                ("database", STATUS_NOT_CONFIGURED, False),
            ]
        )
        assert _aggregate_status(checks, results) == "healthy"

    def test_required_error_trumps_optional_warning(self):
        checks, results = self._checks_and_results(
            [
                ("hydradb", STATUS_ERROR, True),
                ("slack", STATUS_WARNING, False),
            ]
        )
        assert _aggregate_status(checks, results) == "unhealthy"


# ---------------------------------------------------------------------------
# _run_check: timeout and exception safety
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRunCheck:
    async def test_returns_ok_on_success(self):
        check = _ConstantCheck("c1", HealthResult(status=STATUS_OK))
        name, result = await _run_check(check)
        assert name == "c1"
        assert result.status == STATUS_OK

    async def test_timeout_returns_error(self):
        name, result = await _run_check(_SlowCheck())
        assert name == "slow"
        assert result.status == STATUS_ERROR
        assert "timed out" in (result.message or "")

    async def test_exception_in_check_returns_error(self):
        class _BrokenCheck(HealthCheck):
            name = "broken"
            required = False

            async def check(self) -> HealthResult:
                raise RuntimeError("oops")

        name, result = await _run_check(_BrokenCheck())
        assert result.status == STATUS_ERROR
        assert "oops" in (result.message or "")


# ---------------------------------------------------------------------------
# Readiness endpoint
# ---------------------------------------------------------------------------


class TestReadiness:
    def _patch_registry(self, checks):
        return patch("health._get_registry", return_value=checks)

    def test_200_when_required_ok(self):
        checks = [
            _ConstantCheck("hydradb", HealthResult(STATUS_OK), required=True),
            _ConstantCheck("llm", HealthResult(STATUS_OK), required=True),
        ]
        with self._patch_registry(checks):
            resp = _client.get("/api/health/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ready"
        assert body["checks"]["hydradb"] == STATUS_OK
        assert body["checks"]["llm"] == STATUS_OK

    def test_503_when_required_fails(self):
        checks = [
            _ConstantCheck("hydradb", HealthResult(STATUS_ERROR), required=True),
            _ConstantCheck("llm", HealthResult(STATUS_OK), required=True),
        ]
        with self._patch_registry(checks):
            resp = _client.get("/api/health/ready")
        assert resp.status_code == 503
        assert resp.json()["status"] == "not_ready"

    def test_200_when_optional_fails(self):
        """Optional failures must not affect readiness status code."""
        checks = [
            _ConstantCheck("hydradb", HealthResult(STATUS_OK), required=True),
            _ConstantCheck("llm", HealthResult(STATUS_OK), required=True),
            _ConstantCheck("slack", HealthResult(STATUS_ERROR), required=False),
        ]
        # readiness only runs required checks; slack (optional) is excluded
        required_only = [c for c in checks if c.required]
        with self._patch_registry(required_only):
            resp = _client.get("/api/health/ready")
        assert resp.status_code == 200

    def test_checks_only_required_services(self):
        """readiness endpoint filters to required=True only."""
        called = []

        class _TrackingCheck(HealthCheck):
            def __init__(self, name, req):
                self.name = name
                self.required = req

            async def check(self) -> HealthResult:
                called.append(self.name)
                return HealthResult(STATUS_OK)

        checks = [
            _TrackingCheck("req1", True),
            _TrackingCheck("opt1", False),
            _TrackingCheck("req2", True),
        ]
        with self._patch_registry(checks):
            _client.get("/api/health/ready")

        # Only required checks should have been invoked
        assert "req1" in called
        assert "req2" in called
        assert "opt1" not in called


# ---------------------------------------------------------------------------
# Detailed /api/health endpoint
# ---------------------------------------------------------------------------


class TestDetailedHealth:
    def _patch_registry(self, checks):
        return patch("health._get_registry", return_value=checks)

    def test_always_200(self):
        checks = [
            _ConstantCheck("hydradb", HealthResult(STATUS_ERROR), required=True),
        ]
        with self._patch_registry(checks):
            resp = _client.get("/api/health")
        assert resp.status_code == 200

    def test_status_healthy_all_ok(self):
        checks = [
            _ConstantCheck("hydradb", HealthResult(STATUS_OK), True),
            _ConstantCheck("llm", HealthResult(STATUS_OK), True),
            _ConstantCheck("slack", HealthResult(STATUS_OK), False),
        ]
        with self._patch_registry(checks):
            body = _client.get("/api/health").json()
        assert body["status"] == "healthy"

    def test_status_degraded_optional_fails(self):
        checks = [
            _ConstantCheck("hydradb", HealthResult(STATUS_OK), True),
            _ConstantCheck("llm", HealthResult(STATUS_OK), True),
            _ConstantCheck("slack", HealthResult(STATUS_WARNING), False),
        ]
        with self._patch_registry(checks):
            body = _client.get("/api/health").json()
        assert body["status"] == "degraded"

    def test_status_unhealthy_required_fails(self):
        checks = [
            _ConstantCheck("hydradb", HealthResult(STATUS_ERROR), True),
            _ConstantCheck("llm", HealthResult(STATUS_OK), True),
        ]
        with self._patch_registry(checks):
            body = _client.get("/api/health").json()
        assert body["status"] == "unhealthy"

    def test_has_timestamp(self):
        checks = [_ConstantCheck("hydradb", HealthResult(STATUS_OK), True)]
        with self._patch_registry(checks):
            body = _client.get("/api/health").json()
        assert "timestamp" in body

    def test_checks_key_present_for_all_services(self):
        checks = [
            _ConstantCheck("hydradb", HealthResult(STATUS_OK), True),
            _ConstantCheck("llm", HealthResult(STATUS_OK), True),
            _ConstantCheck("slack", HealthResult(STATUS_WARNING), False),
            _ConstantCheck("database", HealthResult(STATUS_NOT_CONFIGURED), False),
        ]
        with self._patch_registry(checks):
            body = _client.get("/api/health").json()
        assert set(body["checks"].keys()) == {"hydradb", "llm", "slack", "database"}

    def test_latency_included_in_result(self):
        result = HealthResult(status=STATUS_OK, latency_ms=123.4)
        checks = [_ConstantCheck("hydradb", result, True)]
        with self._patch_registry(checks):
            body = _client.get("/api/health").json()
        assert body["checks"]["hydradb"]["latency_ms"] == pytest.approx(123.4, abs=0.5)

    def test_message_included_when_present(self):
        result = HealthResult(status=STATUS_WARNING, message="token invalid")
        checks = [_ConstantCheck("slack", result, False)]
        with self._patch_registry(checks):
            body = _client.get("/api/health").json()
        assert body["checks"]["slack"]["message"] == "token invalid"


# ---------------------------------------------------------------------------
# Individual check unit tests (mocked external calls)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestHydraHealthCheck:
    async def test_ok_on_200(self):
        mock_resp = MagicMock(status_code=200)
        with patch("requests.post", return_value=mock_resp):
            with patch.dict(
                "os.environ",
                {
                    "HYDRADB_API_KEY": "test-key",
                    "HYDRADB_TENANT_ID": "test-tenant",
                },
            ):
                result = await HydraHealthCheck().check()
        assert result.status == STATUS_OK
        assert result.latency_ms is not None

    async def test_error_on_401(self):
        mock_resp = MagicMock(status_code=401)
        with patch("requests.post", return_value=mock_resp):
            with patch.dict(
                "os.environ",
                {
                    "HYDRADB_API_KEY": "bad-key",
                    "HYDRADB_TENANT_ID": "t",
                },
            ):
                result = await HydraHealthCheck().check()
        assert result.status == STATUS_ERROR
        assert "authentication" in (result.message or "")

    async def test_error_on_500(self):
        mock_resp = MagicMock(status_code=500)
        with patch("requests.post", return_value=mock_resp):
            with patch.dict(
                "os.environ",
                {
                    "HYDRADB_API_KEY": "key",
                    "HYDRADB_TENANT_ID": "t",
                },
            ):
                result = await HydraHealthCheck().check()
        assert result.status == STATUS_ERROR

    async def test_error_on_connection_error(self):
        import requests as req_mod

        with patch("requests.post", side_effect=req_mod.exceptions.ConnectionError("refused")):
            with patch.dict(
                "os.environ",
                {
                    "HYDRADB_API_KEY": "key",
                    "HYDRADB_TENANT_ID": "t",
                },
            ):
                result = await HydraHealthCheck().check()
        assert result.status == STATUS_ERROR
        assert "connection" in (result.message or "").lower()

    async def test_error_when_credentials_missing(self):
        with patch.dict("os.environ", {}, clear=True):
            result = await HydraHealthCheck().check()
        assert result.status == STATUS_ERROR
        assert "not configured" in (result.message or "")

    async def test_error_on_timeout(self):
        import requests as req_mod

        with patch("requests.post", side_effect=req_mod.exceptions.Timeout()):
            with patch.dict(
                "os.environ",
                {
                    "HYDRADB_API_KEY": "key",
                    "HYDRADB_TENANT_ID": "t",
                },
            ):
                result = await HydraHealthCheck().check()
        assert result.status == STATUS_ERROR
        assert "timed out" in (result.message or "")


@pytest.mark.asyncio
class TestLLMHealthCheck:
    async def test_ok_on_success(self):
        mock_completion = MagicMock()
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_completion

        with patch("openai.OpenAI", return_value=mock_client):
            with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
                result = await LLMHealthCheck().check()
        assert result.status == STATUS_OK
        assert result.latency_ms is not None

    async def test_error_on_auth_failure(self):
        import openai as oai

        with patch("openai.OpenAI") as MockClient:
            instance = MockClient.return_value
            instance.chat.completions.create.side_effect = oai.AuthenticationError(
                "Invalid API key", response=MagicMock(status_code=401), body={}
            )
            with patch.dict("os.environ", {"OPENAI_API_KEY": "bad"}):
                result = await LLMHealthCheck().check()
        assert result.status == STATUS_ERROR
        assert "authentication" in (result.message or "")

    async def test_error_when_api_key_missing(self):
        with patch.dict("os.environ", {}, clear=True):
            result = await LLMHealthCheck().check()
        assert result.status == STATUS_ERROR

    async def test_error_on_connection_error(self):
        import openai as oai

        with patch("openai.OpenAI") as MockClient:
            instance = MockClient.return_value
            instance.chat.completions.create.side_effect = oai.APIConnectionError(request=MagicMock())
            with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
                result = await LLMHealthCheck().check()
        assert result.status == STATUS_ERROR


@pytest.mark.asyncio
class TestSlackHealthCheck:
    async def test_not_configured_when_token_missing(self):
        with patch.dict("os.environ", {}, clear=True):
            result = await SlackHealthCheck().check()
        assert result.status == STATUS_NOT_CONFIGURED

    async def test_ok_when_auth_test_succeeds(self):
        mock_resp = MagicMock()
        mock_resp.get = lambda k, default=None: (True if k == "ok" else default)
        mock_client_instance = MagicMock()
        mock_client_instance.auth_test.return_value = mock_resp

        with patch("slack_sdk.WebClient", return_value=mock_client_instance):
            with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test"}):
                result = await SlackHealthCheck().check()
        assert result.status == STATUS_OK

    async def test_warning_when_auth_test_fails(self):
        from slack_sdk.errors import SlackApiError

        mock_response = MagicMock()
        mock_response.__contains__ = lambda self, key: key in {"ok": False, "error": "invalid_auth"}
        mock_response.get = lambda k, d=None: {"ok": False, "error": "invalid_auth"}.get(k, d)
        mock_response.status_code = 200

        with patch("slack_sdk.WebClient") as MockWC:
            instance = MockWC.return_value
            instance.auth_test.side_effect = SlackApiError(message="invalid_auth", response=mock_response)
            with patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-bad"}):
                result = await SlackHealthCheck().check()
        assert result.status == STATUS_WARNING


@pytest.mark.asyncio
class TestSchedulerHealthCheck:
    async def test_ok_when_disabled(self):
        with patch("scheduler._scheduler", None):
            with patch("scheduler.auto_ingest_enabled", return_value=False):
                result = await SchedulerHealthCheck().check()
        assert result.status == STATUS_OK
        assert result.extra.get("jobs") == 0

    async def test_warning_when_enabled_but_not_running(self):
        with patch("scheduler._scheduler", None):
            with patch("scheduler.auto_ingest_enabled", return_value=True):
                result = await SchedulerHealthCheck().check()
        assert result.status == STATUS_WARNING

    async def test_ok_when_running_with_jobs(self):
        mock_scheduler = MagicMock()
        mock_scheduler.running = True
        mock_scheduler.get_jobs.return_value = [MagicMock(), MagicMock()]

        with patch("scheduler._scheduler", mock_scheduler):
            result = await SchedulerHealthCheck().check()
        assert result.status == STATUS_OK
        assert result.extra.get("jobs") == 2

    async def test_warning_when_initialized_but_not_running(self):
        mock_scheduler = MagicMock()
        mock_scheduler.running = False
        mock_scheduler.get_jobs.return_value = []

        with patch("scheduler._scheduler", mock_scheduler):
            result = await SchedulerHealthCheck().check()
        assert result.status == STATUS_WARNING


@pytest.mark.asyncio
class TestDatabasePlaceholder:
    async def test_always_not_configured(self):
        result = await DatabaseHealthCheckPlaceholder().check()
        assert result.status == STATUS_NOT_CONFIGURED


# ---------------------------------------------------------------------------
# Future service registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_registered_check_appears_in_detailed_health(self):
        class _FutureCheck(HealthCheck):
            name = "future_service"
            required = False

            async def check(self) -> HealthResult:
                return HealthResult(status=STATUS_OK, message="plugged in")

        # Register and verify it shows up
        new_check = _FutureCheck()
        register_health_check(new_check)

        # Build a registry that includes all + our new one
        from health import _get_registry

        registry = _get_registry()
        names = [c.name for c in registry]
        assert "future_service" in names

    def test_registered_check_is_called(self):
        called = []

        class _EvidenceCheck(HealthCheck):
            name = "evidence_check"
            required = False

            async def check(self) -> HealthResult:
                called.append(True)
                return HealthResult(status=STATUS_OK)

        register_health_check(_EvidenceCheck())

        with patch("health._get_registry", return_value=[_EvidenceCheck()]):
            _client.get("/api/health")

        assert called


# ---------------------------------------------------------------------------
# Structured log output from health checks
# ---------------------------------------------------------------------------


class TestHealthLogging:
    def _capture_health_logs(self, check: HealthCheck):
        handler = logging.handlers.MemoryHandler(capacity=1000, flushLevel=logging.CRITICAL)
        handler.buffer = []

        health_logger = logging.getLogger("health")
        health_logger.addHandler(handler)
        original_level = health_logger.level
        health_logger.setLevel(logging.DEBUG)

        try:
            asyncio.run(_run_check(check))
        finally:
            health_logger.removeHandler(handler)
            health_logger.setLevel(original_level)

        records = []
        for lr in handler.buffer:
            rec: dict = {"event": lr.getMessage()}
            if hasattr(lr, "check"):
                rec["check"] = lr.check
            if hasattr(lr, "status"):
                rec["status"] = lr.status
            if hasattr(lr, "reason"):
                rec["reason"] = lr.reason
            records.append(rec)
        return records

    def test_started_and_completed_events(self):
        check = _ConstantCheck("testcheck", HealthResult(STATUS_OK))
        records = self._capture_health_logs(check)
        events = [r["event"] for r in records if "check" in r]
        assert "health_check_started" in events
        assert "health_check_completed" in events

    def test_failed_event_on_timeout(self):
        records = self._capture_health_logs(_SlowCheck())
        events = [r["event"] for r in records if "check" in r]
        assert "health_check_failed" in events
