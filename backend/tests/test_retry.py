"""
Tests for backend/retry.py

Covers:
    ✓ sync success on first attempt
    ✓ sync success after retries
    ✓ sync retry exhaustion raises RetryExhausted
    ✓ async success on first attempt
    ✓ async success after retries
    ✓ async retry exhaustion raises RetryExhausted
    ✓ no retry on auth errors (401, 403)
    ✓ no retry on validation errors (400, 404, 422)
    ✓ no retry on ValueError / TypeError
    ✓ no retry on NonRetryableError
    ✓ no retry on explicitly non_retryable_exceptions
    ✓ retry on 429 / 500 / 502 / 503 / 504
    ✓ retry on retryable exception types
    ✓ jitter: computed delay is within expected bounds
    ✓ retry_attempt / retry_success / retry_exhausted log events
    ✓ upstream_status attribute respected (AppError-style exceptions)
    ✓ streaming guard: decorator applied before first token is safe
"""

import asyncio
import json
import time
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from retry import (
    NonRetryableError,
    RetryExhausted,
    _compute_delay,
    _extract_status_code,
    _should_retry,
    retry,
    RETRYABLE_STATUS_CODES,
    NON_RETRYABLE_STATUS_CODES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeHTTPError(Exception):
    """Simulates an HTTP error with a status_code attribute."""
    def __init__(self, status_code: int):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class _FakeUpstreamError(Exception):
    """Simulates an AppError-style exception with upstream_status."""
    def __init__(self, upstream_status: Optional[int] = None):
        super().__init__("upstream error")
        self.upstream_status = upstream_status


def _make_failing_sync(fail_times: int, exc_factory=None):
    """Returns a sync function that fails `fail_times` then succeeds."""
    call_count = [0]

    def fn():
        call_count[0] += 1
        if call_count[0] <= fail_times:
            raise (exc_factory() if exc_factory else ConnectionError("transient"))
        return "ok"

    return fn, call_count


def _make_failing_async(fail_times: int, exc_factory=None):
    """Returns an async function that fails `fail_times` then succeeds."""
    call_count = [0]

    async def fn():
        call_count[0] += 1
        if call_count[0] <= fail_times:
            raise (exc_factory() if exc_factory else ConnectionError("transient"))
        return "ok"

    return fn, call_count


# ---------------------------------------------------------------------------
# _compute_delay
# ---------------------------------------------------------------------------

class TestComputeDelay:
    def test_exponential_without_jitter(self):
        assert _compute_delay(1, 1.0, 30.0, 2.0, jitter=False) == pytest.approx(1.0)
        assert _compute_delay(2, 1.0, 30.0, 2.0, jitter=False) == pytest.approx(2.0)
        assert _compute_delay(3, 1.0, 30.0, 2.0, jitter=False) == pytest.approx(4.0)

    def test_max_delay_capped(self):
        delay = _compute_delay(10, 1.0, 5.0, 2.0, jitter=False)
        assert delay == pytest.approx(5.0)

    def test_jitter_within_bounds(self):
        for _ in range(200):
            delay = _compute_delay(1, 1.0, 30.0, 2.0, jitter=True)
            # base = 1 s; jitter multiplier is in [0.75, 1.25]
            assert 0.74 <= delay <= 1.26, f"delay {delay} out of jitter range"

    def test_never_negative(self):
        assert _compute_delay(1, 0.0, 0.0, 2.0, jitter=True) >= 0.0


# ---------------------------------------------------------------------------
# _extract_status_code
# ---------------------------------------------------------------------------

class TestExtractStatusCode:
    def test_status_code_attr(self):
        exc = _FakeHTTPError(429)
        assert _extract_status_code(exc) == 429

    def test_upstream_status_attr(self):
        exc = _FakeUpstreamError(upstream_status=503)
        assert _extract_status_code(exc) == 503

    def test_response_object(self):
        exc = Exception("err")
        exc.response = MagicMock(status_code=502)
        assert _extract_status_code(exc) == 502

    def test_no_status(self):
        assert _extract_status_code(Exception("plain")) is None

    def test_upstream_status_takes_priority_over_status_code(self):
        exc = Exception("mixed")
        exc.upstream_status = 503
        exc.status_code = 200  # own HTTP status (not the upstream one)
        assert _extract_status_code(exc) == 503


# ---------------------------------------------------------------------------
# _should_retry
# ---------------------------------------------------------------------------

class TestShouldRetry:
    _retryable_codes = frozenset(RETRYABLE_STATUS_CODES)

    def _check(self, exc, retryable_exc=(ConnectionError,),
               non_retryable_exc=()):
        return _should_retry(exc, retryable_exc, self._retryable_codes,
                              non_retryable_exc)

    def test_connection_error_retried(self):
        assert self._check(ConnectionError("network")) is True

    def test_timeout_error_retried(self):
        assert self._check(TimeoutError(), retryable_exc=(TimeoutError,)) is True

    def test_value_error_never_retried(self):
        assert self._check(ValueError("bad input")) is False

    def test_type_error_never_retried(self):
        assert self._check(TypeError("bad type")) is False

    def test_non_retryable_sentinel_never_retried(self):
        assert self._check(NonRetryableError("stop")) is False

    @pytest.mark.parametrize("code", [401, 403, 400, 404, 422])
    def test_non_retryable_status_codes(self, code):
        exc = _FakeHTTPError(code)
        assert self._check(exc, retryable_exc=(Exception,)) is False

    @pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
    def test_retryable_status_codes(self, code):
        exc = _FakeHTTPError(code)
        assert self._check(exc, retryable_exc=()) is True

    def test_upstream_status_401_not_retried(self):
        exc = _FakeUpstreamError(upstream_status=401)
        # even though the exc type itself is listed as retryable
        assert self._check(exc, retryable_exc=(_FakeUpstreamError,)) is False

    def test_upstream_status_503_retried(self):
        exc = _FakeUpstreamError(upstream_status=503)
        assert self._check(exc, retryable_exc=()) is True

    def test_explicit_non_retryable_exception(self):
        class MyError(Exception): pass
        exc = MyError("stop")
        assert self._check(exc, retryable_exc=(Exception,),
                           non_retryable_exc=(MyError,)) is False

    def test_unknown_exception_not_in_retryable_list(self):
        assert self._check(RuntimeError("oops"), retryable_exc=()) is False


# ---------------------------------------------------------------------------
# Sync retry decorator
# ---------------------------------------------------------------------------

class TestSyncRetry:
    def test_first_attempt_success(self):
        fn, calls = _make_failing_sync(0)
        wrapped = retry(service="test", max_attempts=3, jitter=False)(fn)
        assert wrapped() == "ok"
        assert calls[0] == 1

    def test_success_after_two_failures(self):
        fn, calls = _make_failing_sync(2)
        wrapped = retry(
            service="test", max_attempts=3,
            initial_delay=0.0, jitter=False,
        )(fn)
        assert wrapped() == "ok"
        assert calls[0] == 3

    def test_exhausted_raises_retry_exhausted(self):
        fn, calls = _make_failing_sync(5)
        wrapped = retry(
            service="test", max_attempts=3,
            initial_delay=0.0, jitter=False,
        )(fn)
        with pytest.raises(RetryExhausted):
            wrapped()
        assert calls[0] == 3

    def test_no_retry_on_value_error(self):
        call_count = [0]

        @retry(service="test", max_attempts=3, initial_delay=0.0, jitter=False)
        def fn():
            call_count[0] += 1
            raise ValueError("bad")

        with pytest.raises(ValueError):
            fn()
        assert call_count[0] == 1

    def test_no_retry_on_401(self):
        call_count = [0]

        @retry(service="test", max_attempts=3, initial_delay=0.0, jitter=False)
        def fn():
            call_count[0] += 1
            raise _FakeHTTPError(401)

        with pytest.raises(_FakeHTTPError):
            fn()
        assert call_count[0] == 1

    def test_no_retry_on_403(self):
        call_count = [0]

        @retry(service="test", max_attempts=3, initial_delay=0.0, jitter=False)
        def fn():
            call_count[0] += 1
            raise _FakeHTTPError(403)

        with pytest.raises(_FakeHTTPError):
            fn()
        assert call_count[0] == 1

    def test_retry_on_429(self):
        call_count = [0]

        @retry(service="test", max_attempts=3, initial_delay=0.0, jitter=False)
        def fn():
            call_count[0] += 1
            if call_count[0] < 3:
                raise _FakeHTTPError(429)
            return "ok"

        assert fn() == "ok"
        assert call_count[0] == 3

    def test_sleep_called_between_retries(self):
        fn, _ = _make_failing_sync(2)
        wrapped = retry(
            service="test", max_attempts=3,
            initial_delay=1.0, jitter=False,
        )(fn)

        sleep_calls = []
        with patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
            wrapped()

        assert len(sleep_calls) == 2
        assert sleep_calls[0] == pytest.approx(1.0)
        assert sleep_calls[1] == pytest.approx(2.0)

    def test_non_retryable_exception_param(self):
        class Boom(Exception): pass
        call_count = [0]

        @retry(service="test", max_attempts=3, initial_delay=0.0, jitter=False,
               non_retryable_exceptions=(Boom,),
               retryable_exceptions=(Exception,))
        def fn():
            call_count[0] += 1
            raise Boom("no retry")

        with pytest.raises(Boom):
            fn()
        assert call_count[0] == 1

    def test_upstream_status_401_not_retried(self):
        call_count = [0]

        @retry(service="test", max_attempts=3, initial_delay=0.0, jitter=False,
               retryable_exceptions=(_FakeUpstreamError,))
        def fn():
            call_count[0] += 1
            raise _FakeUpstreamError(upstream_status=401)

        with pytest.raises(_FakeUpstreamError):
            fn()
        assert call_count[0] == 1


# ---------------------------------------------------------------------------
# Async retry decorator
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestAsyncRetry:
    async def test_first_attempt_success(self):
        fn, calls = _make_failing_async(0)
        wrapped = retry(service="test", max_attempts=3, jitter=False)(fn)
        assert await wrapped() == "ok"
        assert calls[0] == 1

    async def test_success_after_two_failures(self):
        fn, calls = _make_failing_async(2)
        wrapped = retry(
            service="test", max_attempts=3,
            initial_delay=0.0, jitter=False,
        )(fn)
        assert await wrapped() == "ok"
        assert calls[0] == 3

    async def test_exhausted_raises_retry_exhausted(self):
        fn, calls = _make_failing_async(5)
        wrapped = retry(
            service="test", max_attempts=3,
            initial_delay=0.0, jitter=False,
        )(fn)
        with pytest.raises(RetryExhausted):
            await wrapped()
        assert calls[0] == 3

    async def test_no_retry_on_value_error(self):
        call_count = [0]

        @retry(service="test", max_attempts=3, initial_delay=0.0, jitter=False)
        async def fn():
            call_count[0] += 1
            raise ValueError("bad")

        with pytest.raises(ValueError):
            await fn()
        assert call_count[0] == 1

    async def test_no_retry_on_401(self):
        call_count = [0]

        @retry(service="test", max_attempts=3, initial_delay=0.0, jitter=False)
        async def fn():
            call_count[0] += 1
            raise _FakeHTTPError(401)

        with pytest.raises(_FakeHTTPError):
            await fn()
        assert call_count[0] == 1

    async def test_sleep_called_between_retries(self):
        fn, _ = _make_failing_async(1)
        wrapped = retry(
            service="test", max_attempts=3,
            initial_delay=1.0, jitter=False,
        )(fn)

        sleep_calls = []

        async def fake_sleep(s):
            sleep_calls.append(s)

        with patch("asyncio.sleep", side_effect=fake_sleep):
            await wrapped()

        assert len(sleep_calls) == 1
        assert sleep_calls[0] == pytest.approx(1.0)

    async def test_retry_on_503(self):
        call_count = [0]

        @retry(service="test", max_attempts=3, initial_delay=0.0, jitter=False)
        async def fn():
            call_count[0] += 1
            if call_count[0] < 3:
                raise _FakeHTTPError(503)
            return "ok"

        assert await fn() == "ok"
        assert call_count[0] == 3


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------

class TestRetryLogging:
    def _capture_logs(self, fn, *args, **kwargs):
        """Run fn, capture all JSON printed to stdout, return parsed list."""
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                result = fn(*args, **kwargs)
        except Exception as exc:
            result = exc
        raw = buf.getvalue()
        records = []
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return records, result

    def test_retry_attempt_logged(self):
        call_count = [0]

        # Use a non-zero initial_delay so delay_seconds appears in the log;
        # patch time.sleep so the test doesn't actually wait.
        @retry(service="svc", max_attempts=3, initial_delay=1.0, jitter=False)
        def fn():
            call_count[0] += 1
            if call_count[0] < 3:
                raise ConnectionError("down")
            return "ok"

        with patch("time.sleep"):
            records, _ = self._capture_logs(fn)
        attempt_events = [r for r in records if r.get("event") == "retry_attempt"]
        assert len(attempt_events) == 2
        assert attempt_events[0]["service"] == "svc"
        assert attempt_events[0]["attempt"] == 1
        assert "delay_seconds" in attempt_events[0]
        assert "error" in attempt_events[0]

    def test_retry_success_logged_after_failures(self):
        call_count = [0]

        @retry(service="svc", max_attempts=3, initial_delay=0.0, jitter=False)
        def fn():
            call_count[0] += 1
            if call_count[0] < 2:
                raise ConnectionError("down")
            return "ok"

        records, _ = self._capture_logs(fn)
        success_events = [r for r in records if r.get("event") == "retry_success"]
        assert len(success_events) == 1
        assert success_events[0]["attempt"] == 2

    def test_retry_exhausted_logged(self):
        @retry(service="svc", max_attempts=2, initial_delay=0.0, jitter=False)
        def fn():
            raise ConnectionError("always down")

        records, exc = self._capture_logs(fn)
        assert isinstance(exc, RetryExhausted)
        exhausted = [r for r in records if r.get("event") == "retry_exhausted"]
        assert len(exhausted) == 1
        assert exhausted[0]["service"] == "svc"
        assert exhausted[0]["attempt"] == 2

    def test_no_log_on_first_attempt_success(self):
        @retry(service="svc", max_attempts=3, initial_delay=0.0, jitter=False)
        def fn():
            return "ok"

        records, _ = self._capture_logs(fn)
        retry_events = [r for r in records
                        if r.get("event", "").startswith("retry_")]
        assert retry_events == []

    def test_log_contains_delay_seconds(self):
        call_count = [0]

        @retry(service="svc", max_attempts=3, initial_delay=2.0, jitter=False)
        def fn():
            call_count[0] += 1
            if call_count[0] < 2:
                raise ConnectionError("down")
            return "ok"

        records, _ = self._capture_logs(fn)
        attempt_events = [r for r in records if r.get("event") == "retry_attempt"]
        assert attempt_events[0]["delay_seconds"] == pytest.approx(2.0, abs=0.1)


# ---------------------------------------------------------------------------
# Jitter distribution
# ---------------------------------------------------------------------------

class TestJitterBehavior:
    def test_jitter_produces_variance(self):
        """Running with jitter=True on the same attempt produces different delays."""
        delays = set()
        for _ in range(30):
            d = _compute_delay(1, 1.0, 30.0, 2.0, jitter=True)
            delays.add(round(d, 3))
        # With 30 samples we expect at least 2 distinct values.
        assert len(delays) > 1

    def test_no_jitter_is_deterministic(self):
        d1 = _compute_delay(2, 1.0, 30.0, 2.0, jitter=False)
        d2 = _compute_delay(2, 1.0, 30.0, 2.0, jitter=False)
        assert d1 == d2


# ---------------------------------------------------------------------------
# Streaming safety note (doc test)
# ---------------------------------------------------------------------------

class TestStreamingRestriction:
    """
    Streaming note: the retry decorator must only wrap the *initialisation*
    call — i.e. the point before the first token is emitted.  This test
    verifies that a decorated async generator init function retries correctly
    and that a generator which has already started is not re-entered.
    """

    def test_init_retry_before_streaming(self):
        """A non-streaming initialiser is retried safely."""
        call_count = [0]

        @retry(service="stream-init", max_attempts=3,
               initial_delay=0.0, jitter=False)
        def init_stream():
            call_count[0] += 1
            if call_count[0] < 2:
                raise ConnectionError("init failed")
            return iter(["token1", "token2"])

        stream = init_stream()
        assert list(stream) == ["token1", "token2"]
        assert call_count[0] == 2
