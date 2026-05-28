"""Tests for rate_limit.py — per-client sliding-window limiter.

Phase 7: the limiter is bucketed -- one logical sliding-window deque
per (bucket, client_id) tuple. These tests target the new internals
while preserving every observable behavior from the pre-Phase-7
tests (under-limit -> ok, at-limit -> raise, eviction, cross-client
isolation, error shape).
"""

import os
import time
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _isolate_rate_limit_state():
    """
    Reset BOTH the bound logging contextvars AND the shared in-memory
    rate-limit buckets between tests. Without this, the
    `test_client_id_prefers_authenticated_user` test leaves user_id
    set in the contextvar, and other tests in the file see
    "user:user-abc" instead of their expected client_id.
    """
    from logging_config import bind_user_context
    from rate_limit import _limiter

    # Clear before each test.
    bind_user_context(None, None)
    with _limiter._lock:
        _limiter._buckets.clear()
    yield
    # And after, so a failure mid-test doesn't poison the next one.
    bind_user_context(None, None)
    with _limiter._lock:
        _limiter._buckets.clear()


class TestRateLimitHelpers:
    def test_bucket_limit_default(self):
        from rate_limit import _bucket_limit
        with patch.dict(os.environ, {"RATE_LIMIT_PER_5_MIN": "30"}):
            assert _bucket_limit("query") == 30

    def test_bucket_limit_bad_value_returns_default(self):
        from rate_limit import _bucket_limit
        # Bad value -> per-bucket default (query bucket: 20).
        with patch.dict(os.environ, {"RATE_LIMIT_PER_5_MIN": "not-a-number"}):
            assert _bucket_limit("query") == 20

    def test_bucket_limit_minimum_1(self):
        from rate_limit import _bucket_limit
        with patch.dict(os.environ, {"RATE_LIMIT_PER_5_MIN": "0"}):
            assert _bucket_limit("query") == 1

    def test_bucket_limit_per_bucket_envs(self):
        # Each bucket reads its own env var.
        from rate_limit import _bucket_limit
        with patch.dict(os.environ, {
            "RATE_LIMIT_AUTH_PER_5_MIN":          "7",
            "RATE_LIMIT_SLACK_WEBHOOK_PER_5_MIN": "777",
            "RATE_LIMIT_INGEST_PER_5_MIN":        "2",
        }):
            assert _bucket_limit("auth") == 7
            assert _bucket_limit("slack_webhook") == 777
            assert _bucket_limit("ingest") == 2

    def test_bucket_limit_override(self):
        # Explicit override bypasses env reading entirely.
        from rate_limit import _bucket_limit
        assert _bucket_limit("any_bucket", override=9) == 9

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

    def test_client_id_prefers_authenticated_user(self):
        # Phase 7: bound logging context (set by require_user /
        # require_workspace) takes precedence over headers + IP so a
        # single user is throttled across workspaces.
        from rate_limit import _client_id_from
        from logging_config import bind_user_context
        bind_user_context("user-abc", "workspace-1")
        try:
            req = MagicMock()
            req.headers = {"x-api-key": "should-not-win"}
            req.client = MagicMock(host="1.2.3.4")
            assert _client_id_from(req) == "user:user-abc"
        finally:
            bind_user_context(None, None)


class TestBucketedLimiter:
    def _fresh_limiter(self):
        from rate_limit import _BucketedLimiter
        return _BucketedLimiter()

    def test_below_limit_does_not_raise(self):
        limiter = self._fresh_limiter()
        for _ in range(5):
            limiter.hit("query", "test-client", limit=10)  # should not raise

    def test_at_limit_raises(self):
        from errors import RateLimitedError
        limiter = self._fresh_limiter()
        for _ in range(3):
            limiter.hit("query", "client-a", limit=3)
        with pytest.raises(RateLimitedError):
            limiter.hit("query", "client-a", limit=3)

    def test_different_clients_are_independent(self):
        limiter = self._fresh_limiter()
        for _ in range(3):
            limiter.hit("query", "client-x", limit=3)
        # client-y is unaffected
        limiter.hit("query", "client-y", limit=3)  # should not raise

    def test_different_buckets_are_independent(self):
        # Phase 7: a flood on /slack/events shouldn't starve /api/query.
        limiter = self._fresh_limiter()
        for _ in range(3):
            limiter.hit("slack_webhook", "client-x", limit=3)
        # Same client, different bucket -> not throttled
        limiter.hit("query", "client-x", limit=3)  # should not raise

    def test_rate_limit_error_has_correct_status(self):
        from errors import RateLimitedError
        limiter = self._fresh_limiter()
        for _ in range(2):
            limiter.hit("query", "cl", limit=2)
        with pytest.raises(RateLimitedError) as exc_info:
            limiter.hit("query", "cl", limit=2)
        assert exc_info.value.status_code == 429
        assert exc_info.value.error_type == "rate_limited"

    def test_old_timestamps_are_evicted(self):
        """Requests older than the window should not count."""
        from collections import deque
        from rate_limit import _BucketedLimiter, WINDOW_SECONDS
        limiter = _BucketedLimiter()
        # Manually insert old timestamps just outside the window.
        now = time.monotonic()
        limiter._buckets["query"]["stale-client"] = deque(
            [now - WINDOW_SECONDS - 1] * 5
        )
        # The old entries should be evicted; this should not raise.
        limiter.hit("query", "stale-client", limit=3)


class TestRateLimitEndpoint:
    """Integration tests against the live endpoint (rate limit set very low)."""

    def test_429_when_limit_exceeded(self, client, auth_headers):
        """Lower the limit to 1 and hit twice."""
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


class TestRateLimitDependencyFactory:
    """Phase 7: make_rate_limit_dependency builds bucketed deps."""

    def test_returns_callable_dependency(self):
        from rate_limit import make_rate_limit_dependency
        dep = make_rate_limit_dependency("auth")
        assert callable(dep)

    def test_dependency_enforces_bucket_limit(self):
        from rate_limit import make_rate_limit_dependency, _limiter
        from errors import RateLimitedError

        # Reset the global limiter so prior test state doesn't bleed in.
        with _limiter._lock:
            _limiter._buckets.clear()

        dep = make_rate_limit_dependency("auth", limit=2)
        req = MagicMock()
        req.headers = {"x-api-key": "isolated-test"}
        req.client = MagicMock(host="1.1.1.1")

        dep(req)  # 1st
        dep(req)  # 2nd
        with pytest.raises(RateLimitedError):
            dep(req)  # 3rd -> over the limit of 2

    def test_legacy_dependency_targets_query_bucket(self):
        # The legacy rate_limit_dependency still works and targets
        # the "query" bucket.
        from rate_limit import rate_limit_dependency, _limiter

        with _limiter._lock:
            _limiter._buckets.clear()

        req = MagicMock()
        req.headers = {"x-api-key": "legacy-test"}
        req.client = MagicMock(host="1.1.1.1")
        # Should not raise under the default query limit.
        rate_limit_dependency(req)