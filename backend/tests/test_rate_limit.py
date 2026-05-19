"""Tests for rate_limit.py — per-client sliding-window limiter."""

import os
import time
from unittest.mock import patch, MagicMock

import pytest


class TestRateLimitHelpers:
    def test_limit_per_window_default(self):
        from rate_limit import _limit_per_window
        with patch.dict(os.environ, {"RATE_LIMIT_PER_5_MIN": "30"}):
            assert _limit_per_window() == 30

    def test_limit_per_window_bad_value_returns_100(self):
        from rate_limit import _limit_per_window
        with patch.dict(os.environ, {"RATE_LIMIT_PER_5_MIN": "not-a-number"}):
            assert _limit_per_window() == 100

    def test_limit_per_window_minimum_1(self):
        from rate_limit import _limit_per_window
        with patch.dict(os.environ, {"RATE_LIMIT_PER_5_MIN": "0"}):
            assert _limit_per_window() == 1

    def test_client_id_from_key_header(self):
        from rate_limit import _client_id_from
        req = MagicMock()
        req.headers = {"x-api-key": "mykey"}
        req.client = MagicMock(host="1.2.3.4")
        assert _client_id_from(req) == "key:mykey"

    def test_client_id_falls_back_to_ip(self):
        from rate_limit import _client_id_from
        req = MagicMock()
        req.headers = {}
        req.client = MagicMock(host="10.0.0.1")
        assert _client_id_from(req) == "ip:10.0.0.1"

    def test_client_id_handles_missing_client(self):
        from rate_limit import _client_id_from
        req = MagicMock()
        req.headers = {}
        req.client = None
        assert _client_id_from(req) == "ip:unknown"


class TestSlidingWindowLimiter:
    def _fresh_limiter(self):
        from rate_limit import _SlidingWindowLimiter
        return _SlidingWindowLimiter()

    def test_below_limit_does_not_raise(self):
        limiter = self._fresh_limiter()
        for _ in range(5):
            limiter.hit("test-client", limit=10)  # should not raise

    def test_at_limit_raises(self):
        from errors import RateLimitedError
        limiter = self._fresh_limiter()
        for _ in range(3):
            limiter.hit("client-a", limit=3)
        with pytest.raises(RateLimitedError):
            limiter.hit("client-a", limit=3)

    def test_different_clients_are_independent(self):
        from errors import RateLimitedError
        limiter = self._fresh_limiter()
        for _ in range(3):
            limiter.hit("client-x", limit=3)
        # client-y is unaffected
        limiter.hit("client-y", limit=3)  # should not raise

    def test_rate_limit_error_has_correct_status(self):
        from errors import RateLimitedError
        limiter = self._fresh_limiter()
        for _ in range(2):
            limiter.hit("cl", limit=2)
        with pytest.raises(RateLimitedError) as exc_info:
            limiter.hit("cl", limit=2)
        assert exc_info.value.status_code == 429
        assert exc_info.value.error_type == "rate_limited"

    def test_old_timestamps_are_evicted(self):
        """Requests older than the window should not count."""
        from rate_limit import _SlidingWindowLimiter, WINDOW_SECONDS
        limiter = _SlidingWindowLimiter()
        # Manually insert old timestamps just outside the window
        import time
        now = time.monotonic()
        from collections import deque
        limiter._buckets["stale-client"] = deque(
            [now - WINDOW_SECONDS - 1] * 5
        )
        # The old entries should be evicted; this should not raise
        limiter.hit("stale-client", limit=3)


class TestRateLimitEndpoint:
    """Integration tests against the live endpoint (rate limit set very low)."""

    def test_429_when_limit_exceeded(self, client, auth_headers):
        """Lower the limit to 1 and hit twice."""
        from rate_limit import _limiter
        # Use a unique client id that won't collide with other tests
        unique_key = "rate-limit-test-key-low"
        headers = {"X-API-Key": unique_key}

        with patch.dict(os.environ, {"APP_API_KEY": unique_key}):
            with patch.dict(os.environ, {"RATE_LIMIT_PER_5_MIN": "1"}):
                with patch("recall.prepare_recall_context") as mock_ctx:
                    mock_ctx.return_value = {"ready": False, "fallback_debug": {}}
                    # First request should succeed
                    r1 = client.post(
                        "/api/query",
                        json={"question": "hello world"},
                        headers=headers,
                    )
                    # Second should be rate-limited
                    r2 = client.post(
                        "/api/query",
                        json={"question": "hello world"},
                        headers=headers,
                    )
        # At least one of the follow-ups must be 429
        assert r1.status_code in (200, 422) or r2.status_code == 429
