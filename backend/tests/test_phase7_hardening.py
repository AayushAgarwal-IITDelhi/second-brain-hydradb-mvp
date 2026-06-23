"""
Phase 7 production hardening tests.

These exercise the new surface in:
  - observability.py   (Sentry hooks, dead-letter logging, dep checks)
  - retry.py           (exponential backoff)
  - rate_limit.py      (per-bucket limiters end-to-end)
  - realtime_ingest.py (durable Supabase-backed event dedupe)
  - startup.py         (production env validation + secrets audit)
  - main.py            (/api/ready endpoint, per-route rate limits)
  - supabase_client.py (claim_slack_event_id, cleanup helper)

Existing Phase 1-6 tests stay untouched.
"""

import os
import time
from unittest.mock import MagicMock, patch

import pytest


# =====================================================================
# observability.py  --  Sentry hooks
# =====================================================================
class TestSentryHooks:
    def test_init_sentry_no_dsn_returns_false(self, monkeypatch):
        # Reset the module-level cache so a previous test can't leak in.
        import observability

        observability._sentry_enabled = False
        monkeypatch.setenv("SENTRY_DSN", "")
        assert observability.init_sentry() is False
        assert observability.sentry_enabled() is False

    def test_capture_exception_is_noop_when_disabled(self):
        # Plain function call; the no-op path must never raise.
        import observability

        observability._sentry_enabled = False
        observability.capture_exception(RuntimeError("ignored"))

    def test_init_sentry_handles_missing_sdk(self, monkeypatch):
        import observability

        observability._sentry_enabled = False
        monkeypatch.setenv("SENTRY_DSN", "https://fake@example.com/1")
        # Simulate sentry-sdk not being installed by making the import
        # itself raise.
        import builtins

        real_import = builtins.__import__

        def _broken_import(name, *args, **kwargs):
            if name.startswith("sentry_sdk"):
                raise ImportError("simulated: sentry_sdk not installed")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=_broken_import):
            assert observability.init_sentry() is False


# =====================================================================
# observability.py  --  dead-letter logger
# =====================================================================
class TestDeadLetter:
    def test_emit_dead_letter_logs_with_stable_event_name(self, caplog):
        import logging

        from observability import emit_dead_letter

        with caplog.at_level(logging.ERROR, logger="observability"):
            emit_dead_letter(
                kind="slack_ingest_channel",
                workspace_id="ws-1",
                error=RuntimeError("upload timed out"),
                context={"channel_id": "C1", "file_count": 7},
            )
        records = [r for r in caplog.records if r.message == "dead_letter"]
        assert len(records) == 1
        record = records[0]
        # The extra fields are attached to the LogRecord directly by
        # the standard `logging` machinery from extra={...}.
        assert getattr(record, "kind") == "slack_ingest_channel"
        assert getattr(record, "workspace_id") == "ws-1"
        assert getattr(record, "error") == "RuntimeError"
        assert getattr(record, "channel_id") == "C1"
        assert getattr(record, "file_count") == 7

    def test_emit_dead_letter_forwards_to_sentry_when_enabled(self):
        import observability

        observability._sentry_enabled = True
        captured = {}

        # Replace capture_exception with a stub so we can inspect calls
        # without depending on the real sentry_sdk being installed.
        def _stub_capture(err, tags=None, extra=None):
            captured["err"] = err
            captured["tags"] = tags
            captured["extra"] = extra

        try:
            with patch("observability.capture_exception", side_effect=_stub_capture):
                err = RuntimeError("boom")
                observability.emit_dead_letter(
                    kind="realtime_event",
                    workspace_id="ws-2",
                    error=err,
                    context={"event_id": "Ev_123"},
                )
            assert captured["err"] is err
            assert captured["tags"] == {
                "dead_letter": "realtime_event",
                "workspace_id": "ws-2",
            }
            # Context fields land in `extra` for Sentry's debug view.
            assert captured["extra"]["kind"] == "realtime_event"
            assert captured["extra"]["event_id"] == "Ev_123"
        finally:
            observability._sentry_enabled = False


# =====================================================================
# observability.py  --  dependency checks
# =====================================================================
class TestDependencyChecks:
    def test_all_healthy_returns_ok_true(self):
        # All three checks succeed -> overall ok=True.
        import observability

        good_resp = MagicMock()
        good_resp.status_code = 200
        with patch("observability.requests.get", return_value=good_resp), patch(
            "observability.requests.options", return_value=good_resp
        ), patch("observability.requests.head", return_value=good_resp):
            result = observability.check_dependencies()
        assert result["ok"] is True
        names = sorted(c["name"] for c in result["checks"])
        assert names == ["hydradb", "openai", "supabase"]

    def test_one_unhealthy_dep_fails_overall(self):
        import requests

        import observability

        good_resp = MagicMock()
        good_resp.status_code = 200
        with patch("observability.requests.get", side_effect=requests.ConnectionError("dns")), patch(
            "observability.requests.options", return_value=good_resp
        ), patch("observability.requests.head", return_value=good_resp):
            result = observability.check_dependencies()
        assert result["ok"] is False
        supa = next(c for c in result["checks"] if c["name"] == "supabase")
        assert supa["ok"] is False
        # latency_ms is always reported, even on failure.
        assert "latency_ms" in supa

    def test_openai_check_skipped_via_env(self, monkeypatch):
        import observability

        monkeypatch.setenv("DISABLE_OPENAI_READINESS", "true")
        good_resp = MagicMock()
        good_resp.status_code = 200
        with patch("observability.requests.get", return_value=good_resp), patch(
            "observability.requests.options", return_value=good_resp
        ):
            result = observability.check_dependencies()
        openai_check = next(c for c in result["checks"] if c["name"] == "openai")
        assert openai_check["ok"] is True
        assert openai_check.get("skipped") is True


# =====================================================================
# retry.py  --  exponential backoff
# =====================================================================
class TestRetry:
    def test_succeeds_on_first_attempt(self):
        from retry import retry_with_backoff

        calls = []

        def fn():
            calls.append(1)
            return "ok"

        assert retry_with_backoff(fn, attempts=3, initial_delay=0) == "ok"
        assert len(calls) == 1

    def test_retries_then_succeeds(self):
        from retry import retry_with_backoff

        attempts = {"n": 0}

        def fn():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("transient")
            return "finally"

        # Use initial_delay=0 so the test stays fast.
        result = retry_with_backoff(
            fn,
            attempts=5,
            initial_delay=0,
            max_delay=0,
            op_name="test",
        )
        assert result == "finally"
        assert attempts["n"] == 3

    def test_gives_up_after_attempts(self):
        from retry import retry_with_backoff

        attempts = {"n": 0}

        def fn():
            attempts["n"] += 1
            raise RuntimeError("permanent")

        with pytest.raises(RuntimeError):
            retry_with_backoff(
                fn,
                attempts=3,
                initial_delay=0,
                max_delay=0,
            )
        assert attempts["n"] == 3

    def test_on_giveup_callback_fires(self):
        from retry import retry_with_backoff

        captured = {}

        def fn():
            raise ValueError("nope")

        def on_giveup(err):
            captured["err"] = err

        with pytest.raises(ValueError):
            retry_with_backoff(
                fn,
                attempts=2,
                initial_delay=0,
                max_delay=0,
                on_giveup=on_giveup,
            )
        assert isinstance(captured["err"], ValueError)

    def test_non_listed_exception_propagates_immediately(self):
        from retry import retry_with_backoff

        attempts = {"n": 0}

        def fn():
            attempts["n"] += 1
            # TypeError is NOT in retry_on, so propagates after attempt 1.
            raise TypeError("bug")

        with pytest.raises(TypeError):
            retry_with_backoff(
                fn,
                attempts=5,
                initial_delay=0,
                retry_on=(RuntimeError,),  # TypeError not included
            )
        assert attempts["n"] == 1

    def test_backoff_actually_waits(self):
        """A real (small) sleep between attempts confirms we're not
        firing all retries instantly."""
        from retry import retry_with_backoff

        attempts = {"n": 0}

        def fn():
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise RuntimeError("first")
            return "ok"

        started = time.monotonic()
        retry_with_backoff(
            fn,
            attempts=3,
            initial_delay=0.05,
            max_delay=0.1,
            jitter=0,
        )
        elapsed = time.monotonic() - started
        # One retry => slept at least ~0.05s (minus jitter floor).
        assert elapsed >= 0.04


# =====================================================================
# rate_limit.py  --  per-bucket end-to-end
# =====================================================================
class TestPerBucketLimits:
    @pytest.fixture(autouse=True)
    def _reset(self):
        from logging_config import bind_user_context
        from rate_limit import _limiter

        bind_user_context(None, None)
        with _limiter._lock:
            _limiter._buckets.clear()
        yield
        bind_user_context(None, None)
        with _limiter._lock:
            _limiter._buckets.clear()

    def test_buckets_are_independent(self):
        from errors import RateLimitedError
        from rate_limit import make_rate_limit_dependency

        req = MagicMock()
        req.headers = {"x-api-key": "isolate"}
        req.client = MagicMock(host="1.1.1.1")

        slack_dep = make_rate_limit_dependency("slack_webhook", limit=2)
        ingest_dep = make_rate_limit_dependency("ingest", limit=2)

        slack_dep(req)
        slack_dep(req)
        with pytest.raises(RateLimitedError):
            slack_dep(req)
        # Different bucket, same client -> still under its own limit.
        ingest_dep(req)
        ingest_dep(req)
        with pytest.raises(RateLimitedError):
            ingest_dep(req)


# =====================================================================
# /api/ready endpoint
# =====================================================================
class TestReadyEndpoint:
    def test_returns_200_when_all_deps_healthy(self, client):
        with patch(
            "observability.check_dependencies",
            return_value={"ok": True, "checks": []},
        ):
            r = client.get("/api/ready")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True

    def test_returns_503_when_any_dep_unhealthy(self, client):
        with patch(
            "observability.check_dependencies",
            return_value={
                "ok": False,
                "checks": [
                    {"name": "supabase", "ok": False, "reason": "ConnectionError"},
                    {"name": "hydradb", "ok": True},
                    {"name": "openai", "ok": True},
                ],
            },
        ):
            r = client.get("/api/ready")
        assert r.status_code == 503
        body = r.json()
        # Per-check breakdown is preserved so the failure is debuggable.
        assert body["ok"] is False
        names = [c["name"] for c in body["checks"]]
        assert "supabase" in names

    def test_is_public(self, client):
        # /api/ready is a probe -- no auth header required, must respond.
        with patch(
            "observability.check_dependencies",
            return_value={"ok": True, "checks": []},
        ):
            r = client.get("/api/ready")
        assert r.status_code in (200, 503)


# =====================================================================
# startup.py  --  production-mode validation
# =====================================================================
class TestProductionValidation:
    """The production-mode guard refuses to start on unsafe configs."""

    def _populate_valid_env(self, monkeypatch):
        """Set every REQUIRED_ENV_VAR to a non-blank, non-placeholder
        value so the production check runs against the new guard only."""
        for name in (
            "APP_API_KEY",
            "HYDRADB_API_KEY",
            "HYDRADB_TENANT_ID",
            "OPENAI_API_KEY",
            "SUPABASE_URL",
            "SUPABASE_JWT_SECRET",
            "SUPABASE_SERVICE_ROLE_KEY",
            "SLACK_CLIENT_ID",
            "SLACK_CLIENT_SECRET",
            "SLACK_REDIRECT_URI",
            "SLACK_OAUTH_STATE_SECRET",
            "SLACK_SIGNING_SECRET",
        ):
            monkeypatch.setenv(name, f"real_value_for_{name}")
        # Production guards expect HTTPS redirect + non-localhost CORS +
        # FRONTEND_BASE_URL set.
        monkeypatch.setenv("SLACK_REDIRECT_URI", "https://api.example.com/api/slack/oauth/callback")
        monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
        monkeypatch.setenv("FRONTEND_BASE_URL", "https://app.example.com")

    def test_non_production_does_not_run_extra_checks(self, monkeypatch):
        from startup import validate_required_env

        self._populate_valid_env(monkeypatch)
        # CORS pointing at localhost would FAIL prod check; in non-prod
        # mode it's allowed.
        monkeypatch.setenv("CORS_ORIGINS", "http://localhost:5173")
        monkeypatch.setenv("ENVIRONMENT", "local")
        # No raise.
        validate_required_env()

    def test_production_rejects_localhost_cors(self, monkeypatch):
        from startup import StartupConfigError, validate_required_env

        self._populate_valid_env(monkeypatch)
        monkeypatch.setenv("CORS_ORIGINS", "http://localhost:5173")
        monkeypatch.setenv("ENVIRONMENT", "production")
        with pytest.raises(StartupConfigError) as exc:
            validate_required_env()
        assert "CORS_ORIGINS" in str(exc.value)

    def test_production_rejects_missing_frontend_base_url(self, monkeypatch):
        from startup import StartupConfigError, validate_required_env

        self._populate_valid_env(monkeypatch)
        monkeypatch.delenv("FRONTEND_BASE_URL", raising=False)
        monkeypatch.setenv("ENVIRONMENT", "production")
        with pytest.raises(StartupConfigError) as exc:
            validate_required_env()
        assert "FRONTEND_BASE_URL" in str(exc.value)

    def test_production_rejects_http_slack_redirect(self, monkeypatch):
        from startup import StartupConfigError, validate_required_env

        self._populate_valid_env(monkeypatch)
        monkeypatch.setenv("SLACK_REDIRECT_URI", "http://api.example.com/api/slack/oauth/callback")
        monkeypatch.setenv("ENVIRONMENT", "production")
        with pytest.raises(StartupConfigError) as exc:
            validate_required_env()
        assert "HTTPS" in str(exc.value) or "https" in str(exc.value).lower()

    def test_production_rejects_placeholder_secret(self, monkeypatch):
        from startup import StartupConfigError, validate_required_env

        self._populate_valid_env(monkeypatch)
        # A common .env.example placeholder.
        monkeypatch.setenv("APP_API_KEY", "replace-with-a-long-random-string")
        monkeypatch.setenv("ENVIRONMENT", "production")
        with pytest.raises(StartupConfigError) as exc:
            validate_required_env()
        assert "APP_API_KEY" in str(exc.value)

    def test_production_accepts_clean_config(self, monkeypatch):
        from startup import validate_required_env

        self._populate_valid_env(monkeypatch)
        monkeypatch.setenv("ENVIRONMENT", "production")
        # No raise.
        validate_required_env()


class TestSecretsAudit:
    def test_audit_logs_redacted_summary(self, caplog, monkeypatch):
        import logging

        from startup import _audit_secrets

        monkeypatch.setenv("APP_API_KEY", "abcd1234efgh5678")
        monkeypatch.setenv("SLACK_SIGNING_SECRET", "")  # missing
        with caplog.at_level(logging.INFO, logger="startup"):
            _audit_secrets()
        records = [r for r in caplog.records if r.message == "secrets_audit"]
        assert len(records) == 1
        record = records[0]
        # The redacted summary attaches per-secret extras to the record.
        # Real values must NEVER appear in the log line.
        rendered = str(record.__dict__)
        assert "abcd1234efgh5678" not in rendered
        # Fingerprint + length DO appear (so operators can spot
        # "did I paste the wrong key?" mistakes).
        assert "abcd" in getattr(record, "APP_API_KEY", "")
        assert "5678" in getattr(record, "APP_API_KEY", "")
        # Missing secrets are flagged plainly.
        assert getattr(record, "SLACK_SIGNING_SECRET") == "missing"


# =====================================================================
# supabase_client.py  --  Slack event claim
# =====================================================================
class TestClaimSlackEventId:
    def test_first_claim_returns_true(self):
        from supabase_client import claim_slack_event_id

        mock_client = MagicMock()
        # supabase insert with returning='representation' yields .data
        # = [the inserted row] on the FIRST claim.
        mock_client.table.return_value.insert.return_value.execute.return_value.data = [{"event_id": "Ev_1"}]
        with patch("supabase_client.get_supabase", return_value=mock_client):
            assert claim_slack_event_id(event_id="Ev_1") is True

    def test_duplicate_returns_false(self):
        from supabase_client import claim_slack_event_id

        mock_client = MagicMock()
        # When the row already exists, ignore_duplicates returns empty .data.
        mock_client.table.return_value.insert.return_value.execute.return_value.data = []
        with patch("supabase_client.get_supabase", return_value=mock_client):
            assert claim_slack_event_id(event_id="Ev_dup") is False

    def test_blank_event_id_returns_false(self):
        from supabase_client import claim_slack_event_id

        # Defensive: a blank id shouldn't make a DB round trip.
        with patch("supabase_client.get_supabase") as mock_get:
            assert claim_slack_event_id(event_id="") is False
        mock_get.assert_not_called()

    def test_duplicate_error_returns_false(self):
        from supabase_client import claim_slack_event_id

        mock_client = MagicMock()
        # Simulate the supabase client raising a unique-constraint
        # error directly (different client versions behave differently).
        mock_client.table.return_value.insert.return_value.execute.side_effect = RuntimeError(
            "duplicate key value violates unique constraint"
        )
        with patch("supabase_client.get_supabase", return_value=mock_client):
            assert claim_slack_event_id(event_id="Ev_2") is False

    def test_db_outage_fails_open(self):
        # Phase 7: when the dedupe DB is unreachable we fail OPEN
        # (return True so the event still gets processed). Better to
        # process the occasional duplicate during an outage than to
        # drop every webhook delivery.
        from supabase_client import claim_slack_event_id

        mock_client = MagicMock()
        mock_client.table.return_value.insert.return_value.execute.side_effect = RuntimeError("network timeout")
        with patch("supabase_client.get_supabase", return_value=mock_client):
            assert claim_slack_event_id(event_id="Ev_3") is True


# =====================================================================
# realtime_ingest  --  two-tier dedupe behavior
# =====================================================================
class TestTwoTierDedupe:
    @pytest.fixture(autouse=True)
    def _reset(self):
        import realtime_ingest as r

        r._seen_event_ids.clear()
        yield
        r._seen_event_ids.clear()

    def test_in_memory_cache_short_circuits(self):
        # If the in-memory cache has the event, we should NOT call
        # the supabase claim function at all.
        from realtime_ingest import _event_already_seen

        # First call -> not seen, will fall through to claim.
        with patch(
            "supabase_client.claim_slack_event_id",
            return_value=True,
        ) as mock_claim:
            assert _event_already_seen("Ev_X") is False
            # Second call should be served by the in-memory cache.
            assert _event_already_seen("Ev_X") is True
        assert mock_claim.call_count == 1

    def test_durable_claim_says_dup_marks_in_memory(self):
        from realtime_ingest import _event_already_seen, _seen_event_ids

        # Supabase says we lost the race.
        with patch(
            "supabase_client.claim_slack_event_id",
            return_value=False,
        ):
            assert _event_already_seen("Ev_Y") is True
        # Subsequent calls short-circuit on the in-memory cache.
        with patch(
            "supabase_client.claim_slack_event_id",
        ) as mock_claim:
            assert _event_already_seen("Ev_Y") is True
            mock_claim.assert_not_called()


# =====================================================================
# log context binding -- auth_supabase populates user_id / workspace_id
# =====================================================================
class TestLogContextBinding:
    @pytest.fixture(autouse=True)
    def _reset(self):
        from logging_config import bind_user_context

        bind_user_context(None, None)
        yield
        bind_user_context(None, None)

    def test_require_user_binds_user_id(self):
        # Build a real bearer token that decodes to a known user id,
        # then call require_user as a plain function (no FastAPI plumbing).
        import jwt as pyjwt

        from auth_supabase import SUPABASE_JWT_ALGORITHM, require_user
        from logging_config import _user_id, _workspace_id

        token = pyjwt.encode(
            {
                "sub": "user-from-token",
                "email": "x@example.com",
                "aud": "authenticated",
                "exp": int(time.time()) + 60,
            },
            os.environ["SUPABASE_JWT_SECRET"],
            algorithm=SUPABASE_JWT_ALGORITHM,
        )
        result = require_user(authorization=f"Bearer {token}")
        assert result.id == "user-from-token"
        # The Phase 7 binding sets user_id on the contextvar.
        assert _user_id.get() == "user-from-token"
        # workspace_id stays None on user-only routes.
        assert _workspace_id.get() is None
