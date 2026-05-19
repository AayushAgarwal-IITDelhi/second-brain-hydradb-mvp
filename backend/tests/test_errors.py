"""Tests for errors.py — typed AppError hierarchy and FastAPI handler."""

import pytest
from fastapi import Request
from fastapi.responses import JSONResponse


class TestAppErrorClasses:
    def test_base_app_error_defaults(self):
        from errors import AppError
        err = AppError()
        assert err.status_code == 500
        assert err.error_type == "internal_error"
        assert err.detail == "Something went wrong."

    def test_app_error_custom_detail(self):
        from errors import AppError
        err = AppError("Custom message")
        assert err.detail == "Custom message"
        assert str(err) == "Custom message"

    def test_app_error_log_context(self):
        from errors import AppError
        err = AppError("msg", log_context="extra info")
        assert err.log_context == "extra info"

    def test_hydradb_error(self):
        from errors import HydraDBError
        err = HydraDBError()
        assert err.status_code == 502
        assert err.error_type == "hydradb_error"
        assert "unavailable" in err.detail.lower()

    def test_llm_error(self):
        from errors import LLMError
        err = LLMError()
        assert err.status_code == 502
        assert err.error_type == "llm_error"

    def test_upstream_timeout_error(self):
        from errors import UpstreamTimeoutError
        err = UpstreamTimeoutError()
        assert err.status_code == 504
        assert err.error_type == "upstream_timeout"

    def test_rate_limited_error(self):
        from errors import RateLimitedError
        err = RateLimitedError()
        assert err.status_code == 429
        assert err.error_type == "rate_limited"

    def test_all_errors_are_app_errors(self):
        from errors import AppError, HydraDBError, LLMError, UpstreamTimeoutError, RateLimitedError
        for klass in (HydraDBError, LLMError, UpstreamTimeoutError, RateLimitedError):
            assert issubclass(klass, AppError)

    def test_hydradb_error_custom_detail(self):
        from errors import HydraDBError
        err = HydraDBError("Service down", log_context="HTTP 503")
        assert err.detail == "Service down"
        assert err.log_context == "HTTP 503"


class TestAppErrorHandler:
    @pytest.mark.asyncio
    async def test_handler_returns_json_response(self):
        from errors import AppError, app_error_handler
        from unittest.mock import MagicMock
        request = MagicMock(spec=Request)
        err = AppError("test error")
        response = await app_error_handler(request, err)
        assert isinstance(response, JSONResponse)

    @pytest.mark.asyncio
    async def test_handler_status_code(self):
        from errors import HydraDBError, app_error_handler
        from unittest.mock import MagicMock
        request = MagicMock(spec=Request)
        err = HydraDBError("down")
        response = await app_error_handler(request, err)
        assert response.status_code == 502

    @pytest.mark.asyncio
    async def test_handler_payload_shape(self):
        from errors import LLMError, app_error_handler
        import json
        from unittest.mock import MagicMock
        request = MagicMock(spec=Request)
        err = LLMError("LLM failed")
        response = await app_error_handler(request, err)
        body = json.loads(response.body)
        assert body["detail"] == "LLM failed"
        assert body["error_type"] == "llm_error"

    @pytest.mark.asyncio
    async def test_handler_does_not_leak_log_context(self):
        from errors import AppError, app_error_handler
        import json
        from unittest.mock import MagicMock
        request = MagicMock(spec=Request)
        err = AppError("friendly", log_context="SECRET_INTERNAL_DETAIL")
        response = await app_error_handler(request, err)
        body = json.loads(response.body)
        assert "SECRET_INTERNAL_DETAIL" not in str(body)
